"""Local filesystem source — walks ~/.claude / ~/.codex / ~/.gemini / Copilot dirs.

Indexes JSONL session files under per-agent homes, detects the agent
from the file head, and emits the universal `SpanData` shape via the
matching parser. Multi-agent by design — drop a Codex session next to
a Claude Code session in the same home and the index picks both up.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from typing import Any

from agentplay._core.ingestion.coding_agent._paths import to_posix
from agentplay._core.ingestion.coding_agent._sources._base import (
    MAX_SESSION_BYTES,
    OversizedSessionError,
)
from agentplay.discover import MAX_SOURCE_BYTES
from agentplay._core.ingestion.coding_agent._sources.local.antigravity_index import (
    TRANSCRIPT_GLOB as ANTIGRAVITY_TRANSCRIPT_GLOB,
    index_antigravity_home,
)
from agentplay._core.ingestion.coding_agent._sources.local.parsers import (
    AgentType,
    detect_agent,
    parse_claude_code,
    parse_codex,
    parse_copilot,
    parse_copilot_vscode,
    parse_cursor_agent_transcript,
    parse_cursor_store,
    parse_antigravity,
    parse_gemini,
    parse_kiro,
    parse_opencode,
)
from agentplay._core.ingestion.coding_agent._sources.local.parsers.gemini import (
    _user_prompt as _gemini_user_prompt,
)
from agentplay._core.ingestion.coding_agent.spans import DiscoveredSession, SpanData

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class ManifestIdentity:
    """The identity block from an optional identity stamp file under
    `<home>/_manifest/`. Used to detect transcripts that belong to more
    than one owner.

    Every field is optional because the stamp may carry only partial
    data; consumers degrade gracefully when a field is absent.
    """

    user_uuid: str | None
    org_uuid: str | None
    org_name: str | None
    email: str | None
    display_name: str | None
    github_username: str | None

    @classmethod
    def from_manifest_block(cls, block: dict | None) -> ManifestIdentity | None:
        """Parse the identity block. Empty / shape-wrong → None."""
        if not isinstance(block, dict):
            return None
        identity = cls(
            user_uuid=_str_or_none(block.get("user_uuid")),
            org_uuid=_str_or_none(block.get("org_uuid")),
            org_name=_str_or_none(block.get("org_name")),
            email=_str_or_none(block.get("email")),
            display_name=_str_or_none(block.get("name")),
            github_username=_str_or_none(block.get("github_username")),
        )
        # All-None means nothing to attribute to — treat as absent.
        if not any(
            (
                identity.user_uuid,
                identity.email,
                identity.github_username,
                identity.display_name,
            )
        ):
            return None
        return identity


def _index_put(
    index: dict[str, _FileRef],
    session_id: str,
    ref: _FileRef,
    newest: dict[str, datetime],
) -> None:
    """Insert ``ref`` under ``session_id``, keeping the richer file on a collision.

    One session legitimately appears under two copy prefixes. A copied transcript tree
    may include a hostname directory, and macOS renames a machine whenever
    the network changes (``Foo.local`` -> ``Foo.lan``, or a lowercase
    ``mac`` fallback), so the same transcript uuid lands under two hostnames and
    both get walked into this index.

    A plain ``index[session_id] = ref`` would let the home visited LAST win,
    and since the walker is fed ``sorted(...)``, the winner would be decided by
    hostname spelling. A stale copy could then beat a fresher, larger one, and
    because the stale copy's content hash never changes, every later scan would
    skip the session as "unchanged".

    Transcripts are append-only, so byte SIZE is the honest "more complete"
    signal; ``mtime`` only breaks a size tie, because a re-copy (redaction, a
    new hostname prefix) rewrites mtime without adding content.

    Args:
        index: The ``session_id -> _FileRef`` map being built; mutated in place.
        session_id: Namespaced id, e.g. ``local:claude-code:<uuid>``.
        ref: Candidate pointer for this session.
        newest: ``session_id -> newest mtime seen`` across ALL candidates.
            Recorded here, applied to the winner only after the whole walk by
            :func:`_carry_collision_mtimes`. It must NOT be folded into the
            winner during selection: with three copies, a smaller-but-newer
            loser would raise the incumbent's mtime, and the next equal-sized
            candidate would then be compared against that synthetic timestamp
            and could lose despite being genuinely fresher.

    Side effects:
        Logs a warning naming both paths whenever a collision is resolved, so a
        duplicated copy prefix is visible rather than silent.
    """
    incumbent = index.get(session_id)

    def _is_narrative(candidate: _FileRef) -> bool:
        return candidate.subformat == "agent_transcript"

    # Recency bookkeeping, decided BEFORE the winner is picked because the two
    # answers differ.
    #
    # `_carry_collision_mtimes` gives each winner the newest mtime seen across
    # its duplicates, which is right when the files are two copies of one thing
    # (a re-copy really is fresher). It is wrong for a Cursor narrative/store
    # pair: only the store cache is stamped with the session's real activity
    # time, so folding in a narrative's filesystem clock re-dates an old session
    # as current and drags the newest-seen timestamp forward.
    #
    # So a narrative contributes nothing once a store is present, and the first
    # store DISPLACES whatever narratives contributed. Between two stores the
    # normal max applies, which is what keeps the newer of two duplicate store
    # copies.
    previous = newest.get(session_id)
    if incumbent is not None and _is_narrative(ref) and not _is_narrative(incumbent):
        pass  # narrative alongside a store: contributes no recency
    elif previous is None or (
        not _is_narrative(ref) and incumbent is not None and _is_narrative(incumbent)
    ):
        # First ref, or the first store to arrive after narratives — displace.
        newest[session_id] = ref.mtime
    else:
        newest[session_id] = max(previous, ref.mtime)

    if incumbent is None:
        index[session_id] = ref
        return

    def _size(candidate: _FileRef) -> int:
        try:
            return candidate.path.stat().st_size
        except OSError:
            return -1

    new_size, old_size = _size(ref), _size(incumbent)

    # Format DOMINANCE outranks size, for the one case where the two files are
    # not two copies of the same thing.
    #
    # cursor-agent persists each session twice: the `agent-transcripts` narrative
    # (subformat "agent_transcript") and the store.db NDJSON (subformat None).
    # They share a session id, so they collide here. The store shape strictly
    # contains the narrative one — it adds tool RESULTS, billed tokens, the model
    # and real timestamps, all of which the narrative omits entirely. Size cannot
    # express that: a chatty narrative log is often the LARGER file, so the
    # size rule would keep the copy with no tool output and silently undo the
    # whole point of materializing the store (no commit SHAs, no produced edges,
    # no tokens). Prefer the store whenever both exist; the narrative still wins
    # uncontested when no store cache was materialized for that session.
    if _is_narrative(ref) != _is_narrative(incumbent):
        richer, poorer = (incumbent, ref) if _is_narrative(ref) else (ref, incumbent)
        index[session_id] = richer
        # Recency was already settled above, where a narrative contributes
        # nothing and the max between two stores is preserved. Re-pinning it here
        # would discard the newer of two duplicate store copies.
        logger.info(
            "session %s found as both a Cursor narrative and a store transcript; "
            "keeping the store copy (%s) over the narrative (%s) — the narrative "
            "carries no tool results, tokens or model",
            session_id,
            richer.path,
            poorer.path,
        )
        return

    if (new_size, ref.mtime) > (old_size, incumbent.mtime):
        winner, loser, winner_size, loser_size = ref, incumbent, new_size, old_size
        index[session_id] = ref
    else:
        winner, loser, winner_size, loser_size = incumbent, ref, old_size, new_size

    # Severity follows whether anything was actually at risk. A duplicate
    # prefix is overwhelmingly benign: nearly all overlaps are byte-identical
    # re-copies under a renamed host. Warning on every one of those, every run,
    # is noise that trains readers to skim past the ones that matter.
    # Diverging sizes mean a real shadow (a fresher copy was
    # being hidden), which is the only case worth a warning.
    if winner_size != loser_size:
        logger.warning(
            "session %s found under more than one path with DIFFERING sizes; "
            "keeping the larger copy (%d bytes at %s) over %d bytes at %s",
            session_id,
            winner_size,
            winner.path,
            loser_size,
            loser.path,
        )
    else:
        logger.debug(
            "session %s found under more than one path, identical size (%d bytes); keeping %s",
            session_id,
            winner_size,
            winner.path,
        )


def _carry_collision_mtimes(index: dict[str, _FileRef], newest: dict[str, datetime]) -> None:
    """Give each collided winner the newest mtime seen across its duplicates.

    Time-window filtering compares the ref's mtime, so a larger-but-older copy
    would be selected and then filtered out before parsing, discarding the
    richer bytes. Size decides WHICH FILE to read; recency decides WHETHER
    the session is inside the requested window, and the session was
    demonstrably touched at the latest of its copies' times.

    Applied after the walk rather than during it, so a loser's timestamp can
    never contaminate the comparison key a later candidate is judged against.

    Args:
        index: Finished ``session_id -> _FileRef`` map; winners mutated in place.
        newest: ``session_id -> newest mtime``, as recorded by :func:`_index_put`.
    """
    for session_id, ts in newest.items():
        ref = index.get(session_id)
        if ref is not None and ts > ref.mtime:
            ref.mtime = ts


def _read_head(
    path: Path,
    *,
    min_lines: int = 20,
    max_bytes: int = 5_000_000,
    stop_when: Callable[[bytes], bool] | None = None,
) -> bytes:
    """Return the head of a JSONL file for cheap metadata sniffing — agent
    detection, cwd, branch.

    Reads whole lines, not a fixed byte slab, so a giant opening record can't
    hide the identifying keys behind a mid-JSON truncation. Newer Claude Code
    builds front-load a multi-MB ``queue-operation`` line (the user's pasted
    context); a fixed 32KB read would cut it mid-object, ``json.loads`` would
    fail on every line, and ``detect_agent`` would return None for a valid
    session. Complete-line reads avoid that.

    ``min_lines`` alone is not a safe stopping rule for agent detection. A line
    budget assumes the identifying record sits near the top, and transcripts
    can open with dozens of metadata records (``queue-operation`` /
    ``file-history-snapshot`` / ``attachment``) before the first ``user`` line,
    so a 64KB chunk can satisfy a line budget well before the marker appears.
    Detection would then see no marker and skip the session. Callers that are
    looking for something specific should say so with ``stop_when`` rather
    than trusting a line count to be generous enough.

    Cost: with ``stop_when`` the read continues past ``min_lines`` until the
    predicate is satisfied or ``max_bytes`` is reached, so a file that never
    satisfies it costs one ``max_bytes`` read instead of one chunk. That is the
    deliberate trade: the alternative is silently discarding a valid session.

    Args:
        path: the JSONL transcript.
        min_lines: keep reading until the buffer holds this many complete lines.
        max_bytes: hard ceiling so a pathological single line can't run away.
        stop_when: optional predicate over the buffer so far. When supplied,
            reading continues past ``min_lines`` until it returns True. Checked
            only after ``min_lines`` is met, so the common case still stops at
            the first chunk.

    Returns:
        The head bytes (may hold fewer than ``min_lines`` lines for short files).
    """
    buf = bytearray()
    newline_count = 0
    # Bytes already offered to ``stop_when``, always on a line boundary. The
    # predicate sees each complete line exactly once: handing it the whole
    # accumulated buffer every chunk is O(n²) on a multi-MB JSONL, the same
    # quadratic trap the newline counter below avoids, one level up.
    scanned_to = 0
    # Absolute offset just past the newest complete line. Derived from each
    # chunk as it arrives: ``buf.rfind`` would rescan the whole accumulated
    # buffer whenever it holds no newline, which is exactly the multi-MB
    # single-line opener this function was written for — O(n²) again, by a
    # different route.
    line_end = 0
    satisfied = stop_when is None
    with open(path, "rb") as fh:
        # Count newlines incrementally per chunk — rescanning the whole buffer
        # each iteration (buf.count) is O(n²) on a multi-MB
        # single-line opener.
        while len(buf) < max_bytes:
            if newline_count >= min_lines and satisfied:
                break
            chunk = fh.read(65536)
            if not chunk:
                break
            newline_count += chunk.count(b"\n")
            chunk_start = len(buf)
            buf += chunk
            last_nl = chunk.rfind(b"\n")
            if last_nl != -1:
                line_end = chunk_start + last_nl + 1
            if not satisfied and line_end > scanned_to:
                # Only whole lines: a record split across the chunk boundary is
                # left for the next pass rather than parsed half-formed.
                if stop_when(bytes(buf[scanned_to:line_end])):
                    satisfied = True
                scanned_to = line_end
    return bytes(buf)


def _agent_detected(buf: bytes) -> bool:
    """``_read_head`` stop predicate for callers that need an agent marker.

    Detection is the reason the head is being read at all, so it is also the
    only honest place to stop reading. A line count cannot know whether the
    identifying record has been seen yet.
    """
    return detect_agent(buf) is not None


# Record types that carry no conversation: Claude Code session metadata and the
# Cursor end-of-turn marker. A file made only of these has nothing to map.
_NON_CONVERSATION_TYPES = frozenset(
    {
        "agent-name",
        "ai-title",
        "attachment",
        "cost-state",
        "file-history-snapshot",
        "last-prompt",
        "mode",
        "permission-mode",
        "queue-operation",
        "summary",
        "system",
        "turn_ended",
    }
)


def _is_metadata_only(head: bytes) -> bool:
    """True when every record in ``head`` is a known non-conversation type.

    Deliberately narrow: an unknown ``type``, or a head with no parseable
    record at all, is not metadata-only, so real format drift still reaches the
    "agent detection failed" warning instead of being filed as empty.
    """
    parsed = False
    for line in head.split(b"\n"):
        try:
            rec = json.loads(line)
        except ValueError:  # blank, truncated, or split mid-character
            continue
        if not isinstance(rec, dict):
            continue
        kind = rec.get("type")
        if not isinstance(kind, str) or kind not in _NON_CONVERSATION_TYPES:
            return False
        parsed = True
    return parsed


def _str_or_none(v: object) -> str | None:
    if isinstance(v, str):
        s = v.strip()
        return s or None
    return None


def _manifest_files(home: Path) -> list[Path]:
    """`<home>/_manifest/*.json`, oldest → newest.

    Ordered by filename - manifests are named with a UTC timestamp
    (`20260702T101500Z.json`), so lexicographic order IS chronological
    order and survives mtime-mangling copies.
    """
    manifest_dir = home / "_manifest"
    if not manifest_dir.is_dir():
        return []
    try:
        return sorted(p for p in manifest_dir.iterdir() if p.is_file() and p.suffix == ".json")
    except OSError:
        return []


def _all_manifest_bodies(home: Path) -> list[dict]:
    """Every parseable manifest body in the home, oldest → newest.

    Each manifest describes only one batch's sessions - so a fact stamped
    for an old session (its cwd→repo, its repo roots) exists only in an old
    manifest. Readers that need such facts (the repo loaders) must merge
    across all of them, newest-wins per key; reading only the newest
    manifest forgets every prior batch's stamps. Unreadable files
    are skipped (logged), not fatal.
    """
    bodies: list[dict] = []
    for path in _manifest_files(home):
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("manifest at %s unreadable; skipping it", path)
            continue
        if isinstance(body, dict):
            bodies.append(body)
    return bodies


def _newest_manifest_body(home: Path) -> dict | None:
    """Parse the newest `<home>/_manifest/*.json`; None if absent/unreadable.

    For stamps where only the LATEST value is meaningful (the identity
    stamp). Facts that accumulate across manifests (cwd_repos,
    repo_roots) go through `_all_manifest_bodies` instead.
    """
    files = _manifest_files(home)
    if not files:
        return None
    newest = files[-1]
    try:
        body = json.loads(newest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("manifest at %s unreadable; scan falls back to other signals", newest)
        return None
    return body if isinstance(body, dict) else None


def _load_manifest_identity(home: Path) -> ManifestIdentity | None:
    """Verified user/org identity from the newest manifest's identity block.

    Missing dir / unreadable JSON / absent block → None (callers treat the
    home as having no identity).
    """
    body = _newest_manifest_body(home)
    if body is None:
        return None
    return ManifestIdentity.from_manifest_block(body.get("entelligence"))


def _load_manifest_repos(home: Path) -> dict[str, str]:
    """Read the manifest's `cwd_repos` map → `{cwd: 'owner/repo'}`.

    Stamped into the manifest, which resolves `git remote` per cwd
    where the repo is live — the only place the owner is knowable for
    copied transcripts. Accepts both the flat `{cwd: 'owner/repo'}` and
    the richer `{cwd: {'repo': ..., 'base': ...}}` shapes. Empty dict
    when no manifest carries the stamp — the repo resolver then falls
    through to its other tiers.

    Merged across ALL the home's manifests (newest wins per cwd): each
    manifest stamps only its own batch's cwds, so an old session's mapping
    lives only in an old manifest - newest-only reading would lose it.
    """
    out: dict[str, str] = {}
    for body in _all_manifest_bodies(home):
        raw = body.get("cwd_repos")
        if not isinstance(raw, dict):
            continue
        for cwd, val in raw.items():
            if isinstance(val, str):
                out[cwd] = val
            elif isinstance(val, dict) and isinstance(val.get("repo"), str):
                out[cwd] = val["repo"]
    return out


def _load_manifest_repo_roots(home: Path) -> dict[str, str]:
    """`{git_toplevel: 'owner/repo'}` for every repo the home's sessions touched.

    Primary source is the manifest's `repo_roots` map - the git toplevel
    of each repo the sessions' TOOL CALLS touched
    (not just their cwds), so cross-repo edits resolve too. For manifests
    predating that stamp, roots are derived from `cwd_repos`: the rich
    shape's `base` IS a git toplevel, and a flat entry's cwd approximates
    one — partial coverage beats none, and `repo_roots` entries overwrite
    the derived ones when both exist.

    Trailing slashes are stripped so prefix matching is uniform.

    Merged across ALL the home's manifests (newest wins per root, and a
    `repo_roots` stamp beats a cwd-derived fallback within each body):
    each manifest stamps only its own batch's roots, so older sessions'
    roots survive later partial batches.
    """
    out: dict[str, str] = {}
    for body in _all_manifest_bodies(home):
        raw_cwds = body.get("cwd_repos")
        if isinstance(raw_cwds, dict):
            for cwd, val in raw_cwds.items():
                if not isinstance(cwd, str):
                    continue
                if isinstance(val, str):
                    out[cwd.rstrip("/")] = val
                elif isinstance(val, dict) and isinstance(val.get("repo"), str):
                    base = val.get("base")
                    root = base if isinstance(base, str) and base else cwd
                    out[root.rstrip("/")] = val["repo"]
        raw_roots = body.get("repo_roots")
        if isinstance(raw_roots, dict):
            for root, repo in raw_roots.items():
                if isinstance(root, str) and isinstance(repo, str) and root and repo:
                    out[root.rstrip("/")] = repo
    return out


def _project_from_cc_slug(slug: str) -> str | None:
    """Reconstruct the project name from a Claude Code project slug.

    Claude Code names its project directories `-<path-with-slashes-as-dashes>`.
    Directory names can themselves contain dashes (`my-cool-app`) so a
    straight split loses information; we walk the real filesystem to
    find the actual split points.

    Worktree sub-paths (`--claude-worktrees-*`) are stripped first.
    """
    base = slug.split("--")[0] if "--" in slug else slug

    resolved = _resolve_slug_path(base)
    if resolved:
        return resolved.name

    # Fallback: the last dash-separated component of the slug.
    parts = base.strip("-").split("-")
    last = parts[-1] if parts else None
    return last or None


def _resolve_slug_path(slug: str) -> Path | None:
    """Walk the filesystem to reconstruct the real path from a slug.

    Claude Code slugs replace both `/` and `.` with `-`. At each level
    we try the longest matching directory name first, testing both
    dash-literal and dot-substituted variants so `my-site-io` resolves
    to `my-site.io` when that's what exists on disk.
    """
    remaining = slug.lstrip("-")
    current = Path("/")

    while remaining:
        parts = remaining.split("-")
        matched = False
        for length in range(len(parts), 0, -1):
            candidate_name = "-".join(parts[:length])
            for name in _slug_name_variants(candidate_name):
                candidate_path = current / name
                if candidate_path.is_dir():
                    current = candidate_path
                    remaining = "-".join(parts[length:])
                    matched = True
                    break
            if matched:
                break
        if not matched:
            break

    return current if str(current) != "/" else None


def _slug_name_variants(name: str) -> list[str]:
    """Candidate directory names from a slug segment.

    Returns the literal name first, then the fully-dotted variant.
    Only the two extremes — generating every intermediate combination
    would blow up the search space.
    """
    variants = [name]
    dotted = name.replace("-", ".")
    if dotted != name:
        variants.append(dotted)
    return variants


#: Where a Codex home keeps rollouts. Codex MOVES a rollout, unchanged, from
#: `sessions/YYYY/MM/DD/` to a flat `archived_sessions/` when the developer
#: archives the thread. The file stem -- and so the session id -- is the same in
#: both, so reading the second folder cannot mint a second session.
CODEX_ROLLOUT_DIRS = ("sessions", "archived_sessions")


def codex_rollout_dirs(home: Path) -> list[Path]:
    """Return the rollout directories that exist under a Codex home.

    The single answer to "where are this home's Codex sessions", shared by
    every reader so none of them can overlook archived threads.

    Args:
        home: a candidate Codex home.

    Returns:
        The existing directories, `sessions/` first. EMPTY means this is not a
        Codex home: a home whose every thread is archived has no `sessions/`
        directory and is still a home.
    """
    return [home / name for name in CODEX_ROLLOUT_DIRS if (home / name).is_dir()]


#: Where Codex records the sessions it imported from another agent, one record
#: per import, each naming the Codex thread it created (`imported_thread_id`).
_CODEX_IMPORT_LEDGER = "external_agent_session_imports.json"
_UUID_LEN = 36


def _codex_imported_thread_ids(home: Path) -> frozenset[str]:
    """Return the thread ids Codex says it imported from another agent.

    Reads Codex's own ledger, so an import is identified by what Codex
    recorded, never guessed from a transcript's shape.

    Args:
        home: a Codex home (the directory holding `sessions/`).

    Returns:
        The imported thread ids. EMPTY when the ledger is absent, unreadable
        or not the expected shape, and malformed records are passed over: an
        empty set skips nothing, so every failure here fails open to
        capturing the rollout, never to dropping a session.
    """
    try:
        ledger = json.loads((home / _CODEX_IMPORT_LEDGER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    records = ledger.get("records") if isinstance(ledger, dict) else None
    if not isinstance(records, list):
        return frozenset()
    return frozenset(
        record["imported_thread_id"]
        for record in records
        if isinstance(record, dict) and isinstance(record.get("imported_thread_id"), str)
    )


def _codex_ran_a_turn(jsonl_file: Path) -> bool:
    """Return whether Codex ran a model turn of its own in this rollout.

    Codex writes a `turn_context` record for every turn it runs. A transcript
    it only imported has none; one the developer then continued in Codex does,
    and carries real Codex spend.

    Returns:
        True when a `turn_context` record is present -- and also when the file
        cannot be read, so an I/O fault can never be what drops a session.
    """
    # An import that was never continued is re-read in full on every index
    # build. Cache by (path, mtime, size) if a home ever holds enough imports
    # for that to matter.
    try:
        with jsonl_file.open("rb") as fh:
            for line in fh:
                if b'"turn_context"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("type") == "turn_context":
                    return True
    except OSError:
        return True
    return False


def _codex_is_uncontinued_import(jsonl_file: Path, imported_thread_ids: frozenset[str]) -> bool:
    """Return whether a rollout is another agent's session that Codex only imported.

    The single statement of the rule, shared by every Codex discovery path
    so they cannot disagree about which rollouts are Codex sessions.

    Args:
        jsonl_file: the rollout; its stem ends in its thread id.
        imported_thread_ids: `_codex_imported_thread_ids` of the rollout's home.

    Returns:
        True only when BOTH hold: Codex's ledger names the thread, and Codex
        never ran a turn of its own in it. The ledger alone would drop an
        import the developer went on to continue in Codex, which carries
        real Codex spend.
    """
    return jsonl_file.stem[-_UUID_LEN:] in imported_thread_ids and not _codex_ran_a_turn(jsonl_file)


def _cwd_from_codex_head(head: bytes) -> str | None:
    """The absolute `cwd` Codex records in its `session_meta` line.

    Codex stamps the working directory once, in the opening `session_meta`
    payload (unlike Claude Code, which repeats it on every record). This is
    machine-independent, so it stays correct for transcripts copied from
    another machine - and it's the key mapped to `owner/repo` via the
    manifest's `cwd_repos`, so without capturing it a Codex session can't
    resolve a repo.
    """
    # Scan every line `_read_head` returned, not a fixed count. `_read_head`
    # already bounds the work (it stops at ~64KB / 5MB), so a second cap here
    # would drop valid sessions: transcripts can open with many lines of
    # metadata (`ai-title`, `agent-name`, `permission-mode`,
    # `file-history-snapshot`, `cursor_usage_event`) before the first real
    # record, and a `cursor_usage_event` stream can be the only place billed
    # tokens live.
    for raw_line in head.split(b"\n"):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            obj = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # A byte-truncated head can split a multi-byte UTF-8 char.
            continue
        if obj.get("type") == "session_meta":
            cwd = obj.get("payload", {}).get("cwd", "").rstrip("/")
            return cwd or None
    return None


def _first_field_in_cc_head(head: bytes, key: str) -> str | None:
    """First non-empty string value for `key` among the head's session lines.

    Claude Code stamps `cwd` and `gitBranch` on (nearly) every record, so the
    first record carrying them is normally line 1 and this returns immediately.

    Scans every line the head holds rather than a fixed count. The head is
    already bounded by ``_read_head``'s ``max_bytes``, and a session whose first
    real record sits deep in the file has its ``cwd`` and ``gitBranch`` on that
    same record, so a line cap would return None for both and the session would
    have no repo attribution. A record carrying the field is the only thing
    worth stopping on; a line number never is.
    """
    for raw_line in head.split(b"\n"):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            obj = json.loads(raw_line)
        except (json.JSONDecodeError, ValueError):
            continue
        val = obj.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _cwd_from_cc_head(head: bytes) -> str | None:
    """The `cwd` Claude Code stamps on its session lines.

    Unlike the on-disk project slug, cwd is **machine-independent** — it
    stays correct when the transcript was copied from another
    developer's machine, where the slug's real path doesn't exist to walk.
    """
    return _first_field_in_cc_head(head, "cwd")


def _branch_metadata(data: bytes, agent: AgentType) -> tuple[str | None, list[str]]:
    """Read observed branch fields in transcript order, excluding subagents.

    No local git lookup: sessions may have run on another machine.
    Claude Code records changes per event, Codex in session_meta.git, and
    Copilot in its session context. Other formats without a branch stay empty.
    """
    latest = None
    branches: list[str] = []
    for line in data.splitlines():
        try:
            obj = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(obj, dict) or obj.get("isSidechain") is True:
            continue
        branch = None
        if agent == AgentType.CLAUDE_CODE:
            branch = obj.get("gitBranch")
        elif agent == AgentType.CODEX and obj.get("type") == "session_meta":
            payload = obj.get("payload")
            git = payload.get("git") if isinstance(payload, dict) else None
            branch = git.get("branch") if isinstance(git, dict) else None
        elif agent == AgentType.COPILOT and obj.get("type") in {"session.start", "session.resume"}:
            payload = obj.get("data")
            context = payload.get("context") if isinstance(payload, dict) else None
            branch = context.get("branch") if isinstance(context, dict) else None
        if isinstance(branch, str) and branch.strip():
            latest = branch.strip()
            if latest != "HEAD" and latest not in branches:
                branches.append(latest)
    return latest, branches


def _branch_from_cc_head(head: bytes) -> str | None:
    """Latest branch observed in the supplied head (full history loads on parse)."""
    return _branch_metadata(head, AgentType.CLAUDE_CODE)[0]


def _cwd_from_opencode_head(head: bytes) -> str | None:
    """The `cwd` opencode records on its assistant messages (`message.path.cwd`).

    Machine-independent (the session's working directory, not an on-disk slug),
    so it stays correct for transcripts copied from another machine - and it's what the repo
    resolver's manifest tier and Conductor-workspace detection key off. The
    opening record is a user message with no `path`, so scan a few lines to
    reach the first assistant message.
    """
    for raw_line in head.split(b"\n")[:30]:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            obj = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        message = obj.get("message")
        if isinstance(message, dict):
            cwd = (message.get("path") or {}).get("cwd")
            if isinstance(cwd, str) and cwd:
                return cwd.rstrip("/")
    return None


def _cwd_from_cursor_store_head(head: bytes) -> str | None:
    """The `cwd` Cursor's synthesized header line carries (line 1, `cursor_store_session`).

    Machine-independent (resolved from the store.db's workspace URI or the
    acp-sessions `meta.json` sidecar before materializing the header), same
    rationale as every other agent's cwd getter - it stays correct for
    transcripts copied from another machine.
    """
    for raw_line in head.split(b"\n")[:5]:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            obj = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if obj.get("type") != "cursor_store_session":
            return None  # first real line isn't the header — nothing to find
        cwd = obj.get("cwd")
        return cwd.rstrip("/") if isinstance(cwd, str) and cwd else None
    return None


def _cursor_store_message_count(head: bytes) -> int:
    """Count real (non-header) message lines in a Cursor NDJSON head.

    The header (line 1) carries no conversation; `find_cursor_store_candidates`
    already refuses to materialize a session with zero messages, but a
    hand-edited file could still produce one - and `parse_cursor_store` would
    then yield an empty span list, which the pipeline treats as a
    FAILED session rather than a benign skip. Index time is where "nothing to
    index" can be told apart from "indexing broke", so callers use this to skip indexing such a file outright.
    """
    count = 0
    # Scan every line `_read_head` returned, not a fixed count. `_read_head`
    # already bounds the work (it stops at ~64KB / 5MB), so a second cap here
    # would drop valid sessions: transcripts can open with many lines of
    # metadata (`ai-title`, `agent-name`, `permission-mode`,
    # `file-history-snapshot`, `cursor_usage_event`) before the first real
    # record, and a `cursor_usage_event` stream can be the only place billed
    # tokens live.
    for i, raw_line in enumerate(head.split(b"\n")):
        if i == 0:
            continue  # header line
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            obj = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(obj, dict) and "role" in obj:
            count += 1
    return count


def _cwd_from_kiro_head(head: bytes) -> str | None:
    """The workspace root Kiro's synthesized header carries (`workspacePaths[0]`).

    Machine-independent (Kiro records the real path the developer had open in
    the IDE), so it survives copying - same rationale as every other agent's
    cwd getter.
    """
    for raw_line in head.split(b"\n")[:5]:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            obj = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if obj.get("type") != "kiro_session":
            return None  # first real line isn't the header — nothing to find
        paths = obj.get("workspacePaths")
        if isinstance(paths, list) and paths and isinstance(paths[0], str) and paths[0]:
            return paths[0].rstrip("/")
        return None
    return None


def _kiro_event_count(head: bytes) -> int:
    """Count real (non-header) event lines in a Kiro NDJSON head.

    The header (line 1) carries no conversation. `find_kiro_candidates`
    already refuses to materialize a session with no `turn_end`, but a
    hand-edited file could still produce an event-less one — and `parse_kiro`
    would then yield an empty span list, which the pipeline
    treats as a FAILED session rather than a benign skip. Same guard, and same
    reasoning, as `_cursor_store_message_count`.
    """
    count = 0
    # Scan every line `_read_head` returned, not a fixed count. `_read_head`
    # already bounds the work (it stops at ~64KB / 5MB), so a second cap here
    # would drop valid sessions: transcripts can open with many lines of
    # metadata (`ai-title`, `agent-name`, `permission-mode`,
    # `file-history-snapshot`, `cursor_usage_event`) before the first real
    # record, and a `cursor_usage_event` stream can be the only place billed
    # tokens live.
    for i, raw_line in enumerate(head.split(b"\n")):
        if i == 0:
            continue  # header line
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            obj = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("payload"), dict):
            count += 1
    return count


def _copilot_session_start(head: bytes) -> dict | None:
    """The ``data`` payload of the head's ``session.start`` event, if any."""
    for raw_line in head.split(b"\n"):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            obj = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "session.start":
            payload = obj.get("data")
            return payload if isinstance(payload, dict) else None
    return None


def _copilot_session_key(start: dict | None, jsonl_file: Path) -> str:
    """The session id segment for a Copilot transcript.

    Every current-layout file is literally named ``events.jsonl``, so the stem
    would collide every session onto one id. Prefer the header's own
    ``sessionId`` (the UUID ``copilot --resume`` takes); fall back to the
    per-session directory name for the current layout and to the stem for the
    legacy flat layout.
    """
    if start is not None:
        sid = start.get("sessionId")
        if isinstance(sid, str) and sid.strip():
            return sid.strip()
    if jsonl_file.stem == "events":
        return jsonl_file.parent.name
    return jsonl_file.stem


def _cwd_from_copilot_head(head: bytes, start: dict | None) -> str | None:
    """cwd from the ``session.start`` context, else the folder-trust notice.

    Legacy builds wrote no ``context`` on ``session.start``; their only cwd hint
    is the ``session.info`` line "Folder <path> has been added to trusted
    folders." (the same fallback the parser uses).
    """
    if start is not None:
        ctx = start.get("context")
        if isinstance(ctx, dict):
            cwd = ctx.get("cwd")
            if isinstance(cwd, str) and cwd:
                return cwd.rstrip("/")
    for raw_line in head.split(b"\n"):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            obj = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(obj, dict) or obj.get("type") != "session.info":
            continue
        payload = obj.get("data")
        if not isinstance(payload, dict) or payload.get("infoType") != "folder_trust":
            continue
        message = payload.get("message")
        if not isinstance(message, str):
            continue
        marker = "Folder "
        at = message.find(marker)
        if at == -1:
            continue
        rest = message[at + len(marker) :]
        end = rest.find(" has been")
        path = (rest if end == -1 else rest[:end]).strip()
        if path:
            return path.rstrip("/")
    return None


def _branch_from_copilot_head(start: dict | None) -> str | None:
    """Git branch from the ``session.start`` context, when Copilot recorded one."""
    if start is None:
        return None
    ctx = start.get("context")
    if not isinstance(ctx, dict):
        return None
    branch = ctx.get("branch")
    return branch if isinstance(branch, str) and branch else None


# Bound for the second, cwd-hunting head read on a Copilot log whose first head
# carried no cwd. Legacy builds wrote the folder-trust notice as a separate
# `session.info` record, so it can sit past the detection-sized head; 1 MiB
# covers any realistic preamble while keeping a cwd-less legacy file (a folder
# already trusted earlier emits no notice at all) from costing a 5 MB read.
_COPILOT_CWD_SCAN_MAX_BYTES = 1_000_000


def _copilot_cwd_present(buf: bytes) -> bool:
    """`_read_head` stop predicate: a cwd-bearing Copilot RECORD has been read.

    Record-aware on purpose: a bare substring test on ``"cwd"`` would also fire
    on a tool call whose arguments happen to carry a ``cwd`` key and stop the
    scan before the later folder-trust notice. `_read_head` hands the predicate
    complete lines, so parsing them here is exact, and the same two getters
    the indexer uses decide what counts.
    """
    return _cwd_from_copilot_head(buf, _copilot_session_start(buf)) is not None


def _copilot_has_conversation(head: bytes) -> bool:
    """True when the head holds a prompt or an assistant reply.

    Same guard, same reasoning as ``_cursor_store_message_count`` /
    ``_kiro_event_count``: a metadata-only file would yield an
    empty span list, which the pipeline treats as a FAILED session
    rather than a benign skip. Scans every line ``_read_head`` returned.
    """
    for raw_line in head.split(b"\n"):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            obj = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(obj, dict) and obj.get("type") in ("user.message", "assistant.message"):
            return True
    return False


def _copilot_vscode_jsonl_session_id(head: bytes) -> str | None:
    """``sessionId`` from a Copilot-VS-Code ``.jsonl``'s first line (the
    kind=0 snapshot). Cheap and stable: a replay may later rewrite
    which requests exist, never the id the session was created under."""
    first_line = head.split(b"\n", 1)[0].strip()
    try:
        obj = json.loads(first_line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict) or obj.get("kind") != 0:
        return None
    snapshot = obj.get("v")
    session_id = snapshot.get("sessionId") if isinstance(snapshot, dict) else None
    return session_id if isinstance(session_id, str) and session_id else None


def _copilot_vscode_json_session_id(head: bytes) -> tuple[str | None, bool]:
    """``(sessionId, has_requests)`` from a Copilot-VS-Code ``.json`` single
    document.

    Confirms the shape directly (``requests`` present) rather than through
    ``detect_agent`` -- a pretty-printed document has no single
    self-contained JSON line for that line-oriented scanner to match. A
    byte-capped ``head`` on a huge file fails ``json.loads`` cleanly (the
    document is no longer valid JSON once truncated mid-object), which reads
    as "not this format" here rather than a crash -- same safe-failure shape
    ``detect_agent`` itself relies on for a malformed line.

    ``has_requests`` lets the caller skip a genuinely empty "empty window"
    document (``requests: []``) without a second full-document parse.
    """
    try:
        obj = json.loads(head)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, False
    if not isinstance(obj, dict) or "requests" not in obj:
        return None, False
    session_id = obj.get("sessionId")
    session_id = session_id if isinstance(session_id, str) and session_id else None
    requests = obj.get("requests")
    return session_id, isinstance(requests, list) and len(requests) > 0


def _gemini_header(head: bytes) -> dict | None:
    """The Gemini CLI session header: the first record with `sessionId` and no `type`."""
    for line in head.split(b"\n"):
        try:
            obj = json.loads(line)
        except ValueError:  # blank, truncated, or split mid-character
            continue
        if isinstance(obj, dict) and "type" not in obj and isinstance(obj.get("sessionId"), str):
            return obj
    return None


def _gemini_has_messages(path: Path) -> bool:
    """True when the file holds a real prompt or a `gemini` message.

    Streams one line at a time and stops at the first qualifying record, so a
    prompt that sits past `_read_head` is still found. Reads at most
    ``MAX_SOURCE_BYTES``. A file of only the header or user content
    ``_user_prompt`` rejects is skipped at EOF. A ``RecursionError`` from a
    deeply nested record skips that line instead of aborting the index.
    """
    remaining = MAX_SOURCE_BYTES
    with path.open("rb") as handle:
        while remaining > 0:
            line = handle.readline(remaining)
            if not line:
                break
            remaining -= len(line)
            # `readline(n)` also returns this when the file ends on a full
            # record with no trailing newline, exactly at the cap. Parse it;
            # only a cut that is not JSON is truncated.
            at_cap = remaining == 0 and not line.endswith(b"\n")
            try:
                obj = json.loads(line)
            except (ValueError, RecursionError):
                if at_cap:
                    break
                continue
            if not isinstance(obj, dict):
                continue
            kind = obj.get("type")
            if kind == "gemini" or (
                kind == "user" and _gemini_user_prompt(obj.get("content")) is not None
            ):
                return True
    return False


def _cwd_from_gemini_project(jsonl_file: Path, header: dict | None) -> str | None:
    """The project root of a Gemini CLI session.

    The transcript stores only `projectHash` (sha256 of the project root). The
    CLI writes the root itself to `.project_root` in the project folder
    (`<home>/tmp/<slug>/.project_root`, two levels above `chats/<file>`).
    When the header carries a hash, the root must hash to it, so a stale or
    copied `.project_root` can't name the wrong project.
    """
    try:
        root = (jsonl_file.parent.parent / ".project_root").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not root:
        return None
    expected = (header or {}).get("projectHash")
    if isinstance(expected, str) and expected:
        if hashlib.sha256(root.encode("utf-8")).hexdigest() != expected:
            return None
    return root


def _project_from_cwd(cwd: str) -> str | None:
    """Project name from the transcript's cwd.

    Normally the cwd's final path component (`…/api` → `api`).

    Claude Code git worktrees live at `<repo>/.claude/worktrees/<name>`,
    where the final component is a generated worktree name
    (`joyful-shimmying-crane`) — useless as a project label and a
    cost-attribution leak: every worktree session would become its own
    phantom project, splitting one repo's spend across many buckets. For
    those, the real project is the repo dir holding `.claude`, so we take
    the component just before `/.claude/worktrees/`.

    Normalized through `_paths.to_posix` first: this runs ahead of every
    parser (the parsers' own basename fallback only fires when this
    returns None), so a backslashed Windows cwd handled here as a raw
    string would surface the whole path as the project name — and the
    worktree marker would never match its backslashed form either.
    """
    path = to_posix(cwd)
    marker = "/.claude/worktrees/"
    if marker in path:
        path = path.split(marker, 1)[0]
    name = path.rsplit("/", 1)[-1]
    return name or None


def _has_cursor_store_header(head: bytes) -> bool:
    """True if the head carries a ``cursor_store_session`` line (the store.db shape).

    Distinguishes Cursor's two local JSONL shapes — both detect as
    ``AgentType.CURSOR`` — so the index can route the store.db NDJSON to
    ``parse_cursor_store`` and the header-less ``agent-transcripts`` narrative to
    ``parse_cursor_agent_transcript``.
    """
    for raw in head.split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(obj, dict) and obj.get("type") == "cursor_store_session":
            return True
    return False


def _claude_code_subagent_files(transcript: Path) -> list[Path]:
    """List child transcripts; missing directory is normal, read errors are not."""
    directory = transcript.with_suffix("") / "subagents"
    try:
        with os.scandir(directory) as entries:
            return sorted(Path(e.path) for e in entries if Path(e.name).match("agent-*.jsonl"))
    except FileNotFoundError:
        return []


def _with_claude_code_subagents(
    data: bytes, transcript: Path, *, related_parents: tuple[Path, ...] = ()
) -> bytes:
    """Read child files beside every duplicate parent, without losing unique calls.

    Any unreadable/oversized child fails this session, leaving the caller's replay
    pending. Exact lines repeated in another file are ignored; streaming fragments
    within a file retain their parser semantics. Parent offsets remain unchanged.
    """
    files = sorted(
        {
            child
            for parent in (transcript, *related_parents)
            for child in _claude_code_subagent_files(parent)
        }
    )
    parts = [data]
    seen: set[bytes] = set()
    total = len(data)
    for path in files:
        size = path.stat().st_size
        if total + size > MAX_SESSION_BYTES:
            raise OversizedSessionError(f"Subagent {path.name} exceeds the session byte cap")
        extra = path.read_bytes()
        if len(extra) > MAX_SESSION_BYTES:
            raise OversizedSessionError(f"Subagent {path.name} grew past the session byte cap")
        lines = extra.splitlines(keepends=True)
        unseen = [line for line in lines if line not in seen]
        seen.update(lines)
        extra = b"".join(unseen)
        if not extra:
            continue
        sep = b"" if parts[-1].endswith(b"\n") else b"\n"
        if total + len(extra) + len(sep) > MAX_SESSION_BYTES:
            raise OversizedSessionError(
                f"{transcript.name}: parent and children exceed the session byte cap"
            )
        parts.extend((sep, extra))
        total += len(extra) + len(sep)
    return b"".join(parts)


class LocalSource:
    """Read JSONL session files from local Claude Code / Codex homes.

    Per-home index pattern:
      Claude Code: `<home>/projects/<slug>/**/*.jsonl`
      Codex:       `<home>/sessions/**/*.jsonl`

      Copilot:     `<home>/session-state/<session-id>/events.jsonl`
                   (legacy `<home>/session-state/<session-id>.jsonl`)

    Sub-agent sessions (Claude Code's `isSidechain=true` files —
    prompt suggestions, compactions, etc.) are skipped to avoid
    polluting the index with non-conversation traces.
    """

    source_name = "local"

    def __init__(
        self,
        claude_code_homes: list[Path],
        codex_homes: list[Path],
        opencode_homes: list[Path] | None = None,
        cursor_store_homes: list[Path] | None = None,
        kiro_homes: list[Path] | None = None,
        copilot_homes: list[Path] | None = None,
        copilot_vscode_homes: list[Path] | None = None,
        gemini_homes: list[Path] | None = None,
        antigravity_homes: list[Path] | None = None,
    ) -> None:
        self.claude_code_homes = claude_code_homes
        self.codex_homes = codex_homes
        # opencode has no per-session file - each SQLite
        # session is materialized to NDJSON under a home labelled `opencode`, laid out
        # `<home>/<project_id>/<session_id>.jsonl` (no `projects/`/`sessions/`
        # wrapper), so it gets its own rglob walk + indexer below.
        self.opencode_homes = opencode_homes or []
        # Cursor CLI/agent sessions have no per-session JSONL either — the
        # materializer decodes each `store.db` into an NDJSON
        # transcript per session, laid out flat as `<home>/<session_id>.jsonl`
        # (no `projects/`/`sessions/` wrapper, no per-project subdir like
        # opencode). Same rglob-the-whole-home treatment.
        self.cursor_store_homes = cursor_store_homes or []
        # Kiro sessions are a DIRECTORY per session, so the materializer merges
        # each one (session.json + messages.jsonl + sub-executions/*.jsonl) into
        # a single NDJSON transcript, laid out `<home>/<ws_hash>/<session_id>
        # .jsonl` — one subdir level, like opencode. Same rglob walk.
        self.kiro_homes = kiro_homes or []
        # Copilot CLI writes a real per-session JSONL itself, so no
        # materialization step: `<home>/session-state/<session-id>/events.jsonl`
        # (older builds: `<home>/session-state/<session-id>.jsonl`). Every file
        # is named `events.jsonl`, so the session id comes from the header's
        # `sessionId` (else the parent directory), never the file stem.
        self.copilot_homes = copilot_homes or []
        # Copilot in VS Code (Chat/agent mode) writes a `User` dir that
        # `_copilot_vscode_homes()` resolves per app/OS; chat sessions live
        # one or two levels under it (`workspaceStorage/*/chatSessions/` or
        # `globalStorage/emptyWindowChatSessions/`), so this also gets the
        # whole-home rglob treatment, over both `.jsonl` and `.json`.
        self.copilot_vscode_homes = copilot_vscode_homes or []
        # Gemini CLI writes a real per-session JSONL itself:
        # `<home>/tmp/<project-slug>/chats/session-*.jsonl`. The session id
        # comes from the header's `sessionId`, the cwd from the project
        # folder's `.project_root` (see `_cwd_from_gemini_project`).
        self.gemini_homes = gemini_homes or []
        # Antigravity CLI step logs; discovery lives in antigravity_index.py.
        self.antigravity_homes = antigravity_homes or []
        # Per-home identity stamp (`<home>/_manifest/*.json`). It carries a
        # user_uuid/email/github_username used to detect duplicated sessions
        # that belong to different owners. Missing manifest → empty dict.
        self._home_identities: dict[Path, ManifestIdentity] = {}
        # Per-home `cwd → owner/repo` map from the manifest's `cwd_repos`
        # (a git-remote stamp). Feeds the repo resolver's top `manifest`
        # tier - the only reliable repo signal for copied transcripts.
        # Empty for manifests predating the stamp.
        self._home_repos: dict[Path, dict[str, str]] = {}
        # Per-home `git_toplevel → owner/repo` map from the manifest's
        # `repo_roots` (a stamp of every repo the sessions' tool calls
        # touched - not just the cwds). Feeds multi-repo attribution:
        # files are prefix-matched against these roots, where no git is
        # on disk.
        self._home_roots: dict[Path, dict[str, str]] = {}
        for home in (
            *claude_code_homes,
            *codex_homes,
            *self.opencode_homes,
            *self.cursor_store_homes,
            *self.kiro_homes,
            *self.copilot_homes,
            *self.copilot_vscode_homes,
            *self.gemini_homes,
            *self.antigravity_homes,
        ):
            identity = _load_manifest_identity(home)
            if identity is not None:
                self._home_identities[home] = identity
                logger.info(
                    "manifest identity loaded home=%s user_uuid=%s email=%s github=%s",
                    home,
                    identity.user_uuid,
                    identity.email,
                    identity.github_username,
                )
            repos = _load_manifest_repos(home)
            if repos:
                self._home_repos[home] = repos
            roots = _load_manifest_repo_roots(home)
            if roots:
                self._home_roots[home] = roots
        self._file_index: dict[str, _FileRef] | None = None
        self._branch_history: dict[str, tuple[str | None, list[str]]] = {}
        self._claude_code_families: dict[str, set[Path]] = {}
        self._slug_cache: dict[str, str | None] = {}
        # Per-agent file-discovery tally, filled in by
        # `_record_discovery` as `_build_index()` walks each home. Pure
        # aggregate integers, keyed by the home list being walked (e.g.
        # "claude-code"), not necessarily the file's own detected agent —
        # see `_record_discovery`'s docstring for why that distinction matters.
        self._discovery_stats: dict[str, dict[str, Any]] = {}

    def _record_discovery(self, walk_agent: str, outcome: str) -> None:
        """Tally one file's discovery outcome. Aggregate counts ONLY.

        `walk_agent` is the home list this file was found under (e.g.
        "claude-code"), not necessarily what `detect_agent` returned for
        it — a `"different_agent"` outcome means detection succeeded but
        for a different type sharing the same home directory, which is
        expected and not a capture problem.

        `outcome` is either `"indexed"` (became a real session) or a skip
        reason (`"different_agent"`, `"detection_failed"`, ...) — see each
        `_index_*_file` method for its own reasons.

        Never pass a file path, name, byte size, or any other per-file
        detail here or downstream of here: the whole point of this tally is
        that nothing more specific than an integer count ever leaves this
        method.
        """
        bucket = self._discovery_stats.setdefault(
            walk_agent, {"seen": 0, "indexed": 0, "skipped": {}}
        )
        bucket["seen"] += 1
        if outcome == "indexed":
            bucket["indexed"] += 1
        else:
            bucket["skipped"][outcome] = bucket["skipped"].get(outcome, 0) + 1

    def discovery_stats(self) -> dict[str, dict[str, Any]]:
        """Per-agent file-discovery counts from the last `_build_index()` call.

        ``{agent: {"seen": int, "indexed": int, "skipped": {reason: int}}}``
        — pure aggregate integers, never a file path, name, or byte size.
        Empty until `discover_sessions()` (which calls `_build_index()`) has
        run at least once.
        """
        return self._discovery_stats

    def capabilities(self) -> frozenset[str]:
        """Local files expose tokens + project_name + tool calls, but
        no user identity beyond the file owner."""
        return frozenset(
            {
                "tokens",
                "tool_calls",
                "project_name",
                "user_id_from_filesystem",
                "multi_agent",
            }
        )

    def _build_index(self) -> dict[str, _FileRef]:
        if self._file_index is not None:
            return self._file_index

        index: dict[str, _FileRef] = {}
        # Newest mtime per session across duplicate prefixes; applied to the
        # winners after the walk (see `_carry_collision_mtimes`).
        newest: dict[str, datetime] = {}

        for home in self.claude_code_homes:
            projects_dir = home / "projects"
            if not projects_dir.is_dir():
                logger.debug("Skipping non-existent path: %s", projects_dir)
                continue
            for jsonl_file in projects_dir.rglob("*.jsonl"):
                ref = self._index_cc_file(jsonl_file, projects_dir, home=home)
                if ref:
                    incumbent = index.get(ref[0])
                    if incumbent:
                        old_identity = self._home_identities.get(incumbent.home)
                        new_identity = self._home_identities.get(home)

                        def owner(identity):
                            return (
                                (identity.org_uuid, identity.user_uuid or identity.email)
                                if identity
                                else None
                            )

                        if owner(old_identity) != owner(new_identity):
                            raise ValueError(f"Conflicting owners for duplicated session {ref[0]}")
                    self._claude_code_families.setdefault(ref[0], set()).add(jsonl_file)
                    _index_put(index, ref[0], ref[1], newest)

        for home in self.codex_homes:
            rollout_dirs = codex_rollout_dirs(home)
            if not rollout_dirs:
                logger.debug("Skipping non-existent path: %s", home / "sessions")
                continue
            imported_thread_ids = _codex_imported_thread_ids(home)
            for rollout_dir in rollout_dirs:
                for jsonl_file in rollout_dir.rglob("*.jsonl"):
                    ref = self._index_codex_file(
                        jsonl_file, home=home, imported_thread_ids=imported_thread_ids
                    )
                    if ref:
                        _index_put(index, ref[0], ref[1], newest)

        # opencode: materialized NDJSON lives at `<home>/<project_id>/*.jsonl`
        # (no fixed sub-dir like Claude's `projects/` or Codex's `sessions/`),
        # so walk the whole home. `detect_agent` content-sniffs each file, so a
        # stray non-opencode `.jsonl` here is skipped rather than mis-parsed.
        for home in self.opencode_homes:
            if not home.is_dir():
                logger.debug("Skipping non-existent path: %s", home)
                continue
            for jsonl_file in home.rglob("*.jsonl"):
                ref = self._index_opencode_file(jsonl_file, home=home)
                if ref:
                    _index_put(index, ref[0], ref[1], newest)

        # Cursor: materialized NDJSON lives flat at `<home>/<session_id>.jsonl`
        # (no per-project subdir even), so walk the whole home too.
        # `detect_agent` content-sniffs each file for the `cursor_store_session`
        # header, so a stray non-Cursor `.jsonl` here is skipped, not mis-parsed.
        for home in self.cursor_store_homes:
            if not home.is_dir():
                logger.debug("Skipping non-existent path: %s", home)
                continue
            for jsonl_file in home.rglob("*.jsonl"):
                ref = self._index_cursor_store_file(jsonl_file, home=home)
                if ref:
                    _index_put(index, ref[0], ref[1], newest)

        # Kiro: materialized NDJSON lives at `<home>/<ws_hash>/<session_id>.jsonl`.
        # `detect_agent` content-sniffs for the `kiro_session` header, so a stray
        # non-Kiro `.jsonl` here is skipped, not mis-parsed.
        for home in self.kiro_homes:
            if not home.is_dir():
                logger.debug("Skipping non-existent path: %s", home)
                continue
            for jsonl_file in home.rglob("*.jsonl"):
                ref = self._index_kiro_file(jsonl_file, home=home)
                if ref:
                    _index_put(index, ref[0], ref[1], newest)

        # Copilot: `<home>/session-state/<session-id>/events.jsonl` (legacy flat
        # `<session-id>.jsonl`). Walk the whole home so a copied tree that
        # kept only the `session-state/` subtree still indexes. `detect_agent`
        # sniffs for the `session.start` / `producer=copilot-agent` header, so
        # a stray non-Copilot `.jsonl` is skipped, not mis-parsed.
        for home in self.copilot_homes:
            if not home.is_dir():
                logger.debug("Skipping non-existent path: %s", home)
                continue
            for jsonl_file in home.rglob("*.jsonl"):
                ref = self._index_copilot_file(jsonl_file, home=home)
                if ref:
                    _index_put(index, ref[0], ref[1], newest)

        # Copilot in VS Code (Chat/agent mode): `<home>/workspaceStorage/<ws>/
        # chatSessions/<id>.jsonl` (snapshot+patch log) and `<home>/
        # globalStorage/emptyWindowChatSessions/<id>.json|.jsonl` (a `.json`
        # is the session's final state as one document; a `.jsonl` is the
        # same snapshot+patch shape as chatSessions'). Walk both extensions —
        # `_index_copilot_vscode_file` tells them apart and confirms the
        # match itself, so a stray non-matching file here is skipped, not
        # mis-parsed. When both a `.json` and a `.jsonl` exist for the same
        # session, they collide on the same `session_id` key and resolve
        # through the SAME size-first, mtime-only-as-tiebreak rule every
        # other agent's multi-file case already uses (`_index_put` -- byte
        # size is the "more complete" signal since transcripts are
        # append-only; mtime only breaks an exact size tie) -- no bespoke
        # "prefer jsonl" rule. In practice a `.jsonl` with any real
        # conversation is larger than an abandoned empty `.json`, so this
        # already resolves the common case correctly; it is not a
        # format-aware rule, just the existing one applied here too.
        for home in self.copilot_vscode_homes:
            if not home.is_dir():
                logger.debug("Skipping non-existent path: %s", home)
                continue
            for session_file in (*home.rglob("*.jsonl"), *home.rglob("*.json")):
                ref = self._index_copilot_vscode_file(session_file, home=home)
                if ref:
                    _index_put(index, ref[0], ref[1], newest)

        # Gemini CLI: `<home>/tmp/<project-slug>/chats/session-*.jsonl`.
        # `detect_agent` sniffs the header, so a stray `.jsonl` is skipped.
        for home in self.gemini_homes:
            tmp_dir = home / "tmp"
            if not tmp_dir.is_dir():
                logger.debug("Skipping non-existent path: %s", tmp_dir)
                continue
            for jsonl_file in tmp_dir.glob("*/chats/session-*.jsonl"):
                ref = self._index_gemini_file(jsonl_file, home=home)
                if ref:
                    _index_put(index, ref[0], ref[1], newest)

        # Antigravity CLI: only `<home>/brain/` is walked (see antigravity_index.py).
        for home in self.antigravity_homes:
            transcripts = home.glob(ANTIGRAVITY_TRANSCRIPT_GLOB)
            for sid, fields in index_antigravity_home(home, transcripts, self._record_discovery):
                _index_put(index, sid, _FileRef(**fields), newest)

        _carry_collision_mtimes(index, newest)
        self._file_index = index
        logger.info(
            "Indexed %d local JSONL files from %d homes",
            len(index),
            len(self.claude_code_homes)
            + len(self.codex_homes)
            + len(self.opencode_homes)
            + len(self.cursor_store_homes)
            + len(self.kiro_homes)
            + len(self.copilot_homes)
            + len(self.copilot_vscode_homes)
            + len(self.gemini_homes)
            + len(self.antigravity_homes),
        )
        return index

    def _index_cc_file(
        self,
        jsonl_file: Path,
        projects_dir: Path,
        *,
        home: Path,
    ) -> tuple[str, _FileRef] | None:
        """Index one Claude Code JSONL file (or skip if sub-agent / wrong agent)."""
        if not jsonl_file.is_file():
            # A directory or broken symlink `rglob` matched by pattern —
            # never a real transcript, so never tallied at all (counting it as
            # "empty_or_unreadable" would report a phantom file).
            return None
        size = jsonl_file.stat().st_size
        if size == 0:
            self._record_discovery("claude-code", "empty_or_unreadable")
            return None
        # `<slug>/<session>/subagents/agent-*.jsonl` are component pieces of
        # the parent session, not sessions of their own — `get_spans_by_session`
        # folds them into the parent (`_with_claude_code_subagents`), so
        # indexing one here would also stand it up as a standalone session and
        # count its turns, tools and spend twice.
        #
        # Checked before the head is read, so a subagent file never reaches
        # detection (and its warning). It is a PATH check because the content
        # check below is not sufficient on its own: it reads the first parseable line, and newer
        # Claude Code builds front-load `queue-operation` / `last-prompt` /
        # `ai-title` records that carry no `isSidechain` flag at all, so the
        # loop breaks on one of those and admits the file. Observed in real
        # transcript sets: ids shaped `local:claude-code:agent-<hex>`,
        # every one of them a subagent transcript admitted this way. The Cursor
        # indexer makes exactly this exclusion already. Check the parts BELOW
        # `projects_dir` only: a configured home (or any ancestor of it) could itself be named
        # `subagents`, which must not drop real sessions.
        try:
            relative_parts = jsonl_file.relative_to(projects_dir).parts
        except ValueError:
            relative_parts = ()
        if "subagents" in relative_parts:
            self._record_discovery("claude-code", "subagent_component")
            return None

        try:
            head = _read_head(jsonl_file, stop_when=_agent_detected)
        except OSError:
            self._record_discovery("claude-code", "empty_or_unreadable")
            return None

        agent = detect_agent(head)
        if agent is None:
            # Only a head that is the whole file can prove there is no conversation;
            # a capped head may stop before the first message.
            if len(head) >= size and _is_metadata_only(head):
                logger.debug("no conversation in %s (%d bytes); session skipped", jsonl_file, size)
                self._record_discovery("claude-code", "no_conversation")
                return None
            # A .jsonl no agent shape claims is skipped, but loudly, so format
            # drift surfaces in logs instead of as quietly-missing data.
            logger.warning(
                "agent detection failed for %s (%d bytes); session skipped",
                jsonl_file,
                size,
            )
            self._record_discovery("claude-code", "detection_failed")
            return None
        if agent != AgentType.CLAUDE_CODE:
            # A *different* agent's file in this home is fine (multi-agent
            # homes are by design) — the matching indexer picks it up.
            self._record_discovery("claude-code", "different_agent")
            return None

        # Sub-agent sessions also carry isSidechain=true; skip them — they're
        # not developer-initiated work. Retained for the older layout, which
        # wrote a sidechain as a sibling transcript with no `subagents/` parent.
        for line in head.split(b"\n"):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("isSidechain") is True:
                    self._record_discovery("claude-code", "subagent_component")
                    return None
                break
            except (json.JSONDecodeError, ValueError):
                continue

        stem = jsonl_file.stem
        session_id = f"local:{agent}:{stem}"
        mtime = datetime.fromtimestamp(jsonl_file.stat().st_mtime, tz=UTC)
        # Time-window filtering reads this clock, and a subagent file can
        # be newer than the parent (parent killed mid-subagent, so no
        # tool_result line ever advanced it) -- the session's activity clock
        # is the newest write across the whole family.
        for sub in _claude_code_subagent_files(jsonl_file):
            try:
                mtime = max(mtime, datetime.fromtimestamp(sub.stat().st_mtime, tz=UTC))
            except OSError:
                continue

        # Project name: prefer the transcript's own `cwd` — it's
        # machine-independent, so it stays correct for transcripts
        # copied from another machine. Fall back to reconstructing the on-disk slug only
        # when cwd is absent; that walk needs the real path to exist
        # locally, so it's unreliable for someone else's transcripts
        # (e.g. `-Users-ada-work-my-cool-app` → `Users`).
        cwd = _cwd_from_cc_head(head)
        project_name = _project_from_cwd(cwd) if cwd else None
        if project_name is None:
            try:
                rel = jsonl_file.relative_to(projects_dir)
                slug = rel.parts[0]
            except (ValueError, IndexError):
                slug = jsonl_file.parent.name
            if slug not in self._slug_cache:
                self._slug_cache[slug] = _project_from_cc_slug(slug)
            project_name = self._slug_cache[slug]

        self._record_discovery("claude-code", "indexed")
        return session_id, _FileRef(
            path=jsonl_file,
            agent=agent,
            mtime=mtime,
            project_name=project_name,
            cwd=cwd,
            branch=_branch_from_cc_head(head),
            home=home,
        )

    def _index_codex_file(
        self,
        jsonl_file: Path,
        *,
        home: Path,
        imported_thread_ids: frozenset[str] = frozenset(),
    ) -> tuple[str, _FileRef] | None:
        """Index one Codex JSONL file.

        Args:
            jsonl_file: the rollout to index.
            home: the Codex home it was found under.
            imported_thread_ids: `_codex_imported_thread_ids(home)`, read once
                per home by the caller. A rollout named for one of these that
                Codex never ran a turn in is another agent's session, already
                captured under that agent, and is skipped.
        """
        if not jsonl_file.is_file():
            # See _index_cc_file's identical guard: a directory/broken symlink
            # is never tallied, only a genuinely empty real file is.
            return None
        size = jsonl_file.stat().st_size
        if size == 0:
            self._record_discovery("codex", "empty_or_unreadable")
            return None
        try:
            head = _read_head(jsonl_file, stop_when=_agent_detected)
        except OSError:
            self._record_discovery("codex", "empty_or_unreadable")
            return None

        agent = detect_agent(head)
        if agent is None:
            logger.warning(
                "agent detection failed for %s (%d bytes); session skipped",
                jsonl_file,
                size,
            )
            self._record_discovery("codex", "detection_failed")
            return None
        if agent != AgentType.CODEX:
            self._record_discovery("codex", "different_agent")
            return None

        if _codex_is_uncontinued_import(jsonl_file, imported_thread_ids):
            self._record_discovery("codex", "imported_not_continued")
            return None
        stem = jsonl_file.stem
        session_id = f"local:{agent}:{stem}"
        mtime = datetime.fromtimestamp(jsonl_file.stat().st_mtime, tz=UTC)

        cwd = _cwd_from_codex_head(head)
        project_name = _project_from_cwd(cwd) if cwd else None

        self._record_discovery("codex", "indexed")
        return session_id, _FileRef(
            path=jsonl_file,
            agent=agent,
            mtime=mtime,
            project_name=project_name,
            cwd=cwd,
            home=home,
        )

    def _index_opencode_file(
        self,
        jsonl_file: Path,
        *,
        home: Path,
    ) -> tuple[str, _FileRef] | None:
        """Index one materialized opencode NDJSON transcript."""
        if not jsonl_file.is_file():
            # See _index_cc_file's identical guard: a directory/broken symlink
            # is never tallied, only a genuinely empty real file is.
            return None
        size = jsonl_file.stat().st_size
        if size == 0:
            self._record_discovery("opencode", "empty_or_unreadable")
            return None
        try:
            head = _read_head(jsonl_file, stop_when=_agent_detected)
        except OSError:
            self._record_discovery("opencode", "empty_or_unreadable")
            return None

        agent = detect_agent(head)
        if agent is None:
            logger.warning(
                "agent detection failed for %s (%d bytes); session skipped",
                jsonl_file,
                size,
            )
            self._record_discovery("opencode", "detection_failed")
            return None
        if agent != AgentType.OPENCODE:
            # A different agent's file under an opencode home (or a stray
            # `.jsonl`) — skip; the matching indexer, if any, owns it.
            self._record_discovery("opencode", "different_agent")
            return None

        stem = jsonl_file.stem
        session_id = f"local:{agent}:{stem}"
        mtime = datetime.fromtimestamp(jsonl_file.stat().st_mtime, tz=UTC)

        # cwd (from the assistant message's `path.cwd`) is machine-independent,
        # so it survives copying - it drives project_name, the repo resolver's
        # manifest tier, and Conductor-workspace detection.
        cwd = _cwd_from_opencode_head(head)
        project_name = _project_from_cwd(cwd) if cwd else None

        self._record_discovery("opencode", "indexed")
        return session_id, _FileRef(
            path=jsonl_file,
            agent=agent,
            mtime=mtime,
            project_name=project_name,
            cwd=cwd,
            home=home,
        )

    def _index_kiro_file(
        self,
        jsonl_file: Path,
        *,
        home: Path,
    ) -> tuple[str, _FileRef] | None:
        """Index one materialized Kiro NDJSON transcript."""
        if not jsonl_file.is_file():
            # See _index_cc_file's identical guard: a directory/broken symlink
            # is never tallied, only a genuinely empty real file is.
            return None
        size = jsonl_file.stat().st_size
        if size == 0:
            self._record_discovery("kiro", "empty_or_unreadable")
            return None
        try:
            head = _read_head(jsonl_file, stop_when=_agent_detected)
        except OSError:
            self._record_discovery("kiro", "empty_or_unreadable")
            return None

        agent = detect_agent(head)
        if agent is None:
            logger.warning(
                "agent detection failed for %s (%d bytes); session skipped",
                jsonl_file,
                size,
            )
            self._record_discovery("kiro", "detection_failed")
            return None
        if agent != AgentType.KIRO:
            # A different agent's file under a Kiro home (or a stray `.jsonl`)
            # — skip; the matching indexer, if any, owns it.
            self._record_discovery("kiro", "different_agent")
            return None

        if _kiro_event_count(head) == 0:
            logger.debug("kiro: %s has zero events; session skipped", jsonl_file)
            self._record_discovery("kiro", "empty_events")
            return None

        stem = jsonl_file.stem
        session_id = f"local:{agent}:{stem}"
        mtime = datetime.fromtimestamp(jsonl_file.stat().st_mtime, tz=UTC)

        # cwd comes from the header's `workspacePaths[0]` — machine-independent,
        # so it survives copying and drives project_name + the repo resolver's
        # manifest tier, same as every other agent.
        cwd = _cwd_from_kiro_head(head)
        project_name = _project_from_cwd(cwd) if cwd else None

        self._record_discovery("kiro", "indexed")
        return session_id, _FileRef(
            path=jsonl_file,
            agent=agent,
            mtime=mtime,
            project_name=project_name,
            cwd=cwd,
            home=home,
        )

    def _index_gemini_file(
        self,
        jsonl_file: Path,
        *,
        home: Path,
    ) -> tuple[str, _FileRef] | None:
        """Index one Gemini CLI session JSONL."""
        if not jsonl_file.is_file():
            # See _index_cc_file's identical guard: a directory/broken symlink
            # is never tallied, only a genuinely empty real file is.
            return None
        size = jsonl_file.stat().st_size
        if size == 0:
            self._record_discovery("gemini-cli", "empty_or_unreadable")
            return None
        # `build_map` refuses the same cap with SkipSession("too_large"). Skip
        # here, before any read, so discovery does not allocate the file.
        if size > MAX_SOURCE_BYTES:
            logger.debug("gemini-cli: %s exceeds the source byte cap; session skipped", jsonl_file)
            self._record_discovery("gemini-cli", "too_large")
            return None
        try:
            head = _read_head(jsonl_file, stop_when=_agent_detected)
        except OSError:
            self._record_discovery("gemini-cli", "empty_or_unreadable")
            return None

        agent = detect_agent(head)
        if agent is None:
            logger.warning(
                "agent detection failed for %s (%d bytes); session skipped",
                jsonl_file,
                size,
            )
            self._record_discovery("gemini-cli", "detection_failed")
            return None
        if agent != AgentType.GEMINI_CLI:
            self._record_discovery("gemini-cli", "different_agent")
            return None

        try:
            has_messages = _gemini_has_messages(jsonl_file)
        except OSError:
            self._record_discovery("gemini-cli", "empty_or_unreadable")
            return None
        if not has_messages:
            logger.debug("gemini-cli: %s has no messages; session skipped", jsonl_file)
            self._record_discovery("gemini-cli", "empty_events")
            return None

        header = _gemini_header(head)
        native_id = (header or {}).get("sessionId") or jsonl_file.stem
        session_id = f"local:{agent}:{native_id}"
        mtime = datetime.fromtimestamp(jsonl_file.stat().st_mtime, tz=UTC)

        cwd = _cwd_from_gemini_project(jsonl_file, header)
        project_name = _project_from_cwd(cwd) if cwd else None

        self._record_discovery("gemini-cli", "indexed")
        return session_id, _FileRef(
            path=jsonl_file,
            agent=agent,
            mtime=mtime,
            project_name=project_name,
            cwd=cwd,
            home=home,
        )

    def _index_copilot_file(
        self,
        jsonl_file: Path,
        *,
        home: Path,
    ) -> tuple[str, _FileRef] | None:
        """Index one Copilot CLI ``events.jsonl`` (or legacy flat) transcript."""
        if not jsonl_file.is_file():
            # See _index_cc_file's identical guard: a directory/broken symlink
            # is never tallied, only a genuinely empty real file is.
            return None
        size = jsonl_file.stat().st_size
        if size == 0:
            self._record_discovery("copilot", "empty_or_unreadable")
            return None
        try:
            head = _read_head(jsonl_file, stop_when=_agent_detected)
        except OSError:
            self._record_discovery("copilot", "empty_or_unreadable")
            return None

        agent = detect_agent(head)
        if agent is None:
            logger.warning(
                "agent detection failed for %s (%d bytes); session skipped",
                jsonl_file,
                size,
            )
            self._record_discovery("copilot", "detection_failed")
            return None
        if agent != AgentType.COPILOT:
            # A different agent's file under a Copilot home (or a stray `.jsonl`)
            # is skipped; the matching indexer, if any, owns it.
            self._record_discovery("copilot", "different_agent")
            return None

        start = _copilot_session_start(head)
        # Copilot creates the session directory (and often the event log with
        # just `session.start` + a model-change line) the moment the binary
        # starts, so a large share of on-disk sessions never held a prompt.
        # Skip those when the head IS the whole file; a file longer than the
        # head is indexed regardless, since its conversation sits further down.
        if not _copilot_has_conversation(head) and len(head) >= jsonl_file.stat().st_size:
            logger.debug("copilot: %s has no conversation events; session skipped", jsonl_file)
            self._record_discovery("copilot", "no_conversation")
            return None

        session_id = f"local:{agent}:{_copilot_session_key(start, jsonl_file)}"
        mtime = datetime.fromtimestamp(jsonl_file.stat().st_mtime, tz=UTC)

        # cwd / branch come from the `session.start` context (machine-independent:
        # Copilot records the real directory the developer ran it in), with the
        # legacy folder-trust notice as the fallback for builds that wrote no
        # context. Drives project_name + the repo resolver, as for every agent.
        cwd = _cwd_from_copilot_head(head, start)
        if cwd is None:
            # The detection-sized head stops as soon as `session.start` is seen,
            # and a legacy log records its cwd on a LATER `session.info`
            # folder-trust line. Re-read, bounded, until a cwd-bearing record
            # shows up so long legacy transcripts keep their repo attribution.
            try:
                longer = _read_head(
                    jsonl_file,
                    max_bytes=_COPILOT_CWD_SCAN_MAX_BYTES,
                    stop_when=_copilot_cwd_present,
                )
            except OSError:
                longer = head
            cwd = _cwd_from_copilot_head(longer, start)
        project_name = _project_from_cwd(cwd) if cwd else None

        self._record_discovery("copilot", "indexed")
        return session_id, _FileRef(
            path=jsonl_file,
            agent=agent,
            mtime=mtime,
            project_name=project_name,
            cwd=cwd,
            branch=_branch_from_copilot_head(start),
            home=home,
        )

    def _index_copilot_vscode_file(
        self,
        session_file: Path,
        *,
        home: Path,
    ) -> tuple[str, _FileRef] | None:
        """Index one Copilot-in-VS-Code chat session (``.jsonl`` snapshot+patch
        log, or a ``.json`` single document).

        Cheap head-only peek for the stable ``sessionId`` — the full
        snapshot+patch replay (``parse_copilot_vscode``) is deferred
        to actual parse time, same division of labour as every other indexer
        here. ``detect_agent`` only covers the ``.jsonl`` shape (see its
        docstring); the ``.json`` shape is confirmed directly, since a
        pretty-printed document has no single self-contained JSON line for
        that line-oriented scanner to match.
        """
        if not session_file.is_file():
            return None
        if session_file.stat().st_size == 0:
            self._record_discovery("copilot-vscode", "empty_or_unreadable")
            return None
        try:
            if session_file.suffix == ".jsonl":
                # Only the FIRST line matters (the kind=0 snapshot); the
                # default head is always enough for that regardless of the
                # rest of the file's length.
                head = _read_head(session_file)
            else:
                # .json is one whole document -- `_read_head`'s DEFAULT
                # behaviour (no `stop_when`) stops as soon as 20 lines have
                # accumulated, which truncates a pretty-printed document mid-
                # object for any session past a few requests. Forcing
                # `stop_when` false disables that early stop, so this reads to
                # EOF (the common case) or the same 5MB cap every other
                # agent's head-read already uses, not a silently truncated
                # blob `json.loads` would then always fail on.
                head = _read_head(session_file, stop_when=lambda _buf: False)
        except OSError:
            self._record_discovery("copilot-vscode", "empty_or_unreadable")
            return None

        if session_file.suffix == ".jsonl":
            agent = detect_agent(head)
            if agent is None:
                self._record_discovery("copilot-vscode", "detection_failed")
                return None
            if agent != AgentType.COPILOT_VSCODE:
                # A different agent's `.jsonl` under a shared VS Code home
                # (or a stray file) is skipped; the matching indexer, if
                # any, owns it.
                self._record_discovery("copilot-vscode", "different_agent")
                return None
            raw_session_id = _copilot_vscode_jsonl_session_id(head)
        else:
            # .json: not JSONL at all -- read and confirm the whole document
            # shape directly rather than forcing it through detect_agent. A
            # `requests: []` document is a genuinely empty "empty window"
            # session that was abandoned before any real message -- unlike an
            # empty `.jsonl` (which may just be mid-conversation, patches
            # still to come), this is FINAL state, so skip it here rather
            # than indexing something the parser will always emit zero spans
            # for and the pipeline then records as a failed session.
            raw_session_id, has_requests = _copilot_vscode_json_session_id(head)
            if raw_session_id is None:
                self._record_discovery("copilot-vscode", "different_agent")
                return None
            if not has_requests:
                self._record_discovery("copilot-vscode", "no_conversation")
                return None
            agent = AgentType.COPILOT_VSCODE

        if not raw_session_id:
            self._record_discovery("copilot-vscode", "detection_failed")
            return None
        # Namespaced like every other local indexer (`local:<agent>:<id>`) --
        # a bare sessionId would collide across agents and break the
        # session-id contract.
        session_id = f"local:{agent}:{raw_session_id}"

        mtime = datetime.fromtimestamp(session_file.stat().st_mtime, tz=UTC)
        self._record_discovery("copilot-vscode", "indexed")
        return session_id, _FileRef(
            path=session_file,
            agent=agent,
            mtime=mtime,
            # No cwd/repo resolution yet (the documented known gap: VS
            # Code stores the workspace path in a sibling workspace.json this
            # indexer doesn't read) -- project_name/cwd/branch stay None,
            # same as any other agent's session with no resolvable repo.
            home=home,
        )

    def _index_cursor_store_file(
        self,
        jsonl_file: Path,
        *,
        home: Path,
    ) -> tuple[str, _FileRef] | None:
        """Index one materialized Cursor CLI/agent NDJSON transcript."""
        if not jsonl_file.is_file():
            # See _index_cc_file's identical guard: a directory/broken symlink
            # is never tallied, only a genuinely empty real file is.
            return None
        size = jsonl_file.stat().st_size
        if size == 0:
            self._record_discovery("cursor", "empty_or_unreadable")
            return None
        # `agent-transcripts/<uuid>/subagents/*.jsonl` are component pieces of
        # the parent session, not standalone sessions — indexing them as their
        # own session would inflate session/turn/tool metrics. Skip them; the parent
        # `<uuid>/<uuid>.jsonl` transcript still indexes normally. Check the
        # HOME-RELATIVE parts only: a configured Cursor home (or any ancestor)
        # could itself be named `subagents`, which must not drop real sessions.
        if "subagents" in jsonl_file.relative_to(home).parts:
            self._record_discovery("cursor", "subagent_component")
            return None
        try:
            head = _read_head(jsonl_file, stop_when=_agent_detected)
        except OSError:
            self._record_discovery("cursor", "empty_or_unreadable")
            return None

        agent = detect_agent(head)
        if agent is None:
            # Only a head that is the whole file can prove there is no conversation;
            # a capped head may stop before the first message.
            if len(head) >= size and _is_metadata_only(head):
                logger.debug("no conversation in %s (%d bytes); session skipped", jsonl_file, size)
                self._record_discovery("cursor", "no_conversation")
                return None
            logger.warning(
                "agent detection failed for %s (%d bytes); session skipped",
                jsonl_file,
                size,
            )
            self._record_discovery("cursor", "detection_failed")
            return None
        if agent != AgentType.CURSOR:
            # A different agent's file under a Cursor home (or a stray
            # `.jsonl`) — skip; the matching indexer, if any, owns it.
            self._record_discovery("cursor", "different_agent")
            return None

        stem = jsonl_file.stem
        session_id = f"local:{agent}:{stem}"
        mtime = datetime.fromtimestamp(jsonl_file.stat().st_mtime, tz=UTC)

        # Cursor has two local JSONL shapes under the same home + `cursor` label.
        # The store.db-derived NDJSON opens with a `cursor_store_session` header;
        # the `agent-transcripts/*.jsonl` narrative has none. Route each to its
        # parser via the `subformat` discriminator (both stay `AgentType.CURSOR`).
        if not _has_cursor_store_header(head):
            # Agent-transcript: no header, no cwd/tokens/timestamps in the source.
            # Derive project_name from the `projects/<slug>/agent-transcripts/…`
            # path slug (same decoder Claude Code uses for its `projects/<slug>`).
            slug: str | None = None
            parts = jsonl_file.parts
            if "agent-transcripts" in parts:
                # Use the INNERMOST `agent-transcripts` component — a configured
                # Cursor home path could itself contain an earlier one, and
                # `parts.index` would then pick the wrong (outer) slug.
                idx = len(parts) - 1 - parts[::-1].index("agent-transcripts")
                if idx >= 1:
                    slug = parts[idx - 1]
            project_name = None
            if slug:
                if slug not in self._slug_cache:
                    self._slug_cache[slug] = _project_from_cc_slug(slug)
                project_name = self._slug_cache[slug]
            self._record_discovery("cursor", "indexed")
            return session_id, _FileRef(
                path=jsonl_file,
                agent=agent,
                mtime=mtime,
                project_name=project_name,
                home=home,
                subformat="agent_transcript",
            )

        # A store.db session with zero messages after the header has nothing to
        # index - `find_cursor_store_candidates` already refuses to
        # materialize these, but skip defensively rather than let
        # `parse_cursor_store` yield an empty span list (which the pipeline
        # would count as a FAILED session, not a skip).
        if _cursor_store_message_count(head) == 0:
            logger.debug("cursor_store: %s has zero messages; session skipped", jsonl_file)
            self._record_discovery("cursor", "empty_store_session")
            return None

        # cwd (from the header's `cwd` field) is machine-independent, so it
        # survives copying - it drives project_name and the repo resolver's
        # manifest tier, same as every other agent's cwd.
        cwd = _cwd_from_cursor_store_head(head)
        project_name = _project_from_cwd(cwd) if cwd else None

        self._record_discovery("cursor", "indexed")
        return session_id, _FileRef(
            path=jsonl_file,
            agent=agent,
            mtime=mtime,
            project_name=project_name,
            cwd=cwd,
            home=home,
        )

    def discover_sessions(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> list[DiscoveredSession]:
        index = self._build_index()
        results = []

        for session_id, ref in index.items():
            # Metadata can arrive after a completed transcript checkpoint.
            # Snapshot validation still happens against the exact parsed bytes.
            try:
                sidecar_time = datetime.fromtimestamp(
                    ref.path.with_suffix(".metadata.json").stat().st_mtime, tz=UTC
                )
            except OSError:
                # One inaccessible optional sidecar must not block discovery
                # of the other sessions. Reading selected sessions still
                # validates metadata and reports access failures for retry.
                pass
            else:
                ref.mtime = max(ref.mtime, sidecar_time)
            if start_time and ref.mtime < start_time:
                continue
            if end_time and ref.mtime > end_time:
                continue

            # Rough span-count estimate: ~500 bytes per line.
            file_size = ref.path.stat().st_size
            estimated_spans = max(file_size // 500, 1)

            results.append(
                DiscoveredSession(
                    session_id=session_id,
                    span_count=estimated_spans,
                    last_span_time=ref.mtime,
                    start_time=ref.mtime,
                    end_time=ref.mtime,
                )
            )

        results.sort(key=lambda s: s.start_time, reverse=True)
        return results

    def get_spans_by_session(self, session_id: str) -> list[SpanData]:
        index = self._build_index()
        ref = index.get(session_id)
        if not ref:
            logger.debug("Session not found in index: %s", session_id)
            return []

        try:
            data = ref.path.read_bytes()
        except FileNotFoundError:
            # The file moved after the index was built -- typically a Codex
            # rollout archived from sessions/YYYY/MM/DD/ to archived_sessions/
            # mid-run. Both locations are indexed, so rebuild the index once
            # and re-resolve the session. Still missing = moved or deleted: an
            # expected race, not an error (no traceback, nothing captured).
            self._file_index = None
            # Report the rebuilt walk only, not both walks added together.
            self._discovery_stats = {}
            ref = self._build_index().get(session_id)
            try:
                if ref is None:
                    raise FileNotFoundError(session_id)
                data = ref.path.read_bytes()
            except FileNotFoundError:
                logger.info("Session file moved or deleted: %s", session_id)
                return []
            except OSError as exc:
                logger.warning(
                    "Failed to read session file: %s", exc, exc_info=logger.isEnabledFor(logging.DEBUG)
                )
                return []
        except OSError as exc:
            logger.warning("Failed to read session file: %s", exc, exc_info=logger.isEnabledFor(logging.DEBUG))
            return []

        # Verify against the exact parent bytes being parsed, before appending
        # subagents. A partially written transcript/metadata pair must remain retryable rather
        # than applying a new repository to an older transcript (or vice versa).
        try:
            metadata = json.loads(ref.path.with_suffix(".metadata.json").read_bytes())
        except FileNotFoundError:
            metadata = None
        else:
            if not isinstance(metadata, dict) or (
                metadata.get("repo_name") is not None and not isinstance(metadata["repo_name"], str)
            ):
                raise ValueError("Invalid repository metadata sidecar; retry")
        if isinstance(metadata, dict) and "transcript_sha256" in metadata:
            if metadata["transcript_sha256"] != hashlib.sha256(data).hexdigest():
                raise ValueError("Transcript and repository metadata snapshot mismatch; retry")

        self._branch_history[session_id] = _branch_metadata(data, ref.agent)

        if ref.agent == AgentType.CLAUDE_CODE:
            data = _with_claude_code_subagents(
                data,
                ref.path,
                related_parents=tuple(self._claude_code_families.get(session_id, ())),
            )

        # Bail before parsing a pathologically large transcript: parsing it
        # can exhaust memory. Callers get OversizedSessionError instead.
        if len(data) > MAX_SESSION_BYTES:
            raise OversizedSessionError(
                f"{ref.path.name}: {len(data) // (1024 * 1024)} MB "
                f"exceeds {MAX_SESSION_BYTES // (1024 * 1024)} MB cap"
            )

        # The OS user running the scan: the operator is the developer.
        user_id = os.environ.get("USER") or Path.home().name

        parsers = {
            AgentType.CLAUDE_CODE: parse_claude_code,
            AgentType.CODEX: parse_codex,
            AgentType.COPILOT: parse_copilot,
            AgentType.COPILOT_VSCODE: parse_copilot_vscode,
            AgentType.OPENCODE: parse_opencode,
            AgentType.CURSOR: parse_cursor_store,
            AgentType.KIRO: parse_kiro,
            AgentType.GEMINI_CLI: parse_gemini,
            AgentType.ANTIGRAVITY_CLI: parse_antigravity,
        }

        # Cursor's `agent-transcripts` narrative shares the `cursor` agent label
        # but needs its own parser (no header/tokens/timestamps).
        if ref.subformat == "agent_transcript":
            parser = parse_cursor_agent_transcript
        else:
            parser = parsers.get(ref.agent)
        if not parser:
            logger.debug("No parser for agent type: %s", ref.agent)
            return []

        _, spans = parser(
            data=data,
            trace_id=session_id,
            user_id=user_id,
            project_name=ref.project_name,
        )
        if ref.agent in (AgentType.GEMINI_CLI, AgentType.ANTIGRAVITY_CLI) and ref.cwd:
            # Gemini CLI's transcript records no cwd; the indexer read the
            # project root from `.project_root`. Every call ran there.
            # Antigravity CLI's cwd comes from `conversation_summaries.db`.
            for span in spans:
                if span.parent_id is not None and not span.attributes.get("cwd"):
                    span.attributes["cwd"] = ref.cwd
        branch, branches = self._branch_history[session_id]
        root = next((span for span in spans if span.parent_id is None), None)
        if root is not None:
            root.attributes.update({"git.branch": branch, "git.branches": branches})
            if ref.parent_session_id:  # a subagent stored as its own conversation (Antigravity CLI)
                root.attributes["harness.session_role"] = "subagent"
                root.attributes["harness.parent_session_id"] = ref.parent_session_id
            # A cloud-run session stores selected-repository metadata in a sidecar
            # beside the unmodified transcript, whose remote cwd may be absent.
            repo = metadata.get("repo_name") if isinstance(metadata, dict) else None
            if isinstance(repo, str):
                root.attributes["harness.repo_name"] = repo
            if isinstance(metadata, dict) and "transcript_sha256" in metadata:
                cloud_ids = metadata.get("claude_cloud_session_ids")
                if (
                    ref.agent == AgentType.CLAUDE_CODE
                    and isinstance(cloud_ids, list)
                    and len(cloud_ids) <= 100
                    and all(isinstance(value, str) and re.fullmatch(r"session_[A-Za-z0-9]{1,128}", value) for value in cloud_ids)
                ):
                    root.attributes["harness.claude_cloud_session_ids"] = sorted(set(cloud_ids))
        return spans

    def get_agent_type(self, session_id: str) -> str | None:
        """Return the agent that produced this session ('claude-code', 'codex', ...)."""
        index = self._build_index()
        ref = index.get(session_id)
        return ref.agent if ref else None

    def get_project_name(self, session_id: str) -> str | None:
        """Return the project name derived during indexing."""
        index = self._build_index()
        ref = index.get(session_id)
        return ref.project_name if ref else None

    def get_cwd(self, session_id: str) -> str | None:
        """Return the transcript's working directory, if captured at indexing.

        The machine-independent project anchor (Claude Code). Used by the
        repo resolver to tie a session to its own repo. None for sources
        that don't record a cwd in the head (e.g. Codex today).
        """
        index = self._build_index()
        ref = index.get(session_id)
        return ref.cwd if ref else None

    def get_branch(self, session_id: str) -> str | None:
        """Latest recorded branch; HEAD explicitly records a detached checkout."""
        self.get_branches(session_id)
        return self._branch_history.get(session_id, (None, []))[0]

    def get_branches(self, session_id: str) -> list[str]:
        """Distinct named branches from the selected original transcript copy."""
        if session_id not in self._branch_history:
            ref = self._build_index().get(session_id)
            if ref:
                self._branch_history[session_id] = _branch_metadata(
                    ref.path.read_bytes(), ref.agent
                )
        return list(self._branch_history.get(session_id, (None, []))[1])

    def get_manifest_repo(self, session_id: str) -> str | None:
        """Canonical `owner/repo` for this session from the manifest stamp.

        Resolution: session → its home → that home's `cwd_repos` map →
        the session's cwd. Returns None when the manifest carries no repo
        for this cwd - the resolver then falls through to its produced-PR /
        git-remote / local-git tiers.
        """
        index = self._build_index()
        ref = index.get(session_id)
        if not ref or not ref.home or not ref.cwd:
            return None
        return self._home_repos.get(ref.home, {}).get(ref.cwd)

    def get_cwd_repos(self, session_id: str) -> dict[str, str]:
        """The full `cwd -> 'owner/repo'` map for this session's home.

        The per-task repo resolver needs every cwd→repo entry (not just
        the session's own cwd) because one session can span several repos
        and each task resolves against its own dominant cwd. Resolution:
        session → its home → that home's `cwd_repos` map. Empty dict when
        the home had no manifest stamp.
        """
        index = self._build_index()
        ref = index.get(session_id)
        if not ref or not ref.home:
            return {}
        return self._home_repos.get(ref.home, {})

    def get_repo_roots(self, session_id: str) -> dict[str, str]:
        """The `git_toplevel -> 'owner/repo'` map for this session's home.

        Per-task multi-repo attribution prefix-matches each task's touched
        files against these roots (the only git-truth file→repo signal
        available at scan time). Empty dict when the home's manifest carries
        neither `repo_roots` nor a derivable `cwd_repos`.
        """
        index = self._build_index()
        ref = index.get(session_id)
        if not ref or not ref.home:
            return {}
        return self._home_roots.get(ref.home, {})


class _FileRef:
    """Internal pointer: session_id → file path + index-time metadata."""

    __slots__ = ("agent", "branch", "cwd", "home", "mtime", "parent_session_id", "path", "project_name", "subformat")

    def __init__(
        self,
        path: Path,
        agent: AgentType,
        mtime: datetime,
        project_name: str | None = None,
        cwd: str | None = None,
        branch: str | None = None,
        home: Path | None = None,
        subformat: str | None = None,
        parent_session_id: str | None = None,
    ):
        self.path = path
        self.agent = agent
        self.mtime = mtime
        self.project_name = project_name
        # Sub-format discriminator within an agent that has more than one local
        # shape. Currently only Cursor: None = store.db NDJSON (`parse_cursor_store`),
        # "agent_transcript" = the `agent-transcripts/*.jsonl` narrative
        # (`parse_cursor_agent_transcript`). Lets the parse step pick the right
        # parser while both keep `agent == AgentType.CURSOR` (one agent label).
        self.subformat = subformat
        # Full working directory from the transcript (Claude Code). The
        # machine-independent ground truth for project_name, and the
        # anchor the repo resolver uses to tie a session to its own repo.
        self.cwd = cwd
        # Git branch the session ran on (Claude Code `gitBranch`).
        self.branch = branch
        # The home dir under which this file was discovered — used to
        # look up the manifest identity for that home.
        self.home = home
        # Session id of the conversation that spawned this one (Antigravity CLI subagents).
        self.parent_session_id = parent_session_id


__all__ = ["LocalSource", "ManifestIdentity"]
