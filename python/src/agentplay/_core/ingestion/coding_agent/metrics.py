"""Cost-efficiency counters for `CodingAgentTask` — pure, no LLM.

Walks the spans that belong to a task once and returns the deterministic
counter set stored for a task. Ratios
(`tokens_per_edit`, `cache_hit_ratio`, `compaction_waste_ratio`,
`context_pressure_score`) are intentionally *not* stored: they are
derived from these counters plus the token aggregates, so slicers
(by-language, by-project, by-cohort) can compute ratios over filtered
subsets without re-walking spans.

# Why one module, one function

Every caller recomputes these counters from spans. Centralizing the
formula here means any future tuning (e.g. adding "TodoWrite" as a
non-edit) lands in exactly one place.

# Why counters, not ratios

A ratio is a function of (numerator, denominator). The denominator
wanted changes by slice - "tokens per edit across this
project" vs "across this user" vs "across this week" — so we keep the
inputs and let the consumer compose.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentplay._core.ingestion.coding_agent.file_paths import extract_file_path as _file_path
from agentplay._core.ingestion.coding_agent.spans import SpanData

# Tool taxonomy used by the counter walk.
#
# SEARCH_TOOLS: "Search" and "ListDir" are
# the canonical names Cursor's SemanticSearch and Kiro's list_directory
# alias to -- tool_taxonomy.TOOL_ALIASES also produces them for Copilot,
# Copilot-in-VS-Code and OpenCode. Missing them here meant search_count
# undercounted every non-Claude, non-Codex agent's exploration activity;
# those tool spans still hit tool_count, just no bucket.
EDIT_TOOLS: frozenset[str] = frozenset({"Edit", "MultiEdit", "Write"})
READ_TOOLS: frozenset[str] = frozenset({"Read", "NotebookRead"})
SEARCH_TOOLS: frozenset[str] = frozenset({"Grep", "Glob", "Search", "ListDir", "LS"})
SHELL_TOOLS: frozenset[str] = frozenset({"Bash", "BashOutput"})

# Taxonomy for the ordering-dependent metrics (read-before-edit and
# full-rewrite). Deliberately distinct from the bucket sets above: read-first
# folds search / web tools into "reads", and the edit set excludes `Write`
# (a full rewrite, counted on its own) and `MultiEdit`.
RF_READ_TOOLS: frozenset[str] = frozenset(
    {"Read", "Glob", "Grep", "Search", "ListDir", "LS", "WebFetch", "WebSearch"}
)
RF_EDIT_TOOLS: frozenset[str] = frozenset({"Edit", "NotebookEdit"})
RF_WRITE_TOOLS: frozenset[str] = frozenset({"Write"})


@dataclass(frozen=True, slots=True)
class TaskCounters:
    """Per-task counter snapshot."""

    tool_count: int
    edit_count: int
    read_count: int
    search_count: int
    shell_count: int
    error_count: int
    max_turn_prompt_tokens: int
    cache_read_total: int
    cache_write_total: int
    # Ordering-dependent (read-before-edit, full-rewrite).
    edit_op_count: int  # Edit / NotebookEdit calls — read-first denominator
    blind_edit_count: int  # edits to a file not read earlier in the task
    full_rewrite_count: int  # Write calls — full-file rewrites
    mutation_count: int  # Edit / NotebookEdit / Write — full-rewrite denominator
    # Context-health signals (duplicate reads, repeated edits, context
    # resets, context pressure, useful edits). Counts only; ratios are
    # derived by the consumer.
    duplicate_read_count: int  # reads of an already-read file
    repeated_edit_count: int  # consecutive Edit/NotebookEdit to the same file
    useful_edit_count: int  # successful mutations (for tokens-per-useful-edit)
    context_reset_count: int  # turns where prompt dropped >40%
    context_steep_count: int  # turns where prompt grew >50% (pressure numerator)
    context_comparison_count: int  # turn-to-turn comparisons (pressure denominator)


def compute_task_counters(
    spans: list[SpanData],
    *,
    start_turn: int,
    end_turn: int,
) -> TaskCounters:
    """Walk a session's spans → counters for the named turn range.

    Args:
        spans: full session span list. Filtered to `[start_turn,
            end_turn]` internally — callers don't pre-filter.
        start_turn: 1-indexed first turn of the task (inclusive).
        end_turn: 1-indexed last turn of the task (inclusive).

    Returns:
        `TaskCounters`. Counts that don't apply (no edits at all) are
        zero, never None.
    """
    tool_count = edit = read = search = shell = errors = 0
    max_prompt = 0
    cache_read = cache_write = 0
    # (start_time, tool, file_path, success) for the task's tool spans, walked
    # in chronological order for the order-dependent metrics.
    ordered: list[tuple[object, str, str | None, bool]] = []
    # (turn_num, prompt_tokens) for context resets / pressure.
    turn_prompts: list[tuple[int, int]] = []

    def in_range(n: int) -> bool:
        return start_turn <= n <= end_turn

    for s in spans:
        # Turn span — picks up peak prompt + per-turn cache numbers.
        if s.name.startswith("Turn "):
            n = _parse_turn_num(s.name)
            if n is None or not in_range(n):
                continue
            prompt = int(s.attributes.get("llm.token_count.prompt", 0))
            if prompt > max_prompt:
                max_prompt = prompt
            cache_read += int(s.attributes.get("llm.token_count.cache_read", 0))
            cache_write += int(s.attributes.get("llm.token_count.cache_write", 0))
            turn_prompts.append((n, prompt))
            continue

        # Tool span — counts, attributed to the parent turn.
        tool = s.attributes.get("tool.name")
        if not tool or not s.parent_id:
            continue
        n = _turn_num_from_parent(s.parent_id)
        if n is None or not in_range(n):
            continue
        tool_count += 1
        if tool in EDIT_TOOLS:
            edit += 1
        elif tool in READ_TOOLS:
            read += 1
        elif tool in SEARCH_TOOLS:
            search += 1
        elif tool in SHELL_TOOLS:
            shell += 1
        if s.status_code == "ERROR":
            errors += 1
        ordered.append(
            (s.start_time, tool, _file_path(s.attributes.get("input.value")), s.status_code == "OK")
        )

    ow = _ordered_walk_metrics(ordered)
    tm = _turn_series_metrics(turn_prompts)

    return TaskCounters(
        tool_count=tool_count,
        edit_count=edit,
        read_count=read,
        search_count=search,
        shell_count=shell,
        error_count=errors,
        max_turn_prompt_tokens=max_prompt,
        cache_read_total=cache_read,
        cache_write_total=cache_write,
        edit_op_count=ow["edit_op_count"],
        blind_edit_count=ow["blind_edit_count"],
        full_rewrite_count=ow["full_rewrite_count"],
        mutation_count=ow["mutation_count"],
        duplicate_read_count=ow["duplicate_read_count"],
        repeated_edit_count=ow["repeated_edit_count"],
        useful_edit_count=ow["useful_edit_count"],
        context_reset_count=tm["context_reset_count"],
        context_steep_count=tm["context_steep_count"],
        context_comparison_count=tm["context_comparison_count"],
    )


def _ordered_walk_metrics(
    ordered: list[tuple[object, str, str | None, bool]],
) -> dict[str, int]:
    """Order-dependent tool metrics from time-ordered (tool, file, success) calls.

    Computes the read-first and full-rewrite counts, duplicate reads,
    repeated edits to the same file, and the `useful` count for
    tokens-per-useful-edit, all in one pass:

    - a file Read (incl. search/web) seeds the read-set; an `Edit`/`NotebookEdit`
      on a file not yet in it is a "blind edit"; `Write` is a full rewrite.
    - re-reading an already-read file is a duplicate read.
    - a consecutive `Edit`/`NotebookEdit` to the same file is a repeated edit.
    - a successful mutation (`Edit`/`NotebookEdit`/`Write`) is a useful edit.
    """
    read_files: set[str] = set()
    seen_reads: set[str] = set()
    blind = edit_ops = writes = mutations = 0
    duplicate_reads = repeated_edits = useful_edits = 0
    last_edit_file: str | None = None

    for _, tool, fp, ok in sorted(ordered, key=lambda x: x[0]):
        is_edit = tool in RF_EDIT_TOOLS
        is_write = tool in RF_WRITE_TOOLS
        if is_edit:
            edit_ops += 1
            mutations += 1
            if fp and fp == last_edit_file:
                repeated_edits += 1
            last_edit_file = fp
        else:
            if is_write:
                writes += 1
                mutations += 1
            last_edit_file = None
        if (is_edit or is_write) and ok:
            useful_edits += 1

        if not fp:
            continue
        if tool in RF_READ_TOOLS:
            if fp in seen_reads:
                duplicate_reads += 1
            seen_reads.add(fp)
            read_files.add(fp)
        elif is_edit and fp not in read_files:
            blind += 1

    return {
        "edit_op_count": edit_ops,
        "blind_edit_count": blind,
        "full_rewrite_count": writes,
        "mutation_count": mutations,
        "duplicate_read_count": duplicate_reads,
        "repeated_edit_count": repeated_edits,
        "useful_edit_count": useful_edits,
    }


def _turn_series_metrics(turn_prompts: list[tuple[int, int]]) -> dict[str, int]:
    """Context resets + pressure counters from the task's per-turn prompt series.

    A >40% prompt drop between consecutive turns is a reset; a >50% jump is
    a steep increase. The pressure ratio (steep / comparisons) is derived
    by the consumer.
    """
    prompts = [p for _, p in sorted(turn_prompts, key=lambda x: x[0])]
    resets = steep = comparisons = 0
    for i in range(1, len(prompts)):
        prev, curr = prompts[i - 1], prompts[i]
        if prev > 1000 and curr < prev * 0.6:
            resets += 1
        if prev > 0:
            comparisons += 1
            if curr > prev * 1.5:
                steep += 1
    return {
        "context_reset_count": resets,
        "context_steep_count": steep,
        "context_comparison_count": comparisons,
    }


def session_tool_distribution(spans: list[SpanData]) -> dict[str, int]:
    """Per-named-tool span counts across the whole session.

    The session-level sibling of `slash_commands` / `skills_used`: an opaque
    count distribution kept in the session payload rather than as a
    scalar counter, since a JSON map can't be summed like one.

    Counts every tool span (any `tool.name`), session-wide — no turn-range
    filter, unlike `compute_task_counters` which is scoped to one task.
    """
    tools: dict[str, int] = {}
    for s in spans:
        tool = s.attributes.get("tool.name")
        if tool:
            tools[tool] = tools.get(tool, 0) + 1
    return tools


def _parse_turn_num(name: str) -> int | None:
    """`Turn N` → N. Returns None when the suffix isn't an int."""
    parts = name.split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _turn_num_from_parent(parent_id: str) -> int | None:
    """Extract N from `{trace_id}:turn:{N}` — the convention every parser emits."""
    head, _, tail = parent_id.rpartition(":turn:")
    if not head or not tail:
        return None
    try:
        return int(tail)
    except ValueError:
        return None


__all__ = [
    "EDIT_TOOLS",
    "READ_TOOLS",
    "SEARCH_TOOLS",
    "SHELL_TOOLS",
    "TaskCounters",
    "compute_task_counters",
    "session_tool_distribution",
]
