"""Session-level compute - pure, no LLM.

Rolls a parsed span list up into session metrics: token
aggregation (fresh / cache-read / cache-write split), the three whole-session
waste components (interrupt / repeat / retry) + floor drift, model resolution,
automation detection, and the **derived behaviour metrics** a session
summary exposes (efficiency score, the read/edit
behaviour ratios, error rate, tool-calls-per-turn, and the tool / skill
distributions).

The public entry for callers is :func:`build_session_metrics`, which returns
the session metrics dict.

SESSION cost is NOT computed here: a consumer may recompute ``total_cost_usd``
from tokens + model, so only token components are emitted. The one
dollar-denominated exception is the marginal-waste trio
(``*_marginal_usd``, :func:`_marginal_waste_usd`): it must price per-turn
token components that never leave this layer, so it is computed here off the
shared rate card (``agentplay._core.pricing``).
"""

from __future__ import annotations

import json
from typing import Any, NamedTuple

from agentplay._core.ingestion.coding_agent.edit_lines import edit_line_counts
from agentplay._core.ingestion.coding_agent.file_paths import extract_file_path as _file_path
from agentplay._core.ingestion.coding_agent.metrics import (
    RF_EDIT_TOOLS,
    RF_READ_TOOLS,
    RF_WRITE_TOOLS,
    session_tool_distribution,
)
from agentplay._core.ingestion.coding_agent.skill_names import normalize_skill_name
from agentplay._core.ingestion.coding_agent.spans import (
    SpanData,
    last_genuine_model_id,
    model_id_or_none,
)
from agentplay._core.pricing import MODEL_RATES, normalize_model_key

# ── automation detection ───────────────────────────────────────────
_AUTOMATION_MARKERS = (
    "sandbox reminder",
    "you are inside a remote e2b",
    "/workspace/repo",
)
_AUTOMATION_SCAN_SPANS = 40  # markers live in early system/setup content


def detect_automated(spans: list[SpanData]) -> bool:
    """True when early span content carries an automation marker.

    Bounded prefix scan (first ``_AUTOMATION_SCAN_SPANS`` spans, string
    attribute values only) so it stays flat-cost even for huge sessions.
    """
    for span in spans[:_AUTOMATION_SCAN_SPANS]:
        for value in span.attributes.values():
            if isinstance(value, str):
                low = value.lower()
                if any(m in low for m in _AUTOMATION_MARKERS):
                    return True
    return False


# ── waste components ────────────────────────
_EDIT_TOOLS: frozenset[str] = frozenset({"Edit", "MultiEdit", "Write"})
_COMPACTION_DROP_RATIO = 0.6
_COMPACTION_MIN_TOKENS = 1000
_RETRY_INPUT_PREFIX = 60


class _ToolCall(NamedTuple):
    tool_name: str
    input_value: str
    # True = OK, False = ERROR, None = UNSET (the call's result was never
    # observed -- a dangling Cursor call). Unknown is neither a success nor a
    # failure: it must not seed a retry pair, count against the error rate,
    # or contribute edit lines.
    success: bool | None
    # Turn number the call belongs to (via the tool span's parent turn span),
    # or None when the parent isn't a turn. Lets waste land in BILLED tokens
    # of the specific turns involved instead of avg context windows.
    turn: int | None = None


def _context_growth(spans: list[SpanData]) -> list[dict]:
    """Per-turn prompt-token series, ascending by turn — for floor drift."""
    rows: list[dict] = []
    for s in spans:
        if not s.name.startswith("Turn "):
            continue
        try:
            n = int(s.name.split()[1])
        except (ValueError, IndexError):
            continue
        rows.append(
            {"turn": n, "prompt_tokens": int(s.attributes.get("llm.token_count.prompt") or 0)}
        )
    rows.sort(key=lambda r: r["turn"])
    return rows


def _turn_number(name: str) -> int | None:
    """Parse ``"Turn N"`` → N, else None."""
    if not name.startswith("Turn "):
        return None
    try:
        return int(name.split()[1])
    except (ValueError, IndexError):
        return None


def _tool_calls(spans: list[SpanData]) -> list[_ToolCall]:
    """Chronologically ordered tool calls — for repeat / retry detection."""
    turn_by_id = {s.span_id: n for s in spans if (n := _turn_number(s.name)) is not None}
    tool_spans = [s for s in spans if s.attributes.get("tool.name")]
    tool_spans.sort(key=lambda s: s.start_time)
    return [
        _ToolCall(
            tool_name=s.attributes["tool.name"],
            input_value=s.attributes.get("input.value", "") or "",
            success=(None if s.status_code == "UNSET" else s.status_code != "ERROR"),
            turn=turn_by_id.get(s.parent_id or ""),
        )
        for s in tool_spans
    ]


def interrupt_count_measured(spans: list[SpanData]) -> int | None:
    """User-interrupt total, or ``None`` when the harness never reported it.

    The distinction is load-bearing for the emitted field. A harness that emits no
    ``harness.session_signals`` is "not instrumented", which is NOT the same as
    "zero interrupts": persisting 0 for it launders an unmeasured session into a
    real-looking measurement, and a consumer needs to be able to tell those
    apart. Waste accounting wants a number, so
    :func:`_interrupt_count` keeps coercing to 0 for that use only.
    """
    root = next((s for s in spans if s.parent_id is None), None)
    if root is None:
        return None
    raw = root.attributes.get("harness.session_signals")
    if not raw:
        return None
    try:
        sig = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return None
    if not isinstance(sig, dict):
        return None
    interrupts = sig.get("interrupts")
    if not isinstance(interrupts, dict):
        return None
    try:
        return int(interrupts.get("total", 0))
    except (TypeError, ValueError):
        return None


def _interrupt_count(spans: list[SpanData]) -> int:
    """Interrupt total coerced to 0 — for waste maths, which needs a number."""
    return interrupt_count_measured(spans) or 0


def repeated_edits_to_same_file(tool_calls: list[_ToolCall]) -> int:
    """Count of consecutive Edit calls to the same file (thrashing)."""
    count = 0
    last_edit_file: str | None = None
    for tc in tool_calls:
        if tc.tool_name in _EDIT_TOOLS:
            file_path = _file_path(tc.input_value)
            if file_path and file_path == last_edit_file:
                count += 1
            last_edit_file = file_path
        else:
            last_edit_file = None
    return count


