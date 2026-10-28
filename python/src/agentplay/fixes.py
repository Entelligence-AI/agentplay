"""Writing fixes into a repo: the only place agentplay writes outside ``~/.agentplay``.

Rules, all enforced here rather than by callers:

* Only a repo the person granted (``grants.json``), at the root recorded for
  it, and only when that root still holds ``.git``.
* Only four files: ``CLAUDE.md``, ``CLAUDE.local.md``, ``.claude/settings.json``
  and ``.claude/settings.local.json``, resolved inside the root (a symlink
  that points elsewhere is refused).
* Instructions go between ``<!-- agentplay:start -->`` and ``<!-- agentplay:end -->``
  markers; text outside them is never touched. Settings are merged, never
  overwritten, and a file that is not valid JSON is left alone.
* Refused when the file changed since the preview (``expect_hash``).
* Every write is journalled in ``applied.json`` so it can be undone.

Fix text is built only from command verbs, repo-relative paths and counts
(``health.py``), so a fix cannot carry redacted content into a repo.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .store import OutsideRoot, _atomic_write

ALLOWED_FILES = ("CLAUDE.md", "CLAUDE.local.md", ".claude/settings.json", ".claude/settings.local.json")
# Claude Code's own settings, outside any repo. A fix to a hook registered there is written under this name,
# with the Claude home as its root; the same grant, journal and undo apply, minus the git checkout rule.
HOME_REPO = "~/.claude"
HOME_FILES = ("settings.json", "settings.local.json")
START = "<!-- agentplay:start -->"
END = "<!-- agentplay:end -->"
HEADING = "## Notes from agentplay"


class FixError(Exception):
    """A write that was refused; ``code`` is what the API and CLI report."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ------------------------------------------------------------------ locking

_held = threading.local()
# Threads of one process: flock already separates them (one open file each), but
# msvcrt locks belong to the process, so Windows needs this too.
_process_lock = threading.Lock()


@contextmanager
def write_lock(base: Path) -> Iterator[None]:
    """Serialize writers (server threads, the CLI and the SessionStart hook) on ``base/write.lock``.

    Re-entrant within a thread, so ``autotune.run`` can hold it across the
    ``apply`` calls it makes.
    """
    depth = getattr(_held, "depth", 0)
    if depth:
        _held.depth = depth + 1
        try:
            yield
        finally:
            _held.depth -= 1
        return
    base.mkdir(parents=True, exist_ok=True)
    with _process_lock, open(base / "write.lock", "a+b") as fh:
        _lock_file(fh, True)
        _held.depth = 1
        try:
            yield
        finally:
            _held.depth = 0
            _lock_file(fh, False)


def _lock_file(fh: Any, on: bool) -> None:
    try:
        import fcntl
    except ImportError:  # Windows
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK if on else msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX if on else fcntl.LOCK_UN)


# ------------------------------------------------------------------ local files


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, value: Any) -> None:
    _atomic_write(path, json.dumps(value, indent=1).encode("utf-8"))


def known_roots(base: Path) -> dict[str, str]:
    """Repo name -> the checkout most recently seen for it (``repos.json``, written by builds)."""
    raw = _read_json(base / "repos.json", {})
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def record_roots(base: Path, roots: dict[str, str]) -> None:
    if not roots:
        return
    cur = known_roots(base)
    if all(cur.get(k) == v for k, v in roots.items()):
        return
    cur.update(roots)
    _write_json(base / "repos.json", cur)


def grants(base: Path) -> dict[str, str]:
    raw = _read_json(base / "grants.json", {})
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def grant(base: Path, repo: str, root: str | None = None) -> str:
    """Allow writes to ``repo``. Uses the root builds recorded unless ``root`` is given."""
    path = root or known_roots(base).get(repo)
    if not path:
        raise FixError("no_root", f"agentplay has not seen a checkout of {repo} on this machine.")
    resolved = Path(path).expanduser().resolve()
    if repo == HOME_REPO:
        if not resolved.is_dir():
            raise FixError("no_root", f"{resolved} is not a folder.")
    elif not (resolved / ".git").exists():
        raise FixError("not_a_repo", f"{resolved} is not a git checkout.")
    with write_lock(base):
        g = grants(base)
        g[repo] = str(resolved)
        _write_json(base / "grants.json", g)
    return str(resolved)