def _same_operation(a: _ToolCall, b: _ToolCall) -> bool:
    if a.tool_name != b.tool_name:
        return False
    ia, ib = (a.input_value or "").strip(), (b.input_value or "").strip()
    if not ia or not ib:
        return False
    return ia[:_RETRY_INPUT_PREFIX] == ib[:_RETRY_INPUT_PREFIX]


def error_retry_sequences(tool_calls: list[_ToolCall]) -> int:
    """Count error→retry patterns: a failed call re-attempted (within 3 calls)
    with the same tool AND the same input prefix — the same operation."""
    count = 0
    for i in range(len(tool_calls) - 1):
        if tool_calls[i].success is False:  # an UNSET call is not a failed one
            for j in range(i + 1, min(i + 4, len(tool_calls))):
                if _same_operation(tool_calls[i], tool_calls[j]):
                    count += 1
                    break
    return count


def _find_compaction_events(context_growth: list[dict]) -> list[tuple[int, int, int]]:
    events: list[tuple[int, int, int]] = []
    for i in range(1, len(context_growth)):
        prev = context_growth[i - 1].get("prompt_tokens", 0)
        curr = context_growth[i].get("prompt_tokens", 0)
        if prev > _COMPACTION_MIN_TOKENS and curr < prev * _COMPACTION_DROP_RATIO:
            events.append((i, prev, curr))
    return events


def floor_drift_score(context_growth: list[dict]) -> float | None:
    """0-1: fraction of consecutive post-compaction floors that grew. None when
    fewer than 2 compaction events (nothing to compare)."""
    if len(context_growth) < 2:
        return None
    events = _find_compaction_events(context_growth)
    if len(events) < 2:
        return None
    floors = [post for _, _, post in events]
    increases = sum(1 for i in range(1, len(floors)) if floors[i] > floors[i - 1])
    return round(increases / (len(floors) - 1), 2)


def _repeat_edit_turns(tool_calls: list[_ToolCall]) -> set[int]:
    """Turns containing consecutive same-file Edit calls (thrashing).

    Same detection as :func:`repeated_edits_to_same_file`, but collects the
    TURNS involved instead of counting events — the unit the billed-waste
    computation charges.
    """
    turns: set[int] = set()
    last_edit_file: str | None = None
    for tc in tool_calls:
        if tc.tool_name in _EDIT_TOOLS:
            file_path = _file_path(tc.input_value)
            if file_path and file_path == last_edit_file and tc.turn is not None:
                turns.add(tc.turn)
            last_edit_file = file_path
        else:
            last_edit_file = None
    return turns


def _retry_turns(tool_calls: list[_ToolCall]) -> set[int]:
    """Turns containing an error→retry pair (same detection as
    :func:`error_retry_sequences`; collects both calls' turns)."""
    turns: set[int] = set()
    for i in range(len(tool_calls) - 1):
        if tool_calls[i].success is False:  # an UNSET call is not a failed one
            for j in range(i + 1, min(i + 4, len(tool_calls))):
                if _same_operation(tool_calls[i], tool_calls[j]):
                    if tool_calls[i].turn is not None:
                        turns.add(tool_calls[i].turn)
                    if tool_calls[j].turn is not None:
                        turns.add(tool_calls[j].turn)
                    break
    return turns


# ── marginal waste in dollars ──────────────────────────────────────
# Per-component cache multipliers when the rate card carries no explicit
# override: read 0.1x input, 5-minute write 1.25x, 1-hour write 2x. These are
# the same defaults `agentplay._core.pricing` applies, so a marginal dollar
# here is a subset of the same bill computed for the whole session.
_CACHE_READ_MULTIPLIER = 0.1
_CACHE_WRITE_5M_MULTIPLIER = 1.25
_CACHE_WRITE_1H_MULTIPLIER = 2.0


def _component_rates(
    model_id: str | None, request: tuple[int, int, int, int, int] | None = None
) -> tuple[float, float, float, float, float] | None:
    """Per-Mtok USD rates ``(fresh_input, cache_read, cache_write_5m,
    cache_write_1h, output)`` for a model, or None when unpriceable.

    ``request`` is ONE request's token components. When its cache-inclusive
    input crosses the card's ``long_context_threshold``, the whole request is
    billed at the long-context tier, the same per-request rule as
    ``builder.estimate_cost_usd``. Turn aggregates pass no ``request``: they
    can sum several requests, so they stay at base rates.

    Resolves through the ONE rate card (``agentplay._core.pricing.MODEL_RATES``
    via ``normalize_model_key``) — no local price table. Missing cache keys
    fall back to the standard multipliers above, which reproduce the
    published cache prices for every Anthropic entry on the card. Models
    the card lacks are unpriceable here (None).
    """
    if not model_id:
        return None
    entry = MODEL_RATES.get(normalize_model_key(model_id))
    if not entry:
        return None
    input_rate = float(entry["input"])
    rates = (
        input_rate,
        float(entry.get("cache_read", input_rate * _CACHE_READ_MULTIPLIER)),
        float(entry.get("cache_write", input_rate * _CACHE_WRITE_5M_MULTIPLIER)),
        input_rate * _CACHE_WRITE_1H_MULTIPLIER,
        float(entry["output"]),
    )
    threshold = entry.get("long_context_threshold")
    if request is None or threshold is None or sum(request[:4]) <= threshold:
        return rates
    in_mult = float(entry["long_context_input_multiplier"])
    *input_rates, output_rate = rates
    return (
        *(rate * in_mult for rate in input_rates),
        output_rate * float(entry["long_context_output_multiplier"]),
    )


def _turn_token_components(spans: list[SpanData]) -> dict[int, tuple[int, int, int, int, int]]:
    """Per-turn billed token split ``{turn: (fresh, cache_read,
    cache_write_5m, cache_write_1h, output)}`` from the turn spans.

    Reads the same attributes ``aggregate_session_spans`` sums session-wide,
    so both grains price off identical inputs. ``fresh`` prefers the
    the DERIVED ``total_billed_prompt − cache_read − cache_write`` (clamped
    ≥ 0), so a marginal dollar is priced off the same split as the
    session's own bill. The parser-stamped
    ``llm.token_count.input_fresh`` is only the FALLBACK, for parsers that
    never stamp a billed prompt (codex, opencode), because its meaning is
    not uniform across parsers: Claude Code stamps genuine
    fresh input (identical to the derived value by construction — see
    ``claude_code.py``'s ``per_msg_prompt = fresh + write + read``), but
    Cursor stamps ``prompt − cache_read``, which still CONTAINS the cache
    writes. Preferring it there billed those tokens twice (once at the
    input rate inside "fresh", again at the 1.25x write rate).

    A turn stamping NEITHER key falls back to plain ``prompt`` minus the
    cache components — mirroring ``aggregate_session_spans``' own
    no-``input_fresh`` fallback (``input_fresh = input_tokens``), so a
    parser that only stamps ``prompt`` prices its waste off the same
    number its session bill uses instead of silently pricing fresh=0.
    (Every current parser stamps one of the two keys or all-zeros — the
    all-zero shape is caught by the no-billed-tokens None guard — so this
    tail is for parser shapes we haven't met yet.)
    Duplicate ``Turn N`` spans resolve last-wins, matching
    ``compute_session_waste``'s ``billed_by_turn``.
    """
    comps: dict[int, tuple[int, int, int, int, int]] = {}
    for s in spans:
        n = _turn_number(s.name)
        if n is None:
            continue
        attrs = s.attributes
        cache_read = int(attrs.get("llm.token_count.cache_read") or 0)
        cache_write = int(attrs.get("llm.token_count.cache_write") or 0)
        # The 1h subset can never exceed the write total (defensive clamp).
        w1h = min(int(attrs.get("llm.token_count.cache_write_1h") or 0), cache_write)
        w5m = cache_write - w1h
        if "llm.token_count.total_billed_prompt" in attrs:
            billed_prompt = int(attrs.get("llm.token_count.total_billed_prompt") or 0)
            fresh = max(billed_prompt - cache_read - cache_write, 0)
        elif "llm.token_count.input_fresh" in attrs:
            fresh = int(attrs.get("llm.token_count.input_fresh") or 0)
        else:
            # Neither key: plain `prompt` IS fresh — deliberately NOT minus
            # the cache components, because that is exactly how the session
            # BILL prices this shape (`aggregate_session_spans` falls back to
            # `input_fresh = input_tokens`, and total input is rebuilt as
            # fresh + read + write on top of it). A
            # "better" cache-subtracted guess here would make a retry turn's
            # marginal dollars disagree with the same turn's share of the
            # bill — consistency with the bill outranks a better estimate.
            fresh = int(attrs.get("llm.token_count.prompt") or 0)
        output = int(attrs.get("llm.token_count.completion") or 0)
        comps[n] = (fresh, cache_read, w5m, w1h, output)
    return comps


def _turn_cost_usd(
    comp: tuple[int, int, int, int, int], rates: tuple[float, float, float, float, float]
) -> float:
    """One turn's full billed dollars: each token component at its own rate."""
    fresh, cache_read, w5m, w1h, output = comp
    in_r, read_r, w5m_r, w1h_r, out_r = rates
    return (
        fresh * in_r + cache_read * read_r + w5m * w5m_r + w1h * w1h_r + output * out_r
    ) / 1_000_000.0


def _repeat_edit_call_counts(tool_calls: list[_ToolCall]) -> dict[int, int]:
    """``{turn: redundant edit-call count}`` — same detection as
    :func:`_repeat_edit_turns`, but counting the redundant calls per turn
    (the numerator of the repeat-edit marginal share)."""
    counts: dict[int, int] = {}
    last_edit_file: str | None = None
    for tc in tool_calls:
        if tc.tool_name in _EDIT_TOOLS:
            file_path = _file_path(tc.input_value)
            if file_path and file_path == last_edit_file and tc.turn is not None:
                counts[tc.turn] = counts.get(tc.turn, 0) + 1
            last_edit_file = file_path
        else:
            last_edit_file = None
    return counts


def _resolve_pricing_model(spans: list[SpanData], model_id: str | None) -> str | None:
    """The model id the marginal dollars price against.

    Mirrors session pricing's fallback chain: an explicit caller-supplied id
    (typically the model the session's ``total_cost_usd`` is priced with)
    wins, else the root span's model, else the last genuine model
    observed in the spans — exactly ``aggregate_session_spans``' resolution.
    """
    resolved = model_id_or_none(model_id) if model_id is not None else None
    if resolved:
        return resolved
    root = next((s for s in spans if s.parent_id is None), None)
    resolved = model_id_or_none(root.model_name) if root else None
    return resolved or last_genuine_model_id(spans)