def revoke(base: Path, repo: str) -> None:
    with write_lock(base):
        g = grants(base)
        if g.pop(repo, None) is not None:
            _write_json(base / "grants.json", g)


def journal(base: Path) -> list[dict[str, Any]]:
    raw = _read_json(base / "applied.json", [])
    return raw if isinstance(raw, list) else []


def _save_journal(base: Path, entries: list[dict[str, Any]]) -> None:
    _write_json(base / "applied.json", entries)


def active(base: Path) -> list[dict[str, Any]]:
    return [e for e in journal(base) if not e.get("undoneAt")]


# ------------------------------------------------------------------ paths


def _target(base: Path, repo: str, rel: str, root_s: str | None = None) -> Path:
    """``rel`` inside ``repo``'s granted checkout, or inside ``root_s`` (undo: the checkout it was written to;
    a preview: the root the fix found before any grant)."""
    allowed = HOME_FILES if repo == HOME_REPO else ALLOWED_FILES
    if rel not in allowed:
        raise FixError("file_not_allowed", f"agentplay only writes {', '.join(allowed)}.")
    root_s = root_s or grants(base).get(repo)
    if not root_s:
        raise FixError("not_granted", f"Allow writes to {repo} first.")
    root = Path(root_s).resolve()
    if repo != HOME_REPO and not (root / ".git").exists():
        raise FixError("not_a_repo", f"{root} is no longer a git checkout.")
    target = (root / rel).resolve()
    if not target.is_relative_to(root):
        raise FixError("outside_repo", f"{rel} points outside {root}.")
    return target


def file_hash(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _read(path: Path) -> str | None:
    # Bytes, not read_text: newline translation would turn a CRLF file into a whole-file rewrite.
    try:
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return None


def current(base: Path, repo: str, rel: str) -> str | None:
    return _read(_target(base, repo, rel))


def _write(path: Path, text: str, root: Path) -> None:
    # Confined at the write itself: a link swapped in after _target resolved must not lead outside the checkout.
    try:
        _atomic_write(path, text.encode("utf-8"), private=False, within=root)
    except OutsideRoot:
        raise FixError("outside_repo", f"{path.name} now points outside {root}. Nothing was written.") from None


def _unlink(path: Path, root: Path) -> None:
    if not Path(os.path.realpath(path)).is_relative_to(root):
        raise FixError("outside_repo", f"{path.name} now points outside {root}. Nothing was removed.")
    path.unlink(missing_ok=True)


def _newline(text: str) -> str:
    """The file's dominant line ending, so lines agentplay adds match the rest."""
    crlf = text.count("\r\n")
    return "\r\n" if crlf > text.count("\n") - crlf else "\n"


def _ignore_locally(root: Path, rel: str) -> None:
    """List a personal file agentplay just created in the repo's ``info/exclude`` so ``git add .`` skips it.

    Not ``.gitignore``: that file is shared and tracked. Not journalled: an ignore
    line for a personal file is harmless after undo. Only the literal anchored
    line is checked (no git subprocess), so a redundant entry is possible when a
    global ignore already covers the file.
    """
    try:
        git = root / ".git"
        if git.is_file():  # a linked worktree or submodule: ".git" holds "gitdir: <path>"
            line = git.read_text(encoding="utf-8").strip()
            if not line.startswith("gitdir:"):
                return
            git = (root / line[len("gitdir:"):].strip()).resolve()
            common = git / "commondir"  # worktrees share the main checkout's info/exclude
            if common.is_file():
                git = (git / common.read_text(encoding="utf-8").strip()).resolve()
        exclude = git / "info" / "exclude"
        text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if f"/{rel}" in text.splitlines():
            return
        sep = "" if not text or text.endswith("\n") else "\n"
        _atomic_write(exclude, f"{text}{sep}/{rel}\n".encode(), private=False)
    except (OSError, ValueError):
        pass  # The fix itself is written and undoable; a missing ignore line must not fail it.


# ------------------------------------------------------------------ edits


def _marker(fix_id: str) -> str:
    return f"<!-- agentplay:{fix_id} -->"


def add_lines(text: str | None, fix_id: str, lines: list[str], rel: str = "CLAUDE.md") -> str:
    """``text`` with ``lines`` added inside the managed block (created at the end if absent)."""
    text = text or ""
    nl = _newline(text)
    tagged = [f"- {line} {_marker(fix_id)}" for line in lines]
    starts, ends = text.count(START), text.count(END)
    if (starts, ends) == (1, 1) and text.index(START) < text.index(END):
        head, rest = text.split(START, 1)
        block, tail = rest.split(END, 1)
        kept = [ln.rstrip("\r") for ln in block.split("\n")]
        kept = [ln for ln in kept if ln.strip() and _marker(fix_id) not in ln]
        return f"{head}{START}{nl}" + nl.join(kept + tagged) + f"{nl}{END}{tail}"
    if starts or ends:
        # Guessing where a half-deleted block ends would swallow the person's own text.
        raise FixError(
            "malformed_block",
            f"The agentplay block in {rel} is broken: it needs one {START} line followed by one {END} line "
            f"(found {starts} and {ends}). Fix the markers by hand, or delete them along with the lines between, "
            "then try again.",
        )
    sep = "" if not text or text.endswith(nl * 2) else nl if text.endswith("\n") else nl * 2
    return f"{text}{sep}{HEADING}{nl}{nl}{START}{nl}" + nl.join(tagged) + f"{nl}{END}{nl}"


def remove_lines(text: str, fix_id: str) -> str:
    return "\n".join(ln for ln in text.split("\n") if _marker(fix_id) not in ln)


def _settings(text: str | None, rel: str) -> dict[str, Any]:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise FixError("bad_json", f"{rel} is not valid JSON; fix it by hand first.") from exc
    if not isinstance(value, dict):
        raise FixError("bad_json", f"{rel} is not a JSON object.")
    return value


def _one_line(texts: list[str]) -> None:
    # Fix text carries command verbs from maps, which an imported map controls: a line break
    # or other control character would smuggle in an instruction, rule or command undo cannot remove.
    if any(ch in "\x85  " or (ord(ch) < 32 and ch != "\t") or ord(ch) == 127 for t in texts for ch in t):
        raise FixError("bad_fix", "This fix text spans more than one line; agentplay will not write it.")


def _apply_text(action: dict[str, Any], text: str | None, fix_id: str, rel: str) -> str:
    kind = action.get("kind")
    if kind == "claude_md":
        lines = [str(x) for x in action.get("lines") or [] if str(x).strip()]
        if not lines:
            raise FixError("empty_fix", "This fix has nothing to write.")
        # A marker inside a line would break the managed block for every later apply and undo.
        if any("<!-- agentplay" in line for line in lines):
            raise FixError("bad_fix", "This fix text contains an agentplay marker; agentplay will not write it.")
        _one_line(lines)
        return add_lines(text, fix_id, lines, rel)
    data = _settings(text, rel)
    if kind == "settings_allow":
        allow = _field(_field(data, "permissions", dict, rel, "permissions"), "allow", list, rel, "permissions.allow")
        entry = str(action.get("entry") or "")
        _one_line([entry])
        if entry and entry not in allow:
            allow.append(entry)
    elif kind == "stop_hook":
        stop = hook_groups(data, rel, "Stop")
        cmd = str(action.get("command") or "")
        # A hook command runs in a shell: a line break would run a second command.
        _one_line([cmd])
        if not any(h.get("command") == cmd for g in stop for h in g.get("hooks", [])):
            stop.append({"hooks": [{"type": "command", "command": cmd}]})
    elif kind == "hook_event":
        # A hook answering the wrong event: its entry leaves the event it fails at and, unless it is already
        # registered there, joins the one it answers as.
        cmd, src, dst = str(action.get("command") or ""), str(action.get("from") or ""), action.get("to")
        _one_line([cmd])
        groups = hook_groups(data, rel, src, create=False)
        if not _runs(groups, cmd):
            raise FixError("already_present", f"{rel} no longer runs that command at {src}.")
        data["hooks"][src] = drop_hook(groups, cmd)
        if not data["hooks"][src]:
            del data["hooks"][src]
        if dst:
            to = hook_groups(data, rel, str(dst))
            if not _runs(to, cmd):
                to.append({"hooks": [{"type": "command", "command": cmd}]})
    else:
        raise FixError("unknown_action", "This fix cannot be written to a file.")
    return _dump(data, text)


def _runs(groups: list[Any], command: str) -> bool:
    return any(isinstance(h, dict) and h.get("command") == command
               for g in groups if isinstance(g, dict) for h in g.get("hooks", []))


def _dump(data: dict[str, Any], like: str | None) -> str:
    # In the file's own style, so the change is the entry and not every line: its indent, whether it writes
    # `"key" : value` (as Claude Code's own writer does), and whether it keeps non-ASCII characters as they are.
    like = like or ""
    indent = len(m.group(1)) if (m := re.search(r"^( +)\S", like, re.MULTILINE)) else 2
    separators = (",", " : ") if re.search(r'"\s+:\s', like) else (",", ": ")
    text = json.dumps(data, indent=indent, separators=separators, ensure_ascii=like.isascii())
    # JSON escapes newlines inside strings, so swapping every "\n" only changes line ends.
    return (text + "\n").replace("\n", _newline(like))


_JSON_TYPES = ((bool, "true/false"), (dict, "an object"), (list, "an array"), (str, "a string"), (type(None), "null"))


def _json_type(value: Any) -> str:
    return next((name for t, name in _JSON_TYPES if isinstance(value, t)), "a number")


def _field(data: dict[str, Any], key: str, kind: type, rel: str, where: str, create: bool = True) -> Any:
    """``data[key]``, created empty if absent; a value of another type is refused, never overwritten."""
    if key not in data:
        if not create:
            return kind()
        data[key] = kind()
    value = data[key]
    if not isinstance(value, kind):
        raise _shape_error(rel, where, value, _json_type(kind()))
    return value


def _shape_error(rel: str, where: str, value: Any, expected: str) -> FixError:
    return FixError("bad_json", f"{rel} has {where} as {_json_type(value)}; expected {expected}; fix it by hand first.")


def hook_groups(data: Any, rel: str, event: str, create: bool = True) -> list[Any]:
    """The ``hooks.<event>`` groups, each checked to be ``{"hooks": [{...}, ...]}``."""
    if not isinstance(data, dict):
        raise _shape_error(rel, "the top level", data, "an object")
    hooks = _field(data, "hooks", dict, rel, "hooks", create)
    groups = _field(hooks, event, list, rel, f"hooks.{event}", create)
    for i, group in enumerate(groups):
        where = f"hooks.{event}[{i}]"
        if not isinstance(group, dict):
            raise _shape_error(rel, where, group, "an object")
        for j, hook in enumerate(_field(group, "hooks", list, rel, f"{where}.hooks", create=False)):
            if not isinstance(hook, dict):
                raise _shape_error(rel, f"{where}.hooks[{j}]", hook, "an object")
    return groups


def drop_hook(groups: list[Any], command: Any) -> list[Any]:
    """``groups`` without the hook that runs ``command``; other hooks in a group stay, empty groups go."""
    out = []
    for g in groups:
        if not isinstance(g, dict):
            out.append(g)
            continue
        hooks = [h for h in g.get("hooks", []) if not (isinstance(h, dict) and h.get("command") == command)]
        if len(hooks) == len(g.get("hooks", [])):
            out.append(g)
        elif hooks:
            out.append({**g, "hooks": hooks})
    return out


# Containers agentplay may add to settings, parents first; the journal records which were absent.
_CONTAINERS = ("permissions", "permissions.allow", "hooks")


def _has(data: Any, dotted: str) -> bool:
    for part in dotted.split("."):
        if not isinstance(data, dict) or part not in data:
            return False
        data = data[part]
    return True


def _undo_text(entry: dict[str, Any], text: str) -> str | None:
    """``text`` without the fix; None when all that is left is what agentplay created, so the file can go."""
    kind, rel = entry["action"].get("kind"), entry["file"]
    if kind == "claude_md":
        text = remove_lines(text, entry["fixId"])
        return None if entry.get("created") and text.split() == f"{HEADING} {START} {END}".split() else text
    data = _settings(text, rel)
    if kind == "settings_allow":
        allow = _field(_field(data, "permissions", dict, rel, "permissions", False), "allow", list, rel,
                       "permissions.allow", False)
        if entry["action"].get("entry") in allow:
            allow.remove(entry["action"]["entry"])
    elif kind == "stop_hook":
        stop = hook_groups(data, rel, "Stop", create=False)
        if stop:
            data["hooks"]["Stop"] = drop_hook(stop, entry["action"].get("command"))
            if not data["hooks"]["Stop"]:
                del data["hooks"]["Stop"]
    elif kind == "hook_event":
        act = entry["action"]
        cmd, src, dst = str(act.get("command") or ""), str(act.get("from") or ""), act.get("to")
        back = hook_groups(data, rel, src)
        if not _runs(back, cmd):
            back.append({"hooks": [{"type": "command", "command": cmd}]})
        if dst:
            # Only ever added when it wasn't there, so taking it out restores the file.
            moved = hook_groups(data, rel, str(dst), create=False)
            if moved:
                data["hooks"][str(dst)] = drop_hook(moved, cmd)
                if not data["hooks"][str(dst)]:
                    del data["hooks"][str(dst)]
    for dotted in reversed(entry.get("added") or []):
        parent, _, key = dotted.rpartition(".")
        holder = data.get(parent) if parent else data
        if isinstance(holder, dict) and holder.get(key) in ({}, []):
            del holder[key]
    return None if entry.get("created") and not data else _dump(data, text)


def preview(base: Path, repo: str, fix: dict[str, Any], action: dict[str, Any], rel: str | None = None,
            root: str | None = None) -> dict[str, Any]:
    """The file as it is and as it would be, without writing. ``root`` lets a fix that knows its file show
    the change before the first grant."""
    rel = rel or str(action.get("file"))
    before = _read(_target(base, repo, rel, root))
    after = _apply_text(action, before, fix["id"], rel)
    return {"file": rel, "hash": file_hash(before), "before": before or "", "after": after}


def apply(
    base: Path,
    fix: dict[str, Any],
    action: dict[str, Any],
    *,
    rel: str | None = None,
    expect_hash: str | None = None,
    source: str = "button",
    baseline: Any = None,
) -> dict[str, Any]:
    """Write one action of ``fix`` into its repo and journal it. Returns the journal entry."""
    repo = str(fix.get("repo") or "")
    if not repo:
        raise FixError("no_repo", "This fix names an integration, not a repo; there is nothing to write.")
    rel = rel or str(action.get("file"))
    if action.get("kind") == "claude_md" and rel not in ("CLAUDE.md", "CLAUDE.local.md"):
        raise FixError("file_not_allowed", "Instructions go in CLAUDE.md or CLAUDE.local.md.")
    if action.get("kind") in ("settings_allow", "stop_hook", "hook_event"):
        if repo == HOME_REPO and rel not in HOME_FILES:
            raise FixError("file_not_allowed", "Claude Code's settings are settings.json or settings.local.json.")
        if repo != HOME_REPO and not rel.startswith(".claude/"):
            raise FixError("file_not_allowed", "Settings go in .claude/settings.json or .claude/settings.local.json.")
    with write_lock(base):
        return _apply_locked(base, fix, action, repo, rel, expect_hash, source, baseline)


def _apply_locked(
    base: Path, fix: dict[str, Any], action: dict[str, Any], repo: str, rel: str,
    expect_hash: str | None, source: str, baseline: Any,
) -> dict[str, Any]:
    entries = journal(base)
    if any(e.get("fixId") == fix["id"] and e.get("repo") == repo and e.get("file") == rel
           and not e.get("undoneAt") for e in entries):
        raise FixError("already_applied", f"This fix is already in {rel}. Undo it first to write it again.")
    target = _target(base, repo, rel)
    root = Path(grants(base)[repo]).resolve()
    before = _read(target)
    if expect_hash is not None and expect_hash != file_hash(before):
        raise FixError("changed", f"{rel} changed since the preview. Look again before applying.")
    after = _apply_text(action, before, fix["id"], rel)
    if after == before or (action.get("kind") != "claude_md" and _settings(before, rel) == _settings(after, rel)):
        # Already there (the person added it themselves): nothing to write, so nothing to undo later.
        raise FixError("already_present", f"{rel} already has this.")
    _write(target, after, root)
    entry = {
        "id": uuid.uuid4().hex[:10],
        "fixId": fix["id"],
        "rule": fix.get("rule"),
        "title": fix.get("title"),
        "repo": repo,
        "root": str(Path(grants(base)[repo]).resolve()),
        "file": rel,
        "action": action,
        "source": source,
        "metric": fix.get("metric"),
        "baseline": baseline,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        # So undo can remove what agentplay itself added and nothing the person had.
        "created": before is None,
    }
    if action.get("kind") != "claude_md":
        old, new = _settings(before, rel), _settings(after, rel)
        entry["added"] = [k for k in _CONTAINERS if _has(new, k) and not _has(old, k)]
    entries.append(entry)
    try:
        _save_journal(base, entries)
    except BaseException:
        # Never leave a repo change that cannot be undone.
        if before is None:
            _unlink(target, root)
        else:
            _write(target, before, root)
        raise
    if before is None and rel in ("CLAUDE.local.md", ".claude/settings.local.json"):
        _ignore_locally(root, rel)
    return entry


def undo(base: Path, entry_id: str, *, reason: str = "undone") -> dict[str, Any]:
    with write_lock(base):
        return _undo_locked(base, entry_id, reason)


def _undo_locked(base: Path, entry_id: str, reason: str) -> dict[str, Any]:
    entries = journal(base)
    entry = next((e for e in entries if e.get("id") == entry_id and not e.get("undoneAt")), None)
    if entry is None:
        raise FixError("not_found", "Nothing to undo with that id.")
    # The checkout it was written to, even if the grant has since moved to another one.
    target = _target(base, entry["repo"], entry["file"], entry.get("root"))
    root = Path(entry.get("root") or grants(base)[entry["repo"]]).resolve()
    text = _read(target)
    if text is not None:
        after = _undo_text(entry, text)
        if after is None:
            _unlink(target, root)
        else:
            _write(target, after, root)
    entry["undoneAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry["undoReason"] = reason
    _save_journal(base, entries)
    return entry


def status(base: Path) -> dict[str, Any]:
    """Repos agentplay can write to, and what it has written."""
    roots, g = known_roots(base), grants(base)
    names = sorted(set(roots) | set(g))
    return {
        "repos": [{"name": n, "root": g.get(n) or roots.get(n), "granted": n in g} for n in names],
        "applied": active(base),
    }


def home() -> Path:
    from .store import root

    return Path(os.environ.get("AGENTPLAY_HOME") or root())