def _marginal_waste_usd(
    spans: list[SpanData],
    calls: list[_ToolCall],
    retry_turns: set[int],
    repeat_turns: set[int],
    model_id: str | None,
) -> dict[str, float | None]:
    """The three MARGINAL waste components, in dollars.

    Marginal = what would not have been billed absent the waste event —
    unlike the billed trio (whole billed tokens of every involved turn, an
    upper bound), each component charges
    only the spend the event actually caused:

    - ``retry_waste_marginal_usd`` — each retry turn's FULL billed cost
      (fresh input at the input rate, cache reads at the read rate, 5m/1h
      cache writes at their write rates, output at the output rate): the
      dead attempt's whole bill bought nothing. Charges every turn in the
      same disjoint retry set the billed trio uses.
    - ``interrupt_waste_marginal_usd`` — only the discarded GENERATION:
      interrupts carry no turn ids, so it charges the session-average
      output cost per turn × interrupt count (capped at turn count).
      The interrupted prompt/context is NOT counted — it
      re-bills as cheap cache reads on the next attempt.
    - ``repeat_edit_waste_marginal_usd`` — for each repeat-edit turn, the
      redundant share of its OUTPUT cost: output cost × (redundant edit calls
      in the turn / total tool calls in the turn); the
      whole turn's output when the turn shows no attributable tool calls.
      The turn's prompt side is NOT counted.

    Approximations (per-turn splits come straight from parser-stamped span
    attributes — see :func:`_turn_token_components` — so only the
    attribution itself is approximate): interrupt attribution uses the
    session-average output per turn (no turn ids); the repeat-edit share
    treats a turn's output as evenly spread across its tool calls.

    All values are ≥ 0, and their sum is clamped to the session's own
    billed total (each request's model rate when complete request evidence
    exists, otherwise the session model rate, and Cursor's stamped
    ``session.cost_usd`` when present).

    Returns all three as None — unknown, never a fabricated $0 — when
    EITHER the model can't be priced off the rate card, OR the session
    carries no per-turn billed tokens to price (see the guard below).
    """
    _none = {
        "retry_waste_marginal_usd": None,
        "interrupt_waste_marginal_usd": None,
        "repeat_edit_waste_marginal_usd": None,
    }
    comps = _turn_token_components(spans)
    # An unpriceable MODEL is not the only way to be unknown. A session whose
    # parser stamps no per-turn billed tokens at all — a Cursor transcript
    # with no `cursor_usage_event` lines, cursor_agent_transcript, kiro; all
    # zero every turn token attr and leave any real total on the ROOT span —
    # would price all three components at exactly $0.00 even though its
    # retry/interrupt EVENTS are detected normally (those come from the tool
    # spans, which ARE stamped). A confident $0.00 reads as "measured, no
    # waste here"; the truth is "never measured". Root-level totals cannot be
    # attributed to individual retry turns without inventing an allocation
    # rule, so unknown is the only honest answer.
    if not any(sum(c) for c in comps.values()):
        return _none
    n_turns = len(comps)
    # Validate request coverage before using it: partial evidence must not
    # price a complete turn, and a display model cannot price other models.
    has_events = session_model_usage(spans) is not None
    fallback_rates = _component_rates(_resolve_pricing_model(spans, model_id))
    turn_costs: dict[int, float] = {}
    output_costs: dict[int, float] = {}
    for span in spans:
        n = _turn_number(span.name)
        if n is None:
            continue
        if has_events:
            full, output = 0.0, 0.0
            for event in span.attributes["llm.cost_events"]:
                component = tuple(
                    event.get(k, 0)
                    for k in (
                        "input_tokens",
                        "cache_read_tokens",
                        "cache_write_5m_tokens",
                        "cache_write_1h_tokens",
                        "output_tokens",
                    )
                )
                if not any(component):
                    # Zero-token evidence (a rate-limit-only or model-less
                    # call) prices to $0 under any card; needing its rate
                    # would make every waste figure unknown for nothing
                    # Positive usage still must price.
                    continue
                rates = _component_rates(event.get("model"), component)
                if rates is None:
                    return _none
                full += _turn_cost_usd(component, rates)
                output += component[4] * rates[4] / 1_000_000.0
        else:
            if fallback_rates is None:
                return _none
            full = _turn_cost_usd(comps[n], fallback_rates)
            output = comps[n][4] * fallback_rates[4] / 1_000_000.0
        turn_costs[n], output_costs[n] = full, output

    retry_usd = sum(turn_costs.get(t, 0.0) for t in retry_turns)
    avg_output_cost = sum(output_costs.values()) / n_turns if n_turns else 0.0
    interrupts = min(_interrupt_count(spans), n_turns)
    interrupt_usd = interrupts * avg_output_cost

    redundant_by_turn = _repeat_edit_call_counts(calls)
    calls_by_turn: dict[int, int] = {}
    for tc in calls:
        if tc.turn is not None:
            calls_by_turn[tc.turn] = calls_by_turn.get(tc.turn, 0) + 1
    repeat_usd = 0.0
    for t in repeat_turns:
        denom = calls_by_turn.get(t, 0)
        share = min(redundant_by_turn.get(t, 0) / denom, 1.0) if denom else 1.0
        repeat_usd += output_costs.get(t, 0.0) * share

    # Guardrail invariant, not the estimator: each component is ≥ 0 by
    # construction, but the three can jointly exceed the session's billed
    # total in pathological shapes (interrupts have no turn ids, so their
    # average-output charge can land on turns retry already billed in full).
    # Clamp the SUM to the session's own billed cost — the same rates over
    # all turns, further capped by Cursor's stamped invoice total when the
    # source provided one — scaling the components proportionally so their
    # relative attribution survives.
    session_total = sum(turn_costs.values())
    root = next((s for s in spans if s.parent_id is None), None)
    stamped = root.attributes.get("session.cost_usd") if root is not None else None
    if stamped is not None:
        session_total = min(session_total, float(stamped))
    marginal_sum = retry_usd + interrupt_usd + repeat_usd
    if marginal_sum > session_total:
        scale = (session_total / marginal_sum) if marginal_sum > 0 else 0.0
        retry_usd *= scale
        interrupt_usd *= scale
        repeat_usd *= scale

    return {
        "retry_waste_marginal_usd": round(max(0.0, retry_usd), 6),
        "interrupt_waste_marginal_usd": round(max(0.0, interrupt_usd), 6),
        "repeat_edit_waste_marginal_usd": round(max(0.0, repeat_usd), 6),
    }


def compute_session_waste(spans: list[SpanData], model_id: str | None = None) -> dict:
    """Session-level waste components, in two unit systems.

    Legacy fields (``*_waste_tokens``) charge ``event_count × avg context
    window`` - a SCORE-space heuristic. They are fine
    for the clamped efficiency score but must never be priced: the unit is
    the cache-inflated context window, so their sum can exceed the session's
    billed tokens many times over.

    Billed fields (``*_waste_billed_tokens``) are the priceable variants:
    the BILLED tokens (total_billed_prompt + completion) of the specific
    turns involved, categories disjoint by construction (retry wins over
    repeat when a turn shows both; interrupts — a session-level count with
    no turn ids — charge ``min(count, turn_count)`` average billed turns).
    Bounded by the session's own billed total, so any dollar figure derived
    from them is a true subset of spend. Still an UPPER bound though: a turn
    involved in waste isn't wholly waste, so billed fields
    over-attribute and are not meant for display.

    Marginal fields (``*_marginal_usd``) are the display numbers: what would
    not have been paid absent each waste event, priced per component off the
    unified rate card. See :func:`_marginal_waste_usd` for the exact
    semantics and approximations. ``model_id`` (optional) is the pricing
    model (typically the one ``total_cost_usd`` is priced with); when absent the root-span → last-genuine-model chain applies
    (identical to :func:`aggregate_session_spans`). All three are None when
    the model can't be priced, or when the session's parser stamped no
    per-turn billed tokens to price.

    ``retry_turn_count`` is the size of the engine's retry TURN set (the same
    disjoint set the billed/marginal retry figures charge — both calls of an
    error→retry pair land their turns in it). It is the span-derived
    instrumentation for "how much retrying happened here": always an int, and
    0 is a real measurement (the engine scanned the tool calls and found no
    retry pair) - unlike the ``error_retry_sequences`` field,
    which is None when never measured.

    Returns ``{interrupt_waste_tokens, repeat_waste_tokens,
    retry_waste_tokens, interrupt_waste_billed_tokens,
    repeat_waste_billed_tokens, retry_waste_billed_tokens,
    retry_waste_marginal_usd, interrupt_waste_marginal_usd,
    repeat_edit_waste_marginal_usd, retry_turn_count, floor_drift}``.
    Deterministic; safe on empty input.
    """
    growth = _context_growth(spans)
    calls = _tool_calls(spans)
    avg_prompt = sum(t["prompt_tokens"] for t in growth) / len(growth) if growth else 0.0

    # Billed tokens per turn, from the turn spans themselves.
    billed_by_turn: dict[int, int] = {}
    for s in spans:
        n = _turn_number(s.name)
        if n is None:
            continue
        billed_by_turn[n] = int(s.attributes.get("llm.token_count.total_billed_prompt") or 0) + int(
            s.attributes.get("llm.token_count.completion") or 0
        )

    retry_t = _retry_turns(calls)
    repeat_t = _repeat_edit_turns(calls) - retry_t  # disjoint: retry wins
    n_turns = len(billed_by_turn)
    avg_billed = (sum(billed_by_turn.values()) / n_turns) if n_turns else 0.0
    interrupts = min(_interrupt_count(spans), n_turns)

    return {
        "interrupt_waste_tokens": int(_interrupt_count(spans) * avg_prompt),
        "repeat_waste_tokens": int(repeated_edits_to_same_file(calls) * avg_prompt),
        "retry_waste_tokens": int(error_retry_sequences(calls) * avg_prompt),
        "interrupt_waste_billed_tokens": int(interrupts * avg_billed),
        "repeat_waste_billed_tokens": sum(billed_by_turn.get(t, 0) for t in repeat_t),
        "retry_waste_billed_tokens": sum(billed_by_turn.get(t, 0) for t in retry_t),
        **_marginal_waste_usd(spans, calls, retry_t, repeat_t, model_id),
        "retry_turn_count": len(retry_t),
        "floor_drift": floor_drift_score(growth),
    }


# ── token aggregation ────
def aggregate_session_spans(spans: list[SpanData]) -> dict:
    """Roll spans up into session token components + model + bounds.

    Returns ``{start_time, end_time, model_name, turn_count, input_tokens,
    input_fresh, cache_read_tokens, cache_write_tokens, cache_write_1h_tokens,
    output_tokens, session_cost_usd}``. Per-source quirks (Cursor session-wide
    override, cache split, etc.) are handled below.
    """
    if not spans:
        return {
            "start_time": None,
            "end_time": None,
            "model_name": None,
            "turn_count": 0,
            "input_tokens": 0,
            "input_fresh": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "cache_write_1h_tokens": 0,
            "output_tokens": 0,
            "session_cost_usd": None,
        }

    root = next((s for s in spans if s.parent_id is None), None)
    start_time = root.start_time if root else min(s.start_time for s in spans)
    end_time = root.end_time if root else max(s.end_time for s in spans)
    timestamps_observed = not any(
        s.attributes.get("session.timestamps_observed") is False for s in spans
    )
    if not timestamps_observed:
        start_time = end_time = None

    model_name = model_id_or_none(root.model_name) if root else None
    if not model_name:
        model_name = last_genuine_model_id(spans)

    turn_spans = [s for s in spans if s.name.startswith("Turn ")]
    turn_count = len(turn_spans)
    input_tokens = sum(s.prompt_tokens for s in turn_spans)
    output_tokens = sum(s.completion_tokens for s in turn_spans)
    cache_read_tokens = sum(
        int(s.attributes.get("llm.token_count.cache_read") or 0) for s in turn_spans
    )
    cache_write_tokens = sum(
        int(s.attributes.get("llm.token_count.cache_write") or 0) for s in turn_spans
    )
    cache_write_1h_tokens = sum(
        int(s.attributes.get("llm.token_count.cache_write_1h") or 0) for s in turn_spans
    )
    # Prefer the summed genuine fresh-input attribute. Fall back to input_tokens
    # ONLY when NO turn span carried the attribute at all (a parser that never
    # stamps it) — never when it is present but legitimately sums to 0 (a
    # cache-only session). Forcing input_tokens there would let a consumer that
    # rebuilds the billed total as `input_fresh + cache_read + cache_write` add
    # the cached tokens a second time and overstate cost.
    _has_fresh_attr = any("llm.token_count.input_fresh" in s.attributes for s in turn_spans)
    input_fresh = sum(int(s.attributes.get("llm.token_count.input_fresh") or 0) for s in turn_spans)
    if not _has_fresh_attr:
        input_fresh = input_tokens

    session_cost_usd: float | None = None
    if root is not None:
        session_prompt = root.attributes.get("session.token_count.prompt")
        if session_prompt is not None:
            input_tokens = int(session_prompt)
            output_tokens = int(root.attributes.get("session.token_count.completion") or 0)
            cache_read_tokens = int(root.attributes.get("session.token_count.cache_read") or 0)
            cache_write_tokens = int(root.attributes.get("session.token_count.cache_write") or 0)
            cache_write_1h_tokens = 0
            input_fresh = max(input_tokens - cache_read_tokens - cache_write_tokens, 0)
        session_cost = root.attributes.get("session.cost_usd")
        if session_cost is not None:
            session_cost_usd = float(session_cost)

    return {
        "start_time": start_time,
        "end_time": end_time,
        "model_name": model_name,
        "turn_count": turn_count,
        "input_tokens": input_tokens,
        "input_fresh": input_fresh,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "cache_write_1h_tokens": cache_write_1h_tokens,
        "output_tokens": output_tokens,
        "session_cost_usd": session_cost_usd,
        **({"timestamps_observed": False} if not timestamps_observed else {}),
    }


# ── derived behaviour metrics ────
# The read/edit ratios use the read-first taxonomy (``RF_*`` in
# ``metrics.py``). A denominator of zero returns ``None``, never a
# fabricated 0.
_RF_MUTATION_TOOLS: frozenset[str] = RF_EDIT_TOOLS | RF_WRITE_TOOLS
_EFFICIENCY_MIN_TOKENS = 500  # below this a session is too small to score
_FLOOR_DRIFT_PENALTY = 0.3


def read_edit_ratio(tool_calls: list[_ToolCall]) -> float | None:
    """Reads / Edits — higher = more research before modifying. None if no edits."""
    reads = sum(1 for tc in tool_calls if tc.tool_name in RF_READ_TOOLS)
    edits = sum(1 for tc in tool_calls if tc.tool_name in RF_EDIT_TOOLS)
    return round(reads / edits, 2) if edits else None


def edits_without_read_pct(tool_calls: list[_ToolCall]) -> float | None:
    """Percent of Edit calls on a file not Read earlier in the session. None if no edits."""
    edits = sum(1 for tc in tool_calls if tc.tool_name in RF_EDIT_TOOLS)
    if not edits:
        return None
    read_files: set[str] = set()
    blind = 0
    for tc in tool_calls:
        fp = _file_path(tc.input_value)
        if not fp:
            continue
        if tc.tool_name in RF_READ_TOOLS:
            read_files.add(fp)
        elif tc.tool_name in RF_EDIT_TOOLS and fp not in read_files:
            blind += 1
    return round(blind / edits * 100, 1)


def research_mutation_ratio(tool_calls: list[_ToolCall]) -> float | None:
    """Reads / mutations (Edit+Write). None if no mutations."""
    research = sum(1 for tc in tool_calls if tc.tool_name in RF_READ_TOOLS)
    mutations = sum(1 for tc in tool_calls if tc.tool_name in _RF_MUTATION_TOOLS)
    return round(research / mutations, 2) if mutations else None


def write_vs_edit_pct(tool_calls: list[_ToolCall]) -> float | None:
    """Write (full-file rewrite) as percent of all mutations. None if no mutations."""
    writes = sum(1 for tc in tool_calls if tc.tool_name in RF_WRITE_TOOLS)
    mutations = sum(1 for tc in tool_calls if tc.tool_name in _RF_MUTATION_TOOLS)
    return round(writes / mutations * 100, 1) if mutations else None


def error_rate(tool_calls: list[_ToolCall]) -> float | None:
    """Percent of tool calls that failed. None when the session ran no tools."""
    known = [tc for tc in tool_calls if tc.success is not None]
    if not known:
        return None  # nothing ran, or nothing whose outcome was observed
    failed = sum(1 for tc in known if tc.success is False)
    return round(failed / len(known) * 100, 1)


def edit_line_totals(tool_calls: list[_ToolCall]) -> tuple[int | None, int | None]:
    """Session-wide ``(lines_added, lines_removed)`` from the edit tool payloads.

    The AI-lines-of-code signal, counted locally - see
    :mod:`agentplay._core.ingestion.coding_agent.edit_lines` for why.

    Only SUCCESSFUL calls count: a failed ``Edit`` changed no lines, the same
    reason ``metrics.py`` gates ``useful_edit_count`` on success.

    Args:
        tool_calls: The session's chronological tool calls.

    Returns:
        ``(added, removed)``, or ``(None, None)`` when not one call carried a
        countable payload - an unmeasured session, which must stay None rather
        than land as a real-looking 0 (the contract
        :func:`session_derived_metrics` documents).
    """
    added = removed = 0
    measured = False
    for call in tool_calls:
        if call.success is not True:  # failed OR never observed: no lines landed
            continue
        counts = edit_line_counts(call.tool_name, call.input_value)
        if counts is None:
            continue
        added += counts[0]
        removed += counts[1]
        measured = True
    return (added, removed) if measured else (None, None)


def efficiency_score(
    total_tokens: int,
    compaction_waste: int,
    interrupt_waste: int,
    repeat_waste: int,
    retry_waste: int,
    floor_drift: float | None,
) -> float | None:
    """Composite 0-100 token-efficiency score (higher = less waste).

    ``100 * (1 - waste/tokens) * (1 - 0.3 * floor_drift)`` where
    ``waste = compaction + interrupt + repeat + retry``, clamped so waste never
    exceeds tokens (score floors at 0).

    The caller supplies ``total_tokens`` and ``compaction_waste`` from the
    **per-task** rollup (``Σ prompt+completion`` and ``Σ estimated_waste_tokens``
    over the session's segmented tasks), NOT the cache-inflated session token
    sum or a whole-session compaction estimate.
    That is why this lives one layer up from :func:`session_derived_metrics`
    (which has no task data). ``None`` for sessions under 500 tokens.
    """
    if total_tokens < _EFFICIENCY_MIN_TOKENS:
        return None
    total_waste = compaction_waste + interrupt_waste + repeat_waste + retry_waste
    waste_ratio = min(total_waste / total_tokens, 1.0)  # clamp: waste ≤ tokens
    score = 100 * (1 - waste_ratio)
    if floor_drift is not None:
        score *= 1 - _FLOOR_DRIFT_PENALTY * floor_drift
    return round(max(0.0, score), 1)


def skill_distribution(spans: list[SpanData]) -> dict[str, int]:
    """``{skill_name: count}`` from the session's ``skills_used`` signal, garbage dropped.

    Normalize each recorded skill path, drop entries that fail validation or carry a
    non-positive count, and sum by normalized name. Empty for parsers that emit
    no session signals (Codex / Copilot) or a session that loaded no skills.
    """
    root = next((s for s in spans if s.parent_id is None), None)
    if root is None:
        return {}
    raw = root.attributes.get("harness.session_signals")
    if not raw:
        return {}
    try:
        sig = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return {}
    if not isinstance(sig, dict):
        return {}
    entries = sig.get("skills_used") or []
    if not isinstance(entries, list):
        return {}
    out: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            count = int(entry.get("count", 0) or 0)
        except (ValueError, TypeError):
            continue
        if count <= 0:
            continue
        path = entry.get("path")
        name = normalize_skill_name(str(path or ""))
        if not path or name is None:
            continue
        out[name] = out.get(name, 0) + count
    return out


def session_derived_metrics(spans: list[SpanData]) -> dict[str, Any]:
    """Span-only derived session metrics.

    The read/edit behaviour ratios, error rate, tool-calls-per-turn, the
    tool / skill distributions, and the added/removed line counts of a session
    summary (behaviour, tool errors, tool and skill usage, lines changed).
    Computed straight from the spans.

    ``efficiency_score`` is deliberately NOT here: it is scored from the
    **per-task** token + waste rollup (see :func:`efficiency_score`), which only
    exists after segmentation - the caller computes it.
    Only non-null / non-empty entries are returned: a missing signal (no
    edits, no skills) is omitted rather than a fabricated 0.
    """
    calls = _tool_calls(spans)
    turn_count = len(_context_growth(spans))  # one row per "Turn N" span == turns

    out: dict[str, Any] = {}

    def _put(key: str, value: Any) -> None:
        if value is not None:
            out[key] = value

    _put("read_edit_ratio", read_edit_ratio(calls))
    _put("edits_without_read_pct", edits_without_read_pct(calls))
    _put("research_mutation_ratio", research_mutation_ratio(calls))
    _put("write_vs_edit_pct", write_vs_edit_pct(calls))
    _put("error_rate", error_rate(calls))
    # These two are computed right here in this module and feed the waste
    # rollup, so they are emitted here too; leaving them off
    # made retry rate and interrupts per session read as missing while token
    # waste, fed by the same numbers, was present.
    #
    # Both keep the null-vs-zero contract this function documents: retries only
    # when there were tool calls to retry, interrupts only when the harness
    # actually reported signals. A fabricated 0 would read as a healthy green
    # score, which is strictly worse than an honest dash.
    if calls:
        _put("error_retry_sequences", error_retry_sequences(calls))
    _put("interrupt_count", interrupt_count_measured(spans))
    # AI lines of code. Same null-vs-zero contract: a session whose edit payloads
    # never parsed leaves both fields None, so a reader shows a dash rather
    # than claiming the agent wrote nothing.
    lines_added, lines_removed = edit_line_totals(calls)
    _put("lines_added", lines_added)
    _put("lines_removed", lines_removed)
    if turn_count and calls:
        out["tool_calls_per_turn"] = round(len(calls) / turn_count, 1)
    tool_dist = session_tool_distribution(spans)
    if tool_dist:
        out["tool_distribution"] = tool_dist
    skill_dist = skill_distribution(spans)
    if skill_dist:
        out["skill_distribution"] = skill_dist
    return out


# ── public entry ─────────────────────────────────────────
def session_model_usage(spans: list[SpanData]) -> list[dict[str, Any]] | None:
    """Per-model billed token totals when the parser supplies request evidence.

    Match every billed component against the independent turn aggregates before
    publishing a complete summary. Never turn a partial request list into a
    session price. Other parsers retain their existing estimate path.
    """
    turns = [s for s in spans if s.name.startswith("Turn ")]
    if not turns or not any("llm.cost_events" in s.attributes for s in turns):
        return None
    if not all("llm.cost_events" in s.attributes for s in turns):
        raise ValueError("Request evidence is missing for part of the session")
    if not any(s.attributes.get("llm.cost_events") for s in turns):
        # Every turn carries the key but not one request row. Two cases:
        # the turns billed NOTHING (rate-limit-only / valid zero usage) --
        # observed zero, reconciled to [] below -- or the parser saw usage
        # it could not place (no timestamp) and kept only the turn totals:
        # absent evidence, so callers keep the aggregate estimate instead
        # of a mismatch error.
        seen = aggregate_session_spans(spans)
        if any(
            seen[k]
            for k in (
                "input_fresh",
                "cache_read_tokens",
                "cache_write_tokens",
                "cache_write_1h_tokens",
                "output_tokens",
            )
        ):
            return None
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    fields = (
        "input_tokens",
        "cache_read_tokens",
        "cache_write_5m_tokens",
        "cache_write_1h_tokens",
        "output_tokens",
    )
    for turn in turns:
        for event in turn.attributes["llm.cost_events"]:
            model = str(event.get("model") or "")
            raw_speed = event.get("speed")
            speed = "" if raw_speed is None else str(raw_speed)
            row = grouped.setdefault(
                (model, speed),
                {
                    "model": model,
                    "requests": 0,
                    **dict.fromkeys(fields, 0),
                    **({"speed": speed} if speed else {}),
                },
            )
            if "cache_ttl_known" in event:
                row["cache_ttl_known"] = (
                    row.get("cache_ttl_known", True) and event["cache_ttl_known"]
                )
            row["requests"] += 1
            for field in fields:
                value = event.get(field, 0)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"Invalid request token component: {field}")
                row[field] += value
    agg = aggregate_session_spans(spans)
    totals = {field: sum(row[field] for row in grouped.values()) for field in fields}
    if (
        totals["input_tokens"] != agg["input_fresh"]
        or totals["cache_read_tokens"] != agg["cache_read_tokens"]
        or totals["cache_write_5m_tokens"] + totals["cache_write_1h_tokens"]
        != agg["cache_write_tokens"]
        or totals["cache_write_1h_tokens"] != agg["cache_write_1h_tokens"]
        or totals["output_tokens"] != agg["output_tokens"]
    ):
        raise ValueError("Request evidence does not cover the session's billed tokens")
    return [grouped[model] for model in sorted(grouped)]


def build_session_metrics(spans: list[SpanData]) -> dict[str, Any]:
    """Session-level metrics dict.

    Combines :func:`aggregate_session_spans`, :func:`compute_session_waste`,
    :func:`detect_automated`, and :func:`session_derived_metrics` (the behaviour
    ratios + error rate + tool-calls-per-turn + tool & skill distributions). Omits
    ``total_cost_usd`` (a consumer can recompute cost from tokens +
    model) — but DOES emit ``billed_cost_usd``: the vendor's own stamped invoice
    figure (Cursor's analytics API → root-span ``session.cost_usd``), a
    different provenance a consumer may prefer. OMITTED - not null - when no
    stamp exists: a present null could be read as an explicit clear, and
    omission is this function's convention for signal-less keys anyway.

    ``efficiency_score`` is NOT emitted here. It needs the per-task token+waste
    rollup that only exists after segmentation, so the caller scores it with
    :func:`efficiency_score` and adds it to this dict. See :func:`session_derived_metrics`.

    Returns:
        A dict of ``{model, billed_cost_usd, input_tokens, input_fresh, output_tokens,
        cache_read_tokens, cache_write_tokens, cache_write_1h_tokens, cached_tokens,
        interrupt_waste_tokens,
        repeat_waste_tokens, retry_waste_tokens, interrupt_waste_billed_tokens,
        repeat_waste_billed_tokens, retry_waste_billed_tokens, retry_turn_count,
        floor_drift, is_automated}``
        plus, when computable, ``{read_edit_ratio, edits_without_read_pct,
        research_mutation_ratio, write_vs_edit_pct, error_rate,
        tool_calls_per_turn, tool_distribution, skill_distribution,
        lines_added, lines_removed}`` — the
        derived keys are omitted (not NULL-valued) when a session lacks the
        signal to compute them. ``efficiency_score`` is added by the caller, not
        this function.
    """
    agg = aggregate_session_spans(spans)
    model_usage = session_model_usage(spans)
    # Same pricing model the session's own cost uses (root → last genuine).
    waste = compute_session_waste(spans, model_id=agg["model_name"])
    out: dict[str, Any] = {
        "timestamps_observed": agg.get("timestamps_observed") is not False,
        "model": agg["model_name"],
        "input_tokens": agg["input_tokens"],
        # The genuinely-fresh (non-cached) input, SUM-flavoured across turns.
        # A session is priced off ``input_fresh + cache_read + cache_write``
        # (the billed sum), so this is emitted to let a consumer price off the
        # SAME cache-inclusive total instead of
        # ``input_tokens`` (the per-turn context-window MAX summed), which on
        # multi-turn sessions falls below the cumulative cache totals and clamps
        # the priced fresh input to 0.
        "input_fresh": agg["input_fresh"],
        "output_tokens": agg["output_tokens"],
        "cache_read_tokens": agg["cache_read_tokens"],
        "cache_write_tokens": agg["cache_write_tokens"],
        # The 1h-TTL write subset — always emitted (0 when none) so a consumer
        # can price 1h cache writes at 2x and any re-computed value stays
        # cache-write-1h-aware for every session.
        "cache_write_1h_tokens": agg["cache_write_1h_tokens"],
        "cached_tokens": agg["cache_read_tokens"] + agg["cache_write_tokens"],
        "interrupt_waste_tokens": waste["interrupt_waste_tokens"],
        "repeat_waste_tokens": waste["repeat_waste_tokens"],
        "retry_waste_tokens": waste["retry_waste_tokens"],
        # Billed-unit waste — the PRICEABLE fields (see compute_session_waste).
        # Emitted so no consumer has to do dollar math on the score-space trio
        # above, which inflates reclaimable spend.
        "interrupt_waste_billed_tokens": waste["interrupt_waste_billed_tokens"],
        "repeat_waste_billed_tokens": waste["repeat_waste_billed_tokens"],
        "retry_waste_billed_tokens": waste["retry_waste_billed_tokens"],
        # Span-derived retry instrumentation — always emitted: unlike the
        # pricing-gated marginal trio below, a 0 here is a real measurement
        # (the engine scanned the calls and found no error→retry pair), the
        # honest replacement for `error_retry_sequences`, which is None
        # when never measured.
        "retry_turn_count": waste["retry_turn_count"],
        "is_automated": detect_automated(spans),
    }
    if model_usage is not None:
        out["model_usage"] = model_usage
    root = next((s for s in spans if s.parent_id is None), None)
    if root and "llm.cost_summary" in root.attributes:
        out["claude_cost_summary"] = root.attributes["llm.cost_summary"]
        checkpoints = out["claude_cost_summary"]["checkpoints"]
        if out["claude_cost_summary"].get("invalid_checkpoints") or any(c["has_unknown_model_cost"] or any(c["visible_unreported_tokens"]) or any(r["reported_tokens"] != r["visible_tokens"] for r in c["models"]) for c in checkpoints):
            for row in out.get("model_usage", []):
                row["session_coverage_complete"] = False
    # Vendor-stamped invoice figure (see docstring): present only when a stamp
    # exists, so a session without a stamp never reports a billed cost of its own.
    if agg["session_cost_usd"] is not None:
        out["billed_cost_usd"] = agg["session_cost_usd"]
    # Marginal-dollar waste — the DISPLAY numbers (the billed trio is an
    # upper bound; see _marginal_waste_usd). Emitted whenever the session's
    # model prices off the rate card (0.0 = genuinely no waste); omitted —
    # like floor_drift — when the model is unpriceable, so an unknown rate
    # never lands as a fabricated $0.
    for key in (
        "retry_waste_marginal_usd",
        "interrupt_waste_marginal_usd",
        "repeat_edit_waste_marginal_usd",
    ):
        if waste[key] is not None:
            out[key] = waste[key]
    if waste["floor_drift"] is not None:
        out["floor_drift"] = waste["floor_drift"]
    out.update(session_derived_metrics(spans))
    if root and root.attributes.get("llm.usage_complete") is False:
        out["usage_complete"] = False
        out["usage_state"] = root.attributes["llm.usage_state"]
        if "llm.reported_total_tokens" in root.attributes:
            out["reported_total_tokens"] = root.attributes["llm.reported_total_tokens"]
        # Known request components remain lower bounds. Neither the session
        # nor counterfactual savings can be priced as a complete observation.
        for key in ("retry_waste_marginal_usd", "interrupt_waste_marginal_usd",
                    "repeat_edit_waste_marginal_usd", "floor_drift"):
            out.pop(key, None)
    return out


__all__ = [
    "aggregate_session_spans",
    "build_session_metrics",
    "compute_session_waste",
    "detect_automated",
    "edits_without_read_pct",
    "efficiency_score",
    "error_rate",
    "error_retry_sequences",
    "floor_drift_score",
    "interrupt_count_measured",
    "read_edit_ratio",
    "repeated_edits_to_same_file",
    "research_mutation_ratio",
    "session_derived_metrics",
    "skill_distribution",
    "write_vs_edit_pct",
]
