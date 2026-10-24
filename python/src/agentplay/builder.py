"""Session maps: a redacted, chronological replay of one coding-agent session.

A session map (schema ``agentplay/v1``) is what the Agentplay viewer
renders: every tool call, file touch, MCP call, skill, subagent
and LLM request of a session, in order, with repo-relative paths.

This module is **pure**: it reads local transcripts and returns a dict. It never
touches the network or the CLI's state files. Anything that goes wrong raises;
callers decide how soft to be.

Redaction contract (raw transcripts never leave the machine):

* no prompt / assistant text, no tool inputs or outputs, no file contents;
* shell commands contribute only their verb (``git log``, ``pytest``), never args;
* subagents contribute their type and model, never their prompt/description;
* file paths inside a git repo are repo-relative; paths outside any repo collapse
  to coarse buckets (``/tmp (scratch)``, ``~/Documents``, ``~/.claude/…``);
* ``path_mode`` = ``dirs_only`` drops file names, ``hashed`` HMACs every segment.

Claude Code is read directly from its JSONL (main transcript plus
``<session>/subagents/agent-*.jsonl``) because the shared span parser does not
emit subagent tool calls and only keeps per-turn token totals. Every other agent
goes through the shared ``LocalSource`` spans (``build_map_from_spans``).
"""

from __future__ import annotations

import configparser
import hashlib
import hmac
import json
import os
import re
from collections.abc import Iterable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ._util import captured

SCHEMA = "agentplay/v1"
LEGACY_SCHEMA = "entelligence.session-map/v1"  # accepted by readers; same document shape
SCHEMA_VERSION = 1
MAX_EVENTS = 20_000  # hard cap per map; beyond it the map is truncated and flagged
ROLE_READ_MAX_BYTES = 64 * 1024 * 1024
PATH_MODES = ("relative", "dirs_only", "hashed")

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"}
SEARCH_TOOLS = {"Grep", "Glob", "LS", "view_image"}
SHELL_TOOLS = {"Bash", "exec_command", "shell", "local_shell", "run_terminal_cmd"}
SUBAGENT_TOOLS = {"Agent", "Task", "spawn_agent"}
WEB_TOOLS = {"WebFetch", "WebSearch", "web_search"}

_VERIFY_RE = re.compile(
    r"\b(pytest|go test|go vet|npm (?:run )?(?:test|build|lint)|pnpm (?:run )?(?:test|check|lint)"
    r"|yarn (?:test|build)|tsc\b|ruff|mypy|make (?:test|check)|cargo (?:test|clippy|check)"
    r"|vitest|jest|just test)"
)
_SEARCH_VERBS = {"grep", "rg", "find", "ls", "tree", "fd", "ag"}
_READ_VERBS = {"cat", "sed", "head", "tail", "less", "wc", "jq", "nl", "bat"}
_MULTI_VERBS = {
    "git",
    "gh",
    "aws",
    "npm",
    "pnpm",
    "yarn",
    "make",
    "cargo",
    "go",
    "uv",
    "just",
    "docker",
    "kubectl",
}
_ABS_PATH_RE = re.compile(r"(?<![\w.-])(/(?:[\w.@+-]+/)*[\w.@+-]+)")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_./@+-]+")
# A bare commit sha (`git show <sha>`, a conflict marker) is never a file path.
_HEX_RE = re.compile(r"[0-9a-f]{7,}")
_CD_RE = re.compile(r"^\s*cd\s+(['\"]?)([^'\";&|]+)\1")
_GIT_C_RE = re.compile(r"\bgit\s+-C\s+(['\"]?)([^'\"\s;&|]+)\1")
_TOUCH_RANK = {"hit": 1, "read": 2, "edit": 3}


# ---------------------------------------------------------------- helpers
def _parse_ts(value: Any) -> float | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def estimate_cost_usd(
    model: str | None,
    fresh: int,
    cache_write: int,
    cache_read: int,
    output: int,
    *,
    single_request: bool = True,
) -> float | None:
    """Advisory per-request cost from the bundled rate card; ``None`` when unpriced.

    Standard multipliers when a card has no cache keys:
    read 0.1x input, write 1.25x input.

    Args:
        single_request: False when the token counts sum several requests (a
            turn aggregate). The long-context tier is a per-request property,
            so an aggregate is priced at base rates.
    """
    if not model:
        return None
    try:
        from agentplay._core.pricing import MODEL_RATES, normalize_model_key
    except Exception:  # pragma: no cover - module always present in a build
        return None
    rates = MODEL_RATES.get(normalize_model_key(model))
    if not rates:
        return None
    inp, out = float(rates["input"]), float(rates["output"])
    cr = float(rates.get("cache_read", inp * 0.1))
    cw = float(rates.get("cache_write", inp * 1.25))
    # Per-request rule: a request
    # whose cache-inclusive input crosses the card's threshold is billed at the
    # long-context tier for the WHOLE request (gpt-6-astra, gpt-6-luna).
    threshold = rates.get("long_context_threshold")
    if single_request and threshold is not None and fresh + cache_write + cache_read > threshold:
        in_mult = float(rates["long_context_input_multiplier"])
        inp, cr, cw = inp * in_mult, cr * in_mult, cw * in_mult
        out *= float(rates["long_context_output_multiplier"])
    return round((fresh * inp + cache_write * cw + cache_read * cr + output * out) / 1e6, 6)


def _cost_from_events(model: str | None, events: list[dict[str, Any]]) -> float | None:
    """Sum of per-request advisory costs from a turn's ``llm.cost_events``.

    ``None`` when any request is unpriced or malformed, so the caller falls
    back to the aggregate estimate instead of reporting a partial sum.
    """
    total = 0.0
    for e in events:
        if not isinstance(e, dict):
            return None
        cost = estimate_cost_usd(
            e.get("model") or model,
            int(e.get("input_tokens") or 0),
            int(e.get("cache_write_5m_tokens") or 0) + int(e.get("cache_write_1h_tokens") or 0),
            int(e.get("cache_read_tokens") or 0),
            int(e.get("output_tokens") or 0),
        )
        if cost is None:
            return None
        total += cost
    return round(total, 6)


class RepoResolver:
    """Maps absolute paths to ``(repo_root, repo_name, remote_slug)`` without spawning git.

    Reads ``.git`` (directory or worktree pointer file) and the ``origin`` url
    from the git config directly; results are cached per directory.
    """

    def __init__(self) -> None:
        self._cache: dict[str, tuple[str, str, str] | None] = {}
        # name -> root, most recently resolved last, so a shared name keeps the latest checkout.
        self._roots: dict[str, str] = {}

    def resolve(self, path: str) -> tuple[str, str, str] | None:
        p = Path(path)
        d = p if p.is_dir() else p.parent
        seen: list[str] = []
        result: tuple[str, str, str] | None = None
        while True:
            key = str(d)
            if key in self._cache:
                result = self._cache[key]
                break
            seen.append(key)
            git = d / ".git"
            if git.exists():
                result = (key, *self._remote(d, git))
                break
            if d.parent == d:
                break
            d = d.parent
        for k in seen:
            self._cache[k] = result
        if result:
            self._roots.pop(result[1], None)
            self._roots[result[1]] = result[0]
        return result

    def roots(self) -> dict[str, str]:
        """Repo name -> checkout root for every repo resolved so far (local use only).

        When two checkouts share a name, the one resolved most recently wins.
        """
        return dict(self._roots)

    @staticmethod
    def _remote(root: Path, git: Path) -> tuple[str, str]:
        config_path = None
        try:
            if git.is_dir():
                config_path = git / "config"
            else:  # worktree / submodule: "gitdir: <path>"
                text = git.read_text(encoding="utf-8", errors="replace").strip()
                if text.startswith("gitdir:"):
                    gitdir = Path(text.split(":", 1)[1].strip())
                    if not gitdir.is_absolute():
                        gitdir = (root / gitdir).resolve()
                    common = gitdir / "commondir"
                    base = (
                        (gitdir / common.read_text(encoding="utf-8", errors="replace").strip()).resolve()
                        if common.exists()
                        else gitdir
                    )
                    config_path = base / "config"
            url = ""
            if config_path and config_path.exists():
                cp = configparser.ConfigParser(strict=False, interpolation=None)
                cp.read(config_path, encoding="utf-8")
                url = cp.get('remote "origin"', "url", fallback="") or ""
        except Exception:
            url = ""
        url = url.strip()
        if _is_local_remote(url):
            # A clone of a local path: the url is the owner's filesystem layout (and
            # username), not a repository identity. Recorded with no remote.
            return root.name, ""
        if url:
            slug = re.sub(
                r"\.git$",
                "",
                re.sub(r"^.*?(?:github\.com|gitlab\.com|bitbucket\.org)[:/]", "", url),
            )
            slug = re.sub(r"^[a-z]+://[^/]+/", "", slug)  # other hosts: drop scheme+host
            name = slug.rstrip("/").rsplit("/", 1)[-1] or root.name
            return name, slug
        return root.name, root.name


def _is_local_remote(url: str) -> bool:
    """True for a remote url that is a filesystem path, not a hosted repository."""
    return url.startswith(("/", "~", ".", "\\", "file:")) or bool(re.match(r"[A-Za-z]:[\\/]", url))


def _isabs(path: str) -> bool:
    """`os.path.isabs`, but a rooted path like `/tmp/x` counts on every OS.

    Transcripts recorded on macOS, Linux or WSL carry POSIX paths. Windows
    stopped treating those as absolute in Python 3.13 (it now wants a drive or
    UNC root), which sent them down the relative-path branches.
    """
    return os.path.isabs(path) or path.startswith(("/", "\\"))


def _outside_bucket(path: str) -> str:
    home = str(Path.home())
    if (
        path.startswith("/tmp")
        or path.startswith("/private/tmp")
        or path.startswith("/var/folders")
    ):
        return "/tmp (scratch)"
    if "/.claude/" in path or path.endswith("/.claude"):
        m = re.search(r"/\.claude/(skills/[^/]+|projects|plans|agents|commands|memory)", path)
        return "~/.claude/" + (
            m.group(1).split("/")[0] + ("/" + m.group(1).split("/")[1] if "/" in m.group(1) else "")
            if m
            else "…"
        )
    for folder in ("Documents", "Downloads", "Desktop"):
        if path.startswith(f"{home}/{folder}"):
            return f"~/{folder}"
    if path.startswith(home):
        return "~ (home)"
    return "outside-repo"


# Commands that only prepare the shell. When one leads a sequence the verb belongs to
# what follows, or `cd x && make test` is filed under `cd` and `export A=1; pytest`
# under `export` — and a long-running tail gets attributed to the setup that preceded it.
_PREFIX_VERBS = {"cd", "export", "set", "unset", "source", "pushd", "popd"}

# `&&`, `||`, `;` and a newline sequence commands; a pipe does not
# (`cat x | grep y` is a grep).
_SEQUENCE_RE = re.compile(r"&&|\|\||;|\n")

# `for i in ...; do CMD; done` and `if ...; then CMD; fi` — the verb is the body's,
# not the shell keyword. `do`/`then` may follow a `;` or a newline. Opener and body
# keyword are separate so the keyword can be required to sit outside quoting.
_BLOCK_FORMS = (
    (re.compile(r"^(?:for|while|until)\b", re.S), re.compile(r"[;\n]\s*do\s+", re.S)),
    (re.compile(r"^if\b", re.S), re.compile(r"[;\n]\s*then\s+", re.S)),
)

# `$(...)` and backticks run their own commands; their words are never this line's verb.
# `P=$(uv tool dir)/x; find .` must be a `find`, not a `tool`.
_SUBSTITUTION_RE = re.compile(r"\$\([^()]*\)|`[^`]*`")


def _unquoted(line: str) -> list[bool]:
    """Per-character mask of where quoting is *not* in effect.

    A separator inside an argument is not a command boundary. Splitting on it
    hands the verb to a fragment of that argument, so ``cd "/private;token" &&
    pytest`` would be filed as ``token`` — a piece of a path written into the
    map, which the redaction contract forbids.

    Args:
        line: one shell command line.

    Returns:
        A list as long as ``line``: ``True`` for a character outside any
        quoting, ``False`` for a quote character, everything inside a matching
        pair, a backslash, and the character it escapes.
    """
    mask = [True] * len(line)
    quote = ""
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            mask[i] = False
            if ch == "\\" and quote == '"' and i + 1 < len(line):
                mask[i + 1] = False
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
            mask[i] = False
        elif ch == "\\" and i + 1 < len(line):
            mask[i] = mask[i + 1] = False
            i += 2
            continue
        i += 1
    return mask


def _search_unquoted(pattern: re.Pattern[str], line: str) -> re.Match[str] | None:
    """First match of ``pattern`` in ``line`` lying wholly outside quoting, else None."""
    mask = _unquoted(line)
    for m in pattern.finditer(line):
        if all(mask[i] for i in range(m.start(), m.end())):
            return m
    return None


def _block_body(line: str) -> str | None:
    """Body of a ``for``/``while``/``until ... do`` or ``if ... then`` line, else None."""
    for opener, keyword in _BLOCK_FORMS:
        if opener.match(line) is None:
            continue
        m = _search_unquoted(keyword, line)
        if m is not None:
            return line[m.end() :].strip()
    return None


# A recorded verb is a command name, optionally with one subcommand word. Anything
# else is an argument fragment and is never written; it is filed under `sh` instead.
_VERB_SHAPE = re.compile(r"[A-Za-z0-9_.+-]{1,32}(?: [a-z][a-z0-9-]{0,24})?")
_ASSIGN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")
_SEQUENCE_SEPS = {";", "&&", "||", "\n"}
# Launcher options whose next word is the option's value, not the command.
_LAUNCHER_OPT_ARGS = {"-u", "-g", "-h", "-p", "-C", "-D", "-U", "-r", "-t", "-s", "-k"}


class _Unbalanced(ValueError):
    """An open quote or substitution that never closes."""


def _skip_quoted(line: str, i: int) -> int:
    """Index just past the quoted span or substitution that opens at ``line[i]``.

    Handles ``'...'``, ``$'...'``, ``"..."``, ``$(...)``, ``${...}`` and backticks,
    nested as the shell nests them. Raises ``_Unbalanced`` when one never closes.
    """
    n = len(line)
    if line.startswith("$'", i):  # ANSI-C string: backslash escapes the next char
        j = i + 2
        while j < n and line[j] != "'":
            j += 2 if line[j] == "\\" else 1
        if j >= n:
            raise _Unbalanced
        return j + 1
    ch = line[i]
    if ch == "'":
        j = line.find("'", i + 1)
        if j < 0:
            raise _Unbalanced
        return j + 1
    if ch == "`":
        j = i + 1
        while j < n and line[j] != "`":
            j += 2 if line[j] == "\\" else 1
        if j >= n:
            raise _Unbalanced
        return j + 1
    if ch == '"':
        j = i + 1
        while j < n and line[j] != '"':
            if line[j] == "\\":
                j += 2
            elif line.startswith(("$(", "${"), j) or line[j] == "`":
                j = _skip_quoted(line, j)
            else:
                j += 1
        if j >= n:
            raise _Unbalanced
        return j + 1
    # `$(` or `${`: balanced up to the matching close, quotes inside respected.
    opener = line[i + 1]
    close = ")" if opener == "(" else "}"
    depth, j = 0, i + 1
    while j < n:
        c = line[j]
        if c == "\\":
            j += 2
            continue
        if c in "'\"`" or line.startswith(("$(", "${", "$'"), j):
            j = _skip_quoted(line, j)
            continue
        if c == opener:
            depth += 1
        elif c == close:
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise _Unbalanced


def _next_command(line: str, pos: int) -> tuple[list[tuple[str, str]], int, str]:
    """Words of the simple command starting at ``line[pos]``, honouring shell quoting.

    Returns ``(words, end, sep)``: each word as ``(raw, text)`` — ``raw`` exactly as
    written, ``text`` with quotes removed and every substitution replaced by ``$_``
    (its contents are never kept) — the index just past the separator that ended
    the command, and that separator (``""`` at end of line). A quote that never
    closes (a heredoc body, a truncated line) makes the rest one opaque word.
    """
    words: list[tuple[str, str]] = []
    raw: list[str] = []
    text: list[str] = []

    def flush() -> None:
        if raw:
            words.append(("".join(raw), "".join(text)))
            raw.clear()
            text.clear()

    n, i = len(line), pos
    while i < n:
        ch = line[i]
        if ch in " \t\r":
            flush()
            i += 1
        elif ch in ";|\n" or (
            ch == "&" and not (i and line[i - 1] in "<>") and not line.startswith("&>", i)
        ):
            flush()
            sep = line[i : i + 2] if line.startswith(("&&", "||"), i) else ch
            return words, i + len(sep), sep
        elif ch == "\\":
            raw.append(line[i : i + 2])
            text.append(line[i + 1 : i + 2])
            i += 2
        elif ch in "'\"`" or line.startswith(("$(", "${", "$'"), i):
            try:
                j = _skip_quoted(line, i)
            except _Unbalanced:  # never closes: the rest of the line is one opaque word
                raw.append(line[i:])
                text.append("$_")
                break
            raw.append(line[i:j])
            if ch in "'\"" or line.startswith("$'", i):
                inner = line[i + 2 if ch == "$" else i + 1 : j - 1]
                text.append(_SUBSTITUTION_RE.sub("$_", inner) if ch == '"' else inner)
            else:
                text.append("$_")
            i = j
        else:
            raw.append(ch)
            text.append(ch)
            i += 1
    flush()
    return words, n, ""


def _shell_verb(cmd: str) -> str:
    """First meaningful command word(s) of a shell line; never arguments or env values.

    The line is tokenized with shell quoting, so a quoted value — ``A="x y" cmd`` —
    is one word and is dropped whole with its assignment, never split into a
    fragment that could pose as the command. Whatever survives must still look
    like a command name (``_VERB_SHAPE``); anything else is filed as ``sh``.
    """
    line = cmd.strip()
    for _ in range(4):  # innermost-out; bounded so nesting can't spin
        collapsed = _SUBSTITUTION_RE.sub("$_", line)
        if collapsed == line:
            break
        line = collapsed
    line = line.strip()
    try:
        # `for ...; do if ...; then CMD` nests, and a block may follow setup segments
        # (`A=1; for ...`). Both loops are bounded so a pathological line can't spin.
        pos = 0
        for _ in range(4):
            body = _block_body(line)
            if body is not None:
                line, pos = body, 0
                continue
            # Walk past `cd`/`export`/`A=1`... segments.
            pos = 0
            for _ in range(8):
                words, end, sep = _next_command(line, pos)
                names = [t for r, t in words if not _ASSIGN_RE.match(r)]
                if (
                    sep in _SEQUENCE_SEPS
                    and line[end:].strip()
                    and (not names or os.path.basename(names[0].strip("()")) in _PREFIX_VERBS)
                ):
                    pos = end
                    continue
                break
            if pos == 0 or _block_body(line[pos:].strip()) is None:
                break
            line, pos = line[pos:].strip(), 0
        # Either loop can run out (deep nesting, many setup segments): tokenize the
        # command where the walk stopped rather than reuse a stale or unset `words`.
        words, _, _ = _next_command(line, pos)
    except (RecursionError, IndexError):
        return "sh"
    words = [t for r, t in words if not _ASSIGN_RE.match(r) and ("=" not in t or t.startswith("-"))]
    if not words or words[0].endswith("()") or words[1:2] == ["()"]:
        return "sh"  # nothing to run, or a function definition: its name is the author's
    first = os.path.basename(words[0].strip("()")) or "sh"
    if first in {"sudo", "time", "timeout", "nohup", "env"} and len(words) > 1:
        rest: list[str] = []
        skip = False
        for w in words[1:]:
            if skip:
                skip = False
            elif w in _LAUNCHER_OPT_ARGS and first in {"sudo", "timeout"}:
                skip = True
            elif rest or not re.fullmatch(r"-?\d+[smh]?|-\w+", w):
                rest.append(w)
        if rest:
            words = rest
            first = os.path.basename(rest[0]) or first
    verb = first[:32]
    if first in _MULTI_VERBS and len(words) > 1:
        sub = next((w for w in words[1:] if not w.startswith("-") and "/" not in w), "")
        if sub and re.fullmatch(r"[a-z][a-z0-9-]{0,24}", sub):
            verb = f"{first} {sub}"
    return verb if _VERB_SHAPE.fullmatch(verb) else "sh"


_QUOTED_RE = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
_SEGMENT_RE = re.compile(r"&&|\|\||[;|&\n]")
_ENV_ASSIGN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=\S*")
_LAUNCHER_ARG_RE = re.compile(r"-\S*|\d+(?:\.\d+)?[smhd]?")
_LAUNCHERS_1 = {"sudo", "nohup", "time", "timeout", "env", "command", "exec", "uvx", "npx", "bunx"}
_LAUNCHERS_2 = {
    ("uv", "run"),
    ("poetry", "run"),
    ("pipenv", "run"),
    ("hatch", "run"),
    ("pdm", "run"),
    ("pnpm", "exec"),
    ("pnpm", "dlx"),
    ("yarn", "dlx"),
}
_PYTHON_RE = re.compile(r"python(?:\d+(?:\.\d+)?)?")


def _url_host(url: str) -> str:
    """Hostname only — never userinfo (credentials), port, path, query or fragment."""
    if not re.match(r"^https?://", url, re.I):
        return ""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return host[:80]


def _is_verify(cmd: str) -> bool:
    """True when some command in the line *runs* a test/lint/build tool.

    Matches the command word (after launchers such as ``uv run`` / ``python -m`` /
    ``npx`` and env assignments) of each ``&&``/``;``/``|`` segment — never an
    argument, so ``echo pytest`` or ``git commit -m "pytest"`` stay ``exec``.
    """
    for seg in _SEGMENT_RE.split(_QUOTED_RE.sub(" ", cmd)):
        words = seg.split()
        while words:
            head = os.path.basename(words[0])
            if _ENV_ASSIGN_RE.fullmatch(words[0]):
                words = words[1:]
            elif head in _LAUNCHERS_1:
                words = words[1:]
                while words and _LAUNCHER_ARG_RE.fullmatch(words[0]):
                    words = words[1:]
            elif len(words) > 1 and (head, words[1]) in _LAUNCHERS_2:
                words = words[2:]
                while words and words[0].startswith("-"):
                    words = words[1:]
            elif len(words) > 1 and _PYTHON_RE.fullmatch(head) and words[1] == "-m":
                words = words[2:]
            else:
                break
        if words and _VERIFY_RE.match(" ".join([os.path.basename(words[0]), *words[1:]])):
            return True
    return False


def _shell_paths(cmd: str, cwd: str | None) -> list[str]:
    base = cwd
    m = _CD_RE.match(cmd) or _GIT_C_RE.search(cmd)
    if m:
        base = os.path.expanduser(m.group(2).strip())
    out = [p for p in _ABS_PATH_RE.findall(cmd) if p.count("/") >= 2]
    if base and _isabs(base) and os.path.isdir(base):
        for tok in _TOKEN_RE.findall(cmd):
            if (
                tok.startswith(("/", "-"))
                or len(tok) < 3
                or _HEX_RE.fullmatch(tok)
                or ("/" not in tok and "." not in tok[1:])
            ):
                continue
            cand = os.path.normpath(os.path.join(base, tok))
            if cand != os.path.normpath(base) and os.path.exists(cand):
                out.append(cand)
    seen: set[str] = set()
    return [p for p in out if not (p in seen or seen.add(p))][:8]


def classify(
    tool: str, inp: dict[str, Any], cwd: str | None
) -> tuple[str, list[str], str | None, str | None]:
    """``(kind, abs_paths, touch, verb)`` for one tool call."""

    def _abs(p: str) -> str:
        return p if _isabs(p) or not cwd else os.path.join(cwd, p)

    if tool == "Read":
        p = inp.get("file_path") or inp.get("path") or ""
        return "read", [_abs(p)] if p else [], "read", None
    if tool in EDIT_TOOLS:
        p = inp.get("file_path") or inp.get("notebook_path") or ""
        if tool == "apply_patch" and not p:
            patch = inp.get("input") or inp.get("patch") or ""
            paths = re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", str(patch), re.M)
            return "edit", [_abs(x.strip()) for x in paths][:8], "edit", None
        return "edit", [_abs(p)] if p else [], "edit", None
    if tool in SEARCH_TOOLS:
        p = inp.get("path") or ""
        return "search", [_abs(p)] if p else [], "hit", None
    if tool in SHELL_TOOLS:
        cmd = inp.get("command") or inp.get("cmd") or ""
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)
        cmd = str(cmd)
        verb = _shell_verb(cmd)
        paths = _shell_paths(cmd, inp.get("workdir") or cwd)
        if _is_verify(cmd):
            return "verify", paths, "hit", verb
        head = verb.split()[0]
        if head in _READ_VERBS:
            return "read", paths, "read", verb
        if head in _SEARCH_VERBS or verb == "git grep":
            return "search", paths, "hit", verb
        return "exec", paths, "hit", verb
    if tool.startswith("mcp__") or (tool.startswith("mcp-") and tool.count("-") >= 2):
        return "mcp", [], None, None
    if tool == "Skill":
        return "skill", [], None, None
    if tool in SUBAGENT_TOOLS:
        return "subagent", [], None, None
    if tool in WEB_TOOLS:
        return "web", [], None, None
    return "tool", [], None, None


# ---------------------------------------------------------------- error signals
# Closed vocabularies: only these class names (and an exit code) ever leave the
# machine; the error text itself is read locally to classify and then dropped.
_TOOL_ERROR_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "permission_denied",
        (
            "doesn't want to proceed",
            "user rejected",
            "Permission denied",
            "permission to use",
            "not allowed",
        ),
    ),
    ("blocked", ("<tool_use_error>Blocked", "blocked by", "Blocked:")),
    ("validation", ("InputValidationError", "invalid input", "Invalid tool parameters")),
    ("timeout", ("timed out", "Timeout", "timeout")),
    ("model_unavailable", ("temporarily unavailable", "model is unavailable")),
    ("rate_limit", ("rate limit", "Rate limit", " 429")),
    ("api_error", ("API Error",)),
    ("stale_file", ("has been modified since", "File has not been read yet")),
    ("not_found", ("not found", "No such file", "does not exist", "ENOENT")),
    ("interrupted", ("interrupted", "Interrupted")),
)
_EXIT_RE = re.compile(r"^\s*Exit code (\d+)")
_API_ERROR_FIELD = {
    "rate_limit": "rate_limit",
    "server_error": "server_error",
    "overloaded_error": "overloaded",
    "authentication_failed": "auth",
    "oauth_org_not_allowed": "auth",
    "permission_error": "auth",
    "invalid_request": "bad_request",
    "invalid_request_error": "bad_request",
}
ERROR_SOURCES = ("tool", "mcp", "llm", "hook", "orchestration")
TOOL_DELAY_MS = 30_000
LLM_DELAY_MS = 60_000
STALL_MS = 120_000
RETRY_WINDOW = 8


def _flat_text(content: Any, limit: int = 600) -> str:
    """Local-only text used to classify an error; never stored in the map."""
    if isinstance(content, str):
        return content[:limit]
    if isinstance(content, list):
        parts = [x.get("text", "") for x in content if isinstance(x, dict)]
        return " ".join(p for p in parts if isinstance(p, str))[:limit]
    return str(content or "")[:limit]


def classify_tool_error(text: str) -> tuple[str, int | None]:
    m = _EXIT_RE.match(text)
    if m:
        return "exit_code", int(m.group(1))
    for cls, pats in _TOOL_ERROR_PATTERNS:
        if any(p in text for p in pats):
            return cls, None
    return "other", None


def classify_api_error(field: Any, text: str) -> str:
    if isinstance(field, str) and field in _API_ERROR_FIELD:
        return _API_ERROR_FIELD[field]
    low = text.lower()
    if "spend limit" in low:
        return "spend_limit"
    if "limit" in low and ("hit your" in low or "reached your" in low):
        return "usage_limit"
    if "overloaded" in low or " 529" in low:
        return "overloaded"
    if "api error: 400" in low:
        return "bad_request"
    if "rate limit" in low or " 429" in low:
        return "rate_limit"
    if "api error: 5" in low:
        return "server_error"
    return "api_error"


_NON_CALL_KINDS = frozenset(
    {
        "llm",
        "user",
        "compaction",
        "error",
        "delay",
        "model_switch",
        *("commit", "pr_merge", "pr_close", "pr_comment", "ticket", "artifact", "skill_write"),
    }
)

# ---------------------------------------------------------------- tool categories, VCS, file ops, PRs
_PKG = {
    "pip",
    "pip3",
    "npm",
    "pnpm",
    "yarn",
    "uv",
    "poetry",
    "brew",
    "apt",
    "apt-get",
    "gem",
    "bundle",
}
_BUILD = {"make", "tsc", "webpack", "vite", "gradle", "mvn", "bazel", "just"}
_CLOUD = {
    "aws",
    "gcloud",
    "az",
    "kubectl",
    "terraform",
    "docker",
    "helm",
    "sam",
    "eb",
    "flyctl",
    "vercel",
}
_HTTP = {"curl", "wget", "http", "httpie", "xh"}
_FS = {"rm", "mv", "cp", "mkdir", "touch", "chmod", "ln", "rmdir", "tee", "rsync", "unzip", "tar"}
_VCS_CLI = {"git": "git", "gh": "github", "hub": "github", "glab": "gitlab", "bb": "bitbucket"}
_PR_URL = re.compile(
    r"https?://(?:github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)"
    r"|[A-Za-z0-9.-]*gitlab[A-Za-z0-9.-]*/((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+)/-/merge_requests/(\d+)"
    r"|bitbucket\.org/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull-requests/(\d+))"
)


def _cmd_words(cmd: str) -> list[str]:
    line = cmd.strip()
    if line.startswith("cd ") and "&&" in line:
        line = line.split("&&", 1)[1].strip()
    return [w.strip("'\"") for w in line.split() if "=" not in w or w.startswith("-")]


def tool_category(ev: dict[str, Any]) -> str:
    """Closed vocabulary for the tool-call breakdown; never args."""
    kind = ev["kind"]
    if kind == "mcp":
        srv = str(ev.get("server") or "").lower()
        return "vcs" if any(h in srv for h in ("github", "gitlab", "bitbucket")) else "mcp"
    if kind in ("skill", "subagent", "web", "tool"):
        return kind
    verb = str(ev.get("verb") or "")
    if not verb:
        return "file"
    head = verb.split()[0]
    if head in _VCS_CLI:
        return "vcs"
    if kind == "verify":
        return "test"
    if head in _READ_VERBS or head in _SEARCH_VERBS:
        return "file"
    if head in _FS:
        return "fs"
    if head in _PKG and any(
        w in verb for w in ("install", "add", "ci", "sync", "update", "remove")
    ):
        return "package"
    if head in _BUILD or verb in ("cargo build", "go build", "npm run", "pnpm run", "yarn build"):
        return "build"
    if head in _CLOUD:
        return "cloud"
    if head in _HTTP:
        return "http"
    return "shell"


def vcs_action(tool: str, inp: dict[str, Any], server: str | None) -> dict[str, Any] | None:
    """``{provider, action[, number]}`` for git/gh/glab/bb commands and VCS MCP tools. Never args."""
    if server is not None:
        srv = server.lower()
        prov = next((p for p in ("github", "gitlab", "bitbucket") if p in srv), None)
        if prov:
            return {"provider": prov, "action": tool[:48]}
        return None
    cmd = inp.get("command") or inp.get("cmd") or ""
    if isinstance(cmd, list):
        cmd = " ".join(str(c) for c in cmd)
    words = _cmd_words(str(cmd))
    if not words:
        return None
    head = os.path.basename(words[0])
    prov = _VCS_CLI.get(head)
    if not prov:
        return None
    rest = [w for w in words[1:] if not w.startswith("-")]
    if head == "git":
        if words[1:2] == ["-C"]:
            rest = [w for w in words[3:] if not w.startswith("-")]
        sub = rest[0] if rest else ""
        return {
            "provider": "git",
            "action": sub if re.fullmatch(r"[a-z-]{2,20}", sub or "") else "git",
        }
    noun = rest[0] if rest else ""
    verb = rest[1] if len(rest) > 1 else ""
    action = f"{noun}_{verb}".strip("_") if re.fullmatch(r"[a-z-]{0,20}", noun + verb) else noun
    if noun in ("pr", "mr") and verb == "create":
        # A command shape, not an outcome: tool_event may overwrite this with the
        # resolved "pr_create" action once (and only once) a real PR/MR URL is seen
        # in the result. Kept distinct from that sentinel so an unresolved command
        # never reads as a confirmed PR (see pr_create_intent's shape fallback).
        return {"provider": prov, "action": "pr_create_cmd"}
    out: dict[str, Any] = {"provider": prov, "action": action[:40] or head}
    # Only the token immediately after the noun/verb (`gh pr view 123`): scanning the
    # whole tail picks up stray digits from free-text titles/bodies.
    num = rest[2] if len(rest) > 2 and rest[2].isdigit() and len(rest[2]) <= 8 else None
    if noun in ("pr", "mr") and num:
        out["number"] = int(num)
    return out


def _pr_from_url(text: str) -> dict[str, Any] | None:
    m = _PR_URL.search(text or "")
    if not m:
        return None
    if m.group(1):
        return {
            "provider": "github",
            "repo": m.group(1),
            "number": int(m.group(2)),
            "url": m.group(0),
        }
    if m.group(3):
        return {
            "provider": "gitlab",
            "repo": m.group(3),
            "number": int(m.group(4)),
            "url": m.group(0),
        }
    return {
        "provider": "bitbucket",
        "repo": m.group(5),
        "number": int(m.group(6)),
        "url": m.group(0),
    }


def pr_create_intent(tool: str, inp: dict[str, Any]) -> dict[str, Any] | None:
    """PR/MR creation intent via the bundled detectors (gh/glab/GitHub API/MCP) + Bitbucket."""
    try:
        from agentplay._core.ingestion.coding_agent.produced import (
            creation_command_attributes,
        )

        value = inp if tool.startswith("mcp__") else (inp.get("command") or inp.get("cmd") or "")
        attrs = creation_command_attributes(
            json.dumps(value) if isinstance(value, dict) else value, tool
        )
    except Exception:  # noqa: BLE001 — detection is best-effort
        attrs = {}
    if attrs.get("pr.create.provider"):
        return {
            "provider": attrs["pr.create.provider"],
            "repo": attrs.get("pr.create.repository")
            or attrs.get("pr.create.requested_repository"),
        }
    cmd = str(inp.get("command") or "")
    if tool in SHELL_TOOLS:
        # Tier 2: the bundled detector marks a long, free-text-bearing command (a
        # real PR title/body) as "unrecognized_creation_text" rather than confirming
        # a provider — a cheap shape check on the first tokens still tells us this IS
        # a create call, so it still gets resolved by URL below.
        words = _cmd_words(cmd)
        head = os.path.basename(words[0]) if words else ""
        rest = [w for w in words[1:] if not w.startswith("-")]
        if head in ("gh", "hub") and rest[:2] == ["pr", "create"]:
            return {"provider": "github", "repo": None}
        if head == "glab" and rest[:2] == ["mr", "create"]:
            return {"provider": "gitlab", "repo": None}
    low = (tool + " " + cmd).lower()
    if re.search(r"\b(?:bb|bitbucket)\b.*\bpr\b.*\bcreate\b", low) or (
        "bitbucket" in low and "pull" in low and "create" in low
    ):
        return {"provider": "bitbucket", "repo": None}
    return None


def file_ops(tool: str, inp: dict[str, Any], cwd: str | None) -> dict[str, str]:
    """abs path -> create | modify | delete | rename-from | rename-to for file-changing calls."""

    def _abs(p: str) -> str:
        return os.path.normpath(p if _isabs(p) or not cwd else os.path.join(cwd, p))

    ops: dict[str, str] = {}
    if tool == "apply_patch":
        patch = str(inp.get("input") or inp.get("patch") or "")
        for kind, path in re.findall(r"^\*\*\* (Add|Delete|Update) File: (.+)$", patch, re.M):
            ops[_abs(path.strip())] = {"Add": "create", "Delete": "delete", "Update": "modify"}[
                kind
            ]
        for path in re.findall(r"^\*\*\* Move to: (.+)$", patch, re.M):
            ops[_abs(path.strip())] = "rename-to"
        return ops
    if tool in SHELL_TOOLS:
        cmd = inp.get("command") or inp.get("cmd") or ""
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)
        base = inp.get("workdir") or cwd
        m = _CD_RE.match(str(cmd))
        if m:
            base = os.path.expanduser(m.group(2).strip())
        for seg in re.split(r"&&|;|\|\||\n", str(cmd)):
            w = _cmd_words(seg)
            if not w:
                continue
            h = os.path.basename(w[0])
            args = [a for a in w[1:] if not a.startswith("-")]
            if h == "git" and args[:1] in (["rm"], ["mv"]):
                h, args = args[0], args[1:]
            ab = [
                os.path.normpath(a if _isabs(a) or not base else os.path.join(base, a))
                for a in args
            ]
            if h in ("rm", "rmdir", "unlink"):
                for a in ab:
                    ops[a] = "delete"
            elif h == "mv" and len(ab) >= 2:
                for a in ab[:-1]:
                    ops[a] = "rename-from"
                ops[ab[-1]] = "rename-to"
            elif h == "cp" and len(ab) >= 2:
                ops[ab[-1]] = "create"
            elif h == "touch":
                for a in ab:
                    ops.setdefault(a, "create")
            # Path-safe characters only: a permissive class also matches Python f-string
            # alignment specs (`{x:>7.0f}`) as a false "redirect target"; this excludes
            # {}, (), quotes, $ outright. `:` is excluded from the lookbehind too, since
            # a real shell redirect is never preceded by one, but an f-string spec always is.
            # Nor by another `>`: a merge-conflict marker (`>>>>>>> <sha>`) in a heredoc
            # is not a write.
            for red in re.findall(r"(?<![0-9&:>])>{1,2}\s*([\w./@+-]+)", seg):
                # Excludes bare numbers, incl. decimals/durations: `[ "$rc" > 1 ]`, and
                # `echo "$val > $threshold" | bc -l` (a common bash float-comparison
                # idiom) are numeric comparisons, not redirects — a real redirect
                # target is never a bare number in practice, so this is a safe, cheap
                # guard against exactly that ambiguity (regex alone can't tell `>`
                # apart from a comparison operator).
                if (
                    red in ("/dev/null", "&1", "&2")
                    or re.fullmatch(r"[\d.]+(s|ms|h|m|x|%)?", red)
                ):
                    continue
                ops.setdefault(
                    _abs(red if _isabs(red) or not base else os.path.join(base, red)),
                    "modify" if ">>" in seg else "create",
                )
    return ops


# ---------------------------------------------------------------- outputs
# Every output is confirmed-only: the call's *input* says what was attempted
# (an "intent"), and an output event is emitted only when the call's *result*
# carries positive evidence it happened (git's own "[branch sha]" line, gh's
# "Merged pull request", a ticket identifier, an artifact URL). Tool status alone
# is not enough — Codex reports status OK for calls that failed ("is not
# mergeable", "Error: Could not find team"). Result text is read locally and
# dropped; only closed-vocabulary fields, ids, counts and repo names are stored.
OUTPUT_KINDS = (
    "commit",
    "pr_merge",
    "pr_close",
    "pr_comment",
    "ticket",
    "artifact",
    "skill_write",
)
_COMMIT_OK = re.compile(r"\[([^\]\n]{1,120}?) ([0-9a-f]{7,40})\]")
_COMMIT_STAT = re.compile(
    r"(\d+) files? changed(?:, (\d+) insertions?\(\+\))?(?:, (\d+) deletions?\(-\))?"
)
_MERGE_OK = re.compile(
    r"(?:squashed and merged|rebased and merged|\bmerged) pull request"
    r"|merge request !?\d+ (?:was |has been )?merged"
    r'|"merged"\s*:\s*true|successfully merged',
    re.I,
)
_CLOSE_OK = re.compile(r"\bclosed pull request|merge request !?\d+ (?:was )?closed", re.I)
_REVIEW_OK = re.compile(
    r"(?:approved|reviewed|requested changes (?:to|for)) pull request"
    r"|#issuecomment-\d+|#pullrequestreview-\d+|#discussion_r\d+",
    re.I,
)
# gh/glab print their success line only to a TTY: a non-interactive `gh pr merge` that
# worked prints nothing (or just the local fast-forward). So a CLI PR action is
# confirmed by a clean exit with nothing in the output saying it was refused.
_PR_REFUSED = re.compile(
    r"not mergeable|already (?:merged|closed)|is (?:still )?a draft|cannot be merged"
    r"|merge conflict|no pull requests? found|could not resolve|graphql:|http 4\d\d"
    r"|(?m:^\W*(?:x\s|error\b|failed\b))",
    re.I,
)
# gh merges on GitHub first, then switches/deletes the local branch; that local step
# failing (exit 1) happens *after* the merge.
_PR_LOCAL_AFTERMATH = re.compile(
    r"failed to run git: fatal: '[^']+' is already used by worktree|failed to delete local branch",
    re.I,
)
_TICKET_ID = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,7})\b")
_GH_ISSUE_URL = re.compile(r"github\.com/[\w.-]+/[\w.-]+/issues/(\d+)")
_ARTIFACT_OK = re.compile(r"claude\.ai/(?:code/)?artifact/[A-Za-z0-9-]+")
_ARTIFACT_VERSION = re.compile(r"\(Version (\d+)\)")
_PR_REF = re.compile(r"([\w.-]+/[\w.-]+)#(\d+)|pull request #(\d+)|/pull/(\d+)", re.I)
_SKILL_FILE = re.compile(r"(?:^|/)skills/([^/]{1,80})/SKILL\.md$")
_FAILED_TEXT = re.compile(
    r"^\W*(?:error\b|x\b|failed\b|fatal:|gh: |graphql: )|is not mergeable|could not|"
    r"not found|permission denied|unauthorized|forbidden|\"isError\"\s*:\s*true",
    re.I,
)
_LINEAR_LEAVES = {
    "save_issue",
    "create_issue",
    "update_issue",
    "save_comment",
    "create_comment",
    "linear_create_issue",
    "linear_update_issue",
    "linear_create_comment",
}
_JIRA_LEAVES = {
    "createjiraissue",
    "editjiraissue",
    "addcommenttojiraissue",
    "transitionjiraissue",
    "jira_create_issue",
    "jira_update_issue",
    "jira_add_comment",
    "jira_transition_issue",
}
_GH_PR_COMMENT_LEAVES = {
    "add_issue_comment",
    "add_pull_request_review_comment",
    "add_pull_request_review_comment_to_pending_review",
    "add_comment_to_pending_review",
    "create_pull_request_review",
    "create_and_submit_pull_request_review",
    "submit_pending_pull_request_review",
    "pull_request_review_write",
}


def unwrap_command(cmd: str) -> tuple[str, str | None]:
    """``(command, workdir)`` with Codex code-mode unwrapped.

    Codex's code mode wraps shell calls in JS — ``const r = await
    tools.exec_command({"cmd": "gh pr merge 1 …", "workdir": "…"}); …`` — so the
    raw input is not the command. Returns the input unchanged when it isn't one.
    """
    i = cmd.find("exec_command(")
    j = cmd.find("{", i) if i >= 0 else -1
    if j < 0:
        return cmd, None
    try:
        d, _ = json.JSONDecoder().raw_decode(cmd[j:])
    except ValueError:
        # A JS object literal with bare keys (`{cmd:"…", workdir:"…"}`) is not JSON.
        inner = _js_string_field(cmd[j:], ("cmd", "command"))
        return (inner, _js_string_field(cmd[j:], ("workdir",))) if inner else (cmd, None)
    if not isinstance(d, dict):
        return cmd, None
    inner = d.get("cmd") or d.get("command")
    if isinstance(inner, list):
        inner = " ".join(str(x) for x in inner)
    if not isinstance(inner, str) or not inner.strip():
        return cmd, None
    wd = d.get("workdir")
    return inner, wd if isinstance(wd, str) else None


def _js_string_field(src: str, keys: tuple[str, ...]) -> str | None:
    """A double-quoted string value for one of ``keys`` in a JS/JSON object; tolerates a
    truncated value (Cursor clips long inputs) by taking what is there."""
    for key in keys:
        m = re.search(r'["\']?\b' + key + r'["\']?\s*:\s*"((?:\\.|[^"\\])*)("?)', src)
        if not m:
            continue
        try:
            return json.loads('"' + m.group(1) + '"')
        except ValueError:
            return m.group(1).replace('\\"', '"')
    return None


def loose_command(raw: str) -> str:
    """The shell command from a tool input that is not valid JSON (truncated JSON, or a
    Codex code-mode snippet); falls back to the raw text."""
    if raw.lstrip().startswith("{"):
        found = _js_string_field(raw, ("command", "cmd"))
        if found:
            return found
    return raw


def _segments_words(cmd: str) -> list[list[str]]:
    out = []
    for seg in re.split(r"&&|\|\||;|\n", cmd):
        w = _cmd_words(seg)
        if w:
            out.append(w)
    return out


def _gh_number(args: list[str]) -> int | None:
    for a in args:
        m = re.fullmatch(r"#?(\d{1,8})", a) or re.search(r"/pull/(\d{1,8})", a)
        if m:
            return int(m.group(1))
    return None


def _gh_repo_arg(words: list[str]) -> str | None:
    for i, w in enumerate(words):
        if w in ("--repo", "-R") and i + 1 < len(words):
            v = words[i + 1].strip("'\"")
            if re.fullmatch(r"[\w.-]+/[\w.-]+", v):
                return v
        if w.startswith("--repo="):
            v = w.split("=", 1)[1].strip("'\"")
            if re.fullmatch(r"[\w.-]+/[\w.-]+", v):
                return v
    return None


_BARE_MCP_LEAVES = (
    _LINEAR_LEAVES
    | _JIRA_LEAVES
    | _GH_PR_COMMENT_LEAVES
    | {"merge_pull_request", "update_pull_request"}
)


def output_intents(
    tool: str, inp: dict[str, Any], server: str | None, leaf: str | None
) -> list[dict[str, Any]]:
    """What a call *attempts* to produce (commit, PR merge/close/comment, ticket, …).

    Never an outcome: :meth:`MapBuilder.resolve_outputs` confirms each intent
    against the call's result before any output event exists.
    """
    intents: list[dict[str, Any]] = []
    if tool in SHELL_TOOLS:
        cmd = inp.get("command") or inp.get("cmd") or ""
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)
        for words in _segments_words(str(cmd)):
            head = os.path.basename(words[0])
            args = [w for w in words[1:] if not w.startswith("-")]
            if head == "git":
                sub = [w for w in words[1:] if not w.startswith("-")]
                if words[1:2] == ["-C"]:
                    sub = [w for w in words[3:] if not w.startswith("-")]
                if sub[:1] == ["commit"]:
                    intents.append({"type": "commit"})
            elif head in ("gh", "hub", "glab") and len(args) >= 2:
                noun, verb = args[0], args[1]
                repo = _gh_repo_arg(words)
                num = _gh_number(args[2:3])
                if noun in ("pr", "mr") and verb == "merge":
                    intents.append(
                        {
                            "type": "pr_merge",
                            "repo": repo,
                            "number": num,
                            "cli": True,
                            "auto": "--auto" in words,
                        }
                    )
                elif noun in ("pr", "mr") and verb == "close":
                    intents.append({"type": "pr_close", "repo": repo, "number": num, "cli": True})
                elif noun in ("pr", "mr") and verb in ("comment", "review", "note"):
                    intents.append({"type": "pr_comment", "repo": repo, "number": num, "cli": True})
                elif noun == "issue" and verb in ("create", "edit", "comment", "close"):
                    intents.append(
                        {
                            "type": "ticket",
                            "tracker": "github",
                            "action": "create" if verb == "create" else "update",
                            "id": f"#{num}" if num else None,
                            "tool": f"{head} issue {verb}",
                        }
                    )
        return intents
    if server is not None or leaf is not None:
        srv = (server or "").lower()
        name = (leaf or tool).lower()
        if "github" in srv or (not srv and (name == "merge_pull_request" or _mcp_repo(inp))):
            if name == "merge_pull_request":
                intents.append(
                    {
                        "type": "pr_merge",
                        "repo": _mcp_repo(inp),
                        "number": _int_or_none(inp.get("pullNumber") or inp.get("pull_number")),
                    }
                )
            elif name == "update_pull_request" and str(inp.get("state") or "") == "closed":
                intents.append(
                    {
                        "type": "pr_close",
                        "repo": _mcp_repo(inp),
                        "number": _int_or_none(inp.get("pullNumber") or inp.get("pull_number")),
                    }
                )
            elif name in _GH_PR_COMMENT_LEAVES:
                intents.append(
                    {
                        "type": "pr_comment",
                        "repo": _mcp_repo(inp),
                        "number": _int_or_none(
                            inp.get("pullNumber")
                            or inp.get("pull_number")
                            or inp.get("issue_number")
                        ),
                    }
                )
            elif name in ("create_issue", "update_issue"):
                intents.append(
                    {
                        "type": "ticket",
                        "tracker": "github",
                        "action": "create" if name == "create_issue" else "update",
                        "id": None,
                        "tool": name[:60],
                    }
                )
            return intents
        tracker = None
        if "linear" in srv or name.startswith("linear") or (not srv and name in _LINEAR_LEAVES):
            tracker = "linear"
        elif "jira" in srv or "atlassian" in srv or "jira" in name:
            tracker = "jira"
        if tracker and (name in _LINEAR_LEAVES or name in _JIRA_LEAVES or "issue" in name):
            if any(k in name for k in ("get", "list", "search", "fetch", "read")):
                return intents
            has_id = any(inp.get(k) for k in ("id", "issueId", "issue_id", "issueIdOrKey"))
            create = ("create" in name and "comment" not in name) or (
                name == "save_issue" and not has_id
            )
            ref = next(
                (
                    str(inp.get(k))
                    for k in ("id", "issueId", "issue_id", "issueIdOrKey")
                    if isinstance(inp.get(k), str) and _TICKET_ID.fullmatch(str(inp.get(k)))
                ),
                None,
            )
            intents.append(
                {
                    "type": "ticket",
                    "tracker": tracker,
                    "action": "create" if create else "update",
                    "id": ref,
                    "tool": name[:60],
                }
            )
        return intents
    if tool == "Artifact":
        action = str(inp.get("action") or "publish")
        if action == "publish" and not inp.get("asset"):
            fp = inp.get("file_path")
            size = None
            if isinstance(fp, str) and fp:
                with suppress(OSError):
                    size = os.path.getsize(fp)
            intents.append({"type": "artifact", "bytes": size, "tool": "Artifact"})
    return intents


def _mcp_repo(inp: dict[str, Any]) -> str | None:
    owner, repo = inp.get("owner"), inp.get("repo")
    if isinstance(owner, str) and isinstance(repo, str) and owner and repo:
        return f"{owner}/{repo}"[:120]
    return None


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _result_core(text: str) -> str:
    """Drop harness framing (Codex: ``Script completed Wall time … Output:``)."""
    i = text.find("Output:")
    return text[i + len("Output:") :] if 0 <= i < 120 else text


def confirm_output(
    intent: dict[str, Any], text: str, err: bool, *, repo_name: str | None = None
) -> dict[str, Any] | None:
    """The output event fields for a confirmed intent, else ``None`` (never a guess)."""
    core = _result_core(text or "")
    # The one error that follows a real merge: gh's *local* branch cleanup.
    after_merge = (
        intent["type"] == "pr_merge"
        and intent.get("cli")
        and _PR_LOCAL_AFTERMATH.search(core)
        and not _PR_REFUSED.search(_PR_LOCAL_AFTERMATH.sub("", core))
    )
    if err and not after_merge:
        return None
    failed = bool(_FAILED_TEXT.search(core[:400]))
    typ = intent["type"]
    if typ == "commit":
        m = _COMMIT_OK.search(core)
        if not m:
            return None
        out: dict[str, Any] = {"kind": "commit", "sha": m.group(2)[:12]}
        if repo_name:
            out["repo"] = repo_name
        st = _COMMIT_STAT.search(core, m.end())
        if st:
            out["files"] = int(st.group(1))
            out["ins"] = int(st.group(2) or 0)
            out["del"] = int(st.group(3) or 0)
        return out
    if typ in ("pr_merge", "pr_close", "pr_comment"):
        ok = {"pr_merge": _MERGE_OK, "pr_close": _CLOSE_OK, "pr_comment": _REVIEW_OK}[typ]
        if intent.get("auto"):
            return None  # `--auto` only queues the merge for when checks pass
        confirmed = bool(ok.search(core)) or (
            typ == "pr_comment" and not failed and "html_url" in core
        )
        if not confirmed and intent.get("cli"):
            confirmed = not _PR_REFUSED.search(_PR_LOCAL_AFTERMATH.sub("", core))
        if not confirmed:
            return None
        out = {"kind": typ}
        repo, num = intent.get("repo"), intent.get("number")
        m = _PR_REF.search(core)
        if m:
            repo = repo or m.group(1)
            num = num or _int_or_none(m.group(2) or m.group(3) or m.group(4))
        if repo:
            out["repo"] = str(repo)[:120]
        if num:
            out["number"] = num
        return out
    if typ == "ticket":
        if failed:
            return None
        tid = intent.get("id")
        if intent.get("tracker") == "github":
            m = _GH_ISSUE_URL.search(core)
            tid = tid or (f"#{m.group(1)}" if m else None)
        else:
            m = _TICKET_ID.search(core)
            tid = tid or (m.group(1) if m else None)
        if intent.get("action") == "create" and not tid:
            return None  # a create with no identifier in its result never demonstrably happened
        if not tid and not core.strip():
            return None
        out = {
            "kind": "ticket",
            "tracker": intent["tracker"],
            "action": intent["action"],
            "tool": intent.get("tool"),
        }
        if tid:
            out["id"] = tid
        return out
    if typ == "artifact":
        if failed or not _ARTIFACT_OK.search(core):
            return None
        out = {"kind": "artifact", "tool": intent.get("tool") or "Artifact"}
        if intent.get("bytes") is not None:
            out["bytes"] = int(intent["bytes"])
        v = _ARTIFACT_VERSION.search(core)
        if v:
            out["version"] = int(v.group(1))
        return out
    return None


OUTCOME_KIND = (
    "pr_created",
    "verified",
    "blocked",
    "interrupted",
    "unresolved_errors",
    "completed",
    "incomplete",
)


def classify_outcome(events: list[dict[str, Any]], last_text: str) -> str:
    """A single closed-vocabulary label for how a session/agent ended.

    Reads local signals only (error/verify/PR events, and the *length* of the
    final assistant reply as a tie-breaker heuristic — never its content) and
    returns one of ``OUTCOME_KIND``. Priority: an actual PR/MR created beats
    everything; then whether the run ends mid-work vs cleanly.
    """
    if not events:
        return "incomplete"
    if any((e.get("vcs") or {}).get("action") == "pr_create" for e in events):
        return "pr_created"
    tail = events[-12:]
    if any(e.get("kind") == "error" and e.get("errClass") == "interrupted" for e in tail):
        return "interrupted"
    if any(e.get("errClass") in ("blocked", "permission_denied") for e in tail if e.get("err")):
        return "blocked"
    verifies = [e for e in events if e.get("kind") == "verify"]
    if verifies and not verifies[-1].get("err"):
        # `seq` is the event's position in the whole session, and `events` may be one
        # agent's slice of it, so compare seqs rather than slicing by one.
        last_verify = verifies[-1]["seq"]
        if not any(e.get("kind") == "edit" and e["seq"] > last_verify for e in events):
            return "verified"
    trailing_errors = sum(1 for e in tail if e.get("err") and e.get("kind") != "error") or sum(
        1 for e in tail if e.get("kind") == "error"
    )
    if trailing_errors >= 2:
        return "unresolved_errors"
    last = events[-1]
    if last.get("kind") in ("edit", "exec") and not last_text.strip():
        return "incomplete"
    return "completed"


def _error_source(e: dict[str, Any]) -> str:
    if e["kind"] == "error":
        return str(e.get("source") or "tool")
    if e["kind"] == "mcp":
        return "mcp"
    if e["kind"] == "subagent":
        return "orchestration"
    return "tool"


def _signal_totals(evs: list[dict[str, Any]]) -> dict[str, Any]:
    by_src: dict[str, int] = {}
    by_cls: dict[str, int] = {}
    for e in evs:
        if not e.get("err"):
            continue
        src = _error_source(e)
        by_src[src] = by_src.get(src, 0) + 1
        cls = str(e.get("errClass") or "other")
        by_cls[cls] = by_cls.get(cls, 0) + 1
    llm = [e for e in evs if e["kind"] == "llm"]
    lats = sorted(int(e["latency"]) for e in llm if e.get("latency"))
    ctx = [e["in"] + e["cw"] + e["cr"] for e in llm if e["agent"] == "main"]
    delays = [e for e in evs if e["kind"] == "delay"]
    return {
        "errorsBySource": by_src,
        "errorsByClass": by_cls,
        "retries": sum(1 for e in evs if e.get("retryOf") is not None),
        "delays": len(delays),
        "delayMs": sum(int(e.get("ms") or 0) for e in delays),
        "maxContext": max(ctx, default=0),
        "compactions": sum(1 for e in evs if e["kind"] == "compaction"),
        "llmLatencyP50": lats[len(lats) // 2] if lats else 0,
        "llmLatencyP95": lats[min(len(lats) - 1, int(len(lats) * 0.95))] if lats else 0,
        "outputTokens": sum(e["out"] for e in llm),
    }


# ---------------------------------------------------------------- builder
class MapBuilder:
    """Accumulates events for one session (main agent plus subagents) and emits the map."""

    def __init__(
        self,
        *,
        path_mode: str = "relative",
        hmac_key: bytes | None = None,
        resolver: RepoResolver | None = None,
    ) -> None:
        if path_mode not in PATH_MODES:
            path_mode = "relative"
        if path_mode == "hashed" and not hmac_key:
            path_mode = "dirs_only"  # never emit plain names when a hash was asked for
        self.path_mode = path_mode
        self.hmac_key = hmac_key or b""
        self.resolver = resolver or RepoResolver()
        self.repos: dict[str, dict[str, Any]] = {}
        self.files: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.agents: list[dict[str, Any]] = [{"id": "main", "parent": None, "kind": "main"}]
        self.t0: float | None = None
        self.truncated = False
        self._touched_keys: set[str] = set()
        self.prs_created: list[dict[str, Any]] = []
        self.last_agent_text: dict[str, str] = {}
        # Model used when a harness reports none per request (Cursor: "default"),
        # and the CLI's own task segmentation for this session, when it has any.
        self.default_model: str = ""
        self.task_specs: list[dict[str, Any]] | None = None

    # -- paths
    def _seg(self, s: str) -> str:
        return hmac.new(self.hmac_key, s.encode(), hashlib.sha256).hexdigest()[:10]

    def _redact_rel(self, rel: str) -> str:
        if self.path_mode == "relative":
            return rel
        parts = rel.split("/")
        if self.path_mode == "dirs_only":
            return "/".join(parts[:-1] + ["•" + hashlib.sha256(rel.encode()).hexdigest()[:6]])
        return "/".join(self._seg(p) for p in parts)

    def target(self, abspath: str, touch: str) -> dict[str, Any] | None:
        abspath = abspath.rstrip(".,:;)'\"")
        if not abspath or not _isabs(abspath):
            return None
        found = None
        # Only the user's own ~/.claude (config, transcripts, skills) is bucketed without
        # repo lookup; a repo's checked-in .claude/ files stay repo-relative.
        home_claude = os.path.join(str(Path.home()), ".claude")
        if not (abspath == home_claude or abspath.startswith(home_claude + os.sep)):
            try:
                found = self.resolver.resolve(abspath)
            except Exception:
                found = None
        if not found:
            return {"scope": "outside", "label": _outside_bucket(abspath), "touch": touch}
        root, name, slug = found
        rel = os.path.relpath(abspath, root)
        if rel in (".", "") or rel.startswith("..") or rel.split(os.sep)[0] == ".git":
            return None
        rel = rel.replace(os.sep, "/")
        repo_key = slug or f"local:{root}"  # a local-path remote has no slug; key by checkout
        repo = self.repos.get(repo_key)
        if repo is None:
            repo = {"id": f"r{len(self.repos)}", "name": name, "remote": slug or None}
            self.repos[repo_key] = repo
        key = f"{repo['id']}:{rel}"
        f = self.files.get(key)
        if f is None:
            f = {
                "id": len(self.files),
                "repo": repo["id"],
                "path": self._redact_rel(rel),
                "reads": 0,
                "edits": 0,
                "hits": 0,
                "first": None,
            }
            self.files[key] = f
        return {"file": f["id"], "touch": touch, "_key": key}

    # -- events
    def rel_ms(self, ts: float | None) -> int:
        if ts is None:
            return self.events[-1]["t"] if self.events else 0
        if self.t0 is None or ts < self.t0:
            self.t0 = ts if self.t0 is None else self.t0
        return int((ts - (self.t0 or ts)) * 1000)

    def add(self, ev: dict[str, Any], ts: float | None) -> dict[str, Any]:
        ev["_ts"] = ts
        ev.setdefault("err", False)
        if len(self.events) >= MAX_EVENTS:
            self.truncated = True
            return ev
        self.events.append(ev)
        return ev

    def tool_event(
        self, *, tool: str, inp: dict[str, Any], cwd: str | None, agent: str, ts: float | None
    ) -> dict[str, Any]:
        inp = inp if isinstance(inp, dict) else {}
        if tool in SHELL_TOOLS or tool == "js":
            raw = inp.get("command") or inp.get("cmd") or ""
            if isinstance(raw, str) and "exec_command(" in raw:
                unwrapped, wd = unwrap_command(raw)
                if unwrapped is not raw:
                    inp = {**inp, "command": unwrapped}
                    if wd:
                        inp["workdir"] = wd
                    tool = "Bash" if tool == "js" else tool
        kind, paths, touch, verb = classify(tool, inp, cwd)
        ev: dict[str, Any] = {"kind": kind, "tool": tool, "agent": agent}
        if verb:
            ev["verb"] = verb
        if kind == "mcp":
            if tool.startswith("mcp__"):
                parts = tool.split("__")
                ev["server"] = parts[1] if len(parts) > 2 else "mcp"
                ev["tool"] = parts[-1]
            else:  # Cursor: "mcp-<server>-<tool>"; tool names use underscores, servers dashes
                server, _, leaf = tool[len("mcp-") :].rpartition("-")
                ev["server"] = (server or "mcp")[:80]
                ev["tool"] = (leaf or tool)[:80]
        elif kind == "skill":
            skill = str((inp or {}).get("skill") or (inp or {}).get("name") or "")
            ev["skill"] = skill[:80]
        elif kind == "subagent":
            ev["subagentType"] = str(
                (inp or {}).get("subagent_type") or (inp or {}).get("agent_type") or "general"
            )[:40]
            if (inp or {}).get("model"):
                ev["model"] = str(inp["model"])[:60]
        elif kind == "web":
            url = str((inp or {}).get("url") or "")
            ev["host"] = _url_host(url) or "search"
        ops = file_ops(tool, inp if isinstance(inp, dict) else {}, cwd)
        if tool == "Write" and paths:
            ops.setdefault(os.path.normpath(paths[0]), "write")
        elif tool in EDIT_TOOLS and paths:
            ops.setdefault(os.path.normpath(paths[0]), "modify")
        targets = []
        intents: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for p in list(paths) + [q for q in ops if q not in {os.path.normpath(x) for x in paths}]:
            np_ = os.path.normpath(p) if _isabs(p) else p
            if np_ in seen_paths:
                continue
            seen_paths.add(np_)
            op = ops.get(np_)
            t = self.target(p, "edit" if op else (touch or "hit"))
            if t:
                if op:
                    key = t.get("_key")
                    if op == "write":
                        if key in self._touched_keys:
                            op = "modify"
                        else:
                            # First Write to this path in the session: only "create" when
                            # the path didn't already exist on disk before this Write (a
                            # local stat, same trust level as the shell-path existence
                            # check above) — not merely unseen so far in-session.
                            with suppress(OSError):
                                op = "modify" if os.path.exists(np_) else "create"
                            if op == "write":
                                op = "create"
                    t["op"] = op
                    sk = (
                        _SKILL_FILE.search(np_.replace(os.sep, "/")) if tool in EDIT_TOOLS else None
                    )
                    if sk:
                        intents.append(
                            {
                                "type": "skill_write",
                                "name": sk.group(1)[:80],
                                "action": "create" if op == "create" else "update",
                            }
                        )
                if t.get("_key"):
                    self._touched_keys.add(t["_key"])
                targets.append(t)
        if targets:
            ev["targets"] = targets
        server = ev.get("server") if kind == "mcp" else None
        vcs = vcs_action(
            ev.get("tool", tool) if kind == "mcp" else tool,
            inp if isinstance(inp, dict) else {},
            server,
        )
        intent = pr_create_intent(tool, inp if isinstance(inp, dict) else {})
        if intent:
            vcs = {**(vcs or {}), "provider": intent["provider"], "action": "pr_create"}
            ev["_prIntent"] = intent
        if vcs:
            ev["vcs"] = vcs
        ev["cat"] = tool_category(ev)
        leaf = ev.get("tool") if kind == "mcp" else None
        if leaf is None and tool.lower() in _BARE_MCP_LEAVES:
            leaf = tool  # Codex reports MCP tools without their server
        intents.extend(
            output_intents(
                leaf if kind == "mcp" else tool,
                inp,
                ev.get("server") if kind == "mcp" else None,
                leaf,
            )
        )
        if intents:
            ev["_intents"] = intents
            base = inp.get("workdir") or cwd
            cmd = inp.get("command") or inp.get("cmd") or ""
            m = _CD_RE.match(cmd) if isinstance(cmd, str) else None
            if m:
                base = os.path.expanduser(m.group(2).strip())
            ev["_cwd"] = base
        return self.add(ev, ts)

    def resolve_outputs(self, ev: dict[str, Any], text: str, err: bool, ts: float | None) -> None:
        """Confirm a call's output intents against its result; emit output events."""
        intents = ev.pop("_intents", None)
        cwd = ev.pop("_cwd", None)
        if not intents:
            return
        found = None
        if cwd and _isabs(cwd):
            with captured():
                found = self.resolver.resolve(cwd)
        for it in intents:
            if it["type"] == "skill_write":
                # Status alone is not proof (Codex/Cursor report OK for a failed edit):
                # the result must not say the write failed either.
                core = _result_core(text or "")
                failed = err or "tool_use_error" in core or _FAILED_TEXT.search(core[:400])
                out = (
                    None
                    if failed
                    else {"kind": "skill_write", "name": it["name"], "action": it["action"]}
                )
            else:
                out = confirm_output(it, text, err, repo_name=found[1] if found else None)
            if (
                out
                and out["kind"] in ("pr_merge", "pr_close", "pr_comment")
                and not out.get("repo")
                and found
                and "/" in found[2]
            ):
                out["repo"] = found[2][:120]  # the checkout's own origin (owner/repo)
            if out:
                out.update(agent=ev["agent"], err=False, _ref=ev)
                self.add(out, ts)

    def llm_event(
        self,
        *,
        agent: str,
        model: str | None,
        usage: dict[str, Any],
        ts: float | None,
        meta: dict[str, Any] | None = None,
        aggregate: bool = False,
        cost_events: list[dict[str, Any]] | None = None,
    ) -> None:
        """One LLM request. ``meta`` carries response metadata only (never text):
        ``stop``, ``thinking``, ``toolCalls``, ``textChars``, ``latency``.

        ``aggregate`` marks ``usage`` as a turn's summed requests; ``cost_events``
        (the turn span's ``llm.cost_events``) then prices each request on its own,
        so the per-request long-context tier applies only where one request
        actually crossed it.
        """
        fresh = int(usage.get("input_tokens") or 0)
        cw = int(usage.get("cache_creation_input_tokens") or 0)
        cr = int(usage.get("cache_read_input_tokens") or 0)
        out = int(usage.get("output_tokens") or 0)
        ev: dict[str, Any] = {
            "kind": "llm",
            "agent": agent,
            "model": model or "",
            "in": fresh,
            "cw": cw,
            "cr": cr,
            "out": out,
        }
        cost = _cost_from_events(model, cost_events) if cost_events else None
        if cost is None:
            cost = estimate_cost_usd(model, fresh, cw, cr, out, single_request=not aggregate)
        if cost is not None:
            ev["cost"] = cost
        for k, v in (meta or {}).items():
            if v not in (None, "", 0, False) or k == "toolCalls":
                ev[k] = v
        self.add(ev, ts)

    def signal(
        self, *, agent: str, source: str, err_class: str, ts: float | None, **extra: Any
    ) -> dict[str, Any]:
        """An error signal that is not the result of one of our tool events."""
        ev: dict[str, Any] = {
            "kind": "error",
            "agent": agent,
            "source": source,
            "errClass": err_class,
            "err": True,
        }
        ev.update({k: v for k, v in extra.items() if v is not None})
        return self.add(ev, ts)

    def _derive(self, evs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Add delay events (slow tools, slow LLM responses, stalls). Returns new events."""
        extra: list[dict[str, Any]] = []
        for e in evs:
            ts = e.get("_ts")
            if ts is None:
                continue
            if e["kind"] not in ("llm", "user", "compaction", "error", "delay"):
                dur = e.get("dur") or 0
                if dur >= TOOL_DELAY_MS:
                    extra.append(
                        {
                            "kind": "delay",
                            "agent": e["agent"],
                            "err": False,
                            "cause": "subagent" if e["kind"] == "subagent" else "tool",
                            "ms": int(dur),
                            "_ref": e,
                            "_ts": ts + 0.0005,
                        }
                    )
            elif e["kind"] == "llm" and (e.get("latency") or 0) >= LLM_DELAY_MS:
                extra.append(
                    {
                        "kind": "delay",
                        "agent": e["agent"],
                        "err": False,
                        "cause": "llm",
                        "ms": int(e["latency"]),
                        "_ref": e,
                        "_ts": ts - e["latency"] / 1000.0,
                    }
                )
        main = [e for e in evs if e["agent"] == "main" and e.get("_ts") is not None]
        for prev, nxt in zip(main, main[1:], strict=False):
            gap = (nxt["_ts"] - prev["_ts"]) * 1000
            if gap >= STALL_MS and nxt["kind"] not in ("user",) and prev["kind"] != "user":
                covered = (prev.get("dur") or 0) >= gap * 0.8 or (
                    nxt.get("latency") or 0
                ) >= gap * 0.8
                if not covered:
                    extra.append(
                        {
                            "kind": "delay",
                            "agent": "main",
                            "err": False,
                            "cause": "stall",
                            "ms": int(gap),
                            "_ts": prev["_ts"] + 0.0005,
                        }
                    )
        return extra

    @staticmethod
    def _retry_key(e: dict[str, Any]) -> tuple[Any, ...] | None:
        if e["kind"] in _NON_CALL_KINDS:
            return None
        tg = (e.get("targets") or [{}])[0]
        return (
            e["agent"],
            e.get("tool"),
            e.get("server"),
            e.get("verb"),
            e.get("skill"),
            tg.get("file"),
            tg.get("label"),
        )

    def _mark_retries(self, evs: list[dict[str, Any]]) -> None:
        recent: dict[str, list[dict[str, Any]]] = {}
        for e in evs:
            key = self._retry_key(e)
            if key is None:
                continue
            window = recent.setdefault(e["agent"], [])
            prev = next((p for p in reversed(window) if self._retry_key(p) == key), None)
            if prev is not None and prev.get("err"):
                e["retryOf"] = prev["seq"]
                e["attempt"] = int(prev.get("attempt") or 1) + 1
            window.append(e)
            del window[:-RETRY_WINDOW]

    # -- finalize
    def finish(self, *, session: dict[str, Any]) -> dict[str, Any]:
        evs = [e for e in self.events if e.get("_ts") is not None] + [
            e for e in self.events if e.get("_ts") is None
        ]
        evs.sort(key=lambda e: e["_ts"] if e["_ts"] is not None else float("inf"))
        derived = self._derive(evs)
        room = max(MAX_EVENTS - len(evs), 0)  # raw events are already capped in add()
        if len(derived) > room:
            derived = derived[:room]
            self.truncated = True
        evs.extend(derived)
        evs.sort(key=lambda e: e["_ts"] if e["_ts"] is not None else float("inf"))
        switches = self._assign_models(evs)
        room = max(MAX_EVENTS - len(evs), 0)
        if len(switches) > room:
            switches = switches[:room]
            self.truncated = True
        if switches:
            evs.extend(switches)
            evs.sort(key=lambda e: e["_ts"] if e["_ts"] is not None else float("inf"))
        t0 = next((e["_ts"] for e in evs if e["_ts"] is not None), None)
        last_t = 0
        for i, e in enumerate(evs):
            for private in ("_intents", "_cwd", "_prIntent"):
                e.pop(private, None)
            ts = e.pop("_ts")
            e["t"] = int((ts - t0) * 1000) if (ts is not None and t0 is not None) else last_t
            last_t = e["t"]
            e["seq"] = i
            for tg in e.get("targets", []):
                key = tg.pop("_key", None)
                if key:
                    f = self.files[key]
                    f[{"read": "reads", "edit": "edits", "hit": "hits"}[tg["touch"]]] += 1
                    if f["first"] is None:
                        f["first"] = i
                    if tg.get("op"):
                        f["op"] = tg["op"]
        for e in evs:
            ref = e.pop("_ref", None)
            if ref is not None and "seq" in ref:
                e["ref"] = ref["seq"]
        self._mark_retries(evs)
        tasks = self._assign_tasks(evs, t0)
        # A PR-creating call past MAX_EVENTS never got a home (add() drops it without a
        # seq once truncated) — the PR still happened, but there's no event to link it
        # to, so it's dropped from the list rather than crashing the whole build.
        kept: list[dict[str, Any]] = []
        for pr in self.prs_created:
            ev_ref = pr.pop("_ev")
            if "seq" in ev_ref:
                pr["seq"] = ev_ref["seq"]
                kept.append(pr)
        self.prs_created = kept
        for a in self.agents:
            mine = [e for e in evs if e["agent"] == a["id"]]
            if not mine:
                continue
            a.update(
                start=mine[0]["t"],
                end=mine[-1]["t"],
                calls=sum(1 for e in mine if e["kind"] not in _NON_CALL_KINDS),
                requests=sum(1 for e in mine if e["kind"] == "llm"),
                tokens=sum(
                    e.get("in", 0) + e.get("cw", 0) + e.get("cr", 0) + e.get("out", 0) for e in mine
                ),
                cost=round(sum(e.get("cost", 0.0) for e in mine), 4),
                outcome=classify_outcome(mine, self.last_agent_text.get(a["id"], "")),
            )
        agents = [a for a in self.agents if "start" in a]
        llm = [e for e in evs if e["kind"] == "llm"]
        prs = [dict(pr) for pr in self.prs_created]
        created_numbers = [pr["number"] for pr in prs]
        for pr in prs:
            pr["state"] = "open"
            for e in evs:
                if e["kind"] not in ("pr_merge", "pr_close") or e.get("number") != pr["number"]:
                    continue
                if e.get("repo"):
                    if str(e["repo"]).lower() != str(pr["repo"]).lower():
                        continue  # same number in another repo: not this PR
                elif created_numbers.count(pr["number"]) != 1:
                    continue  # a repo-less outcome is only attributable when unambiguous
                pr["state"] = "merged" if e["kind"] == "pr_merge" else "closed"
        users = [e for e in evs if e["kind"] == "user" and e["agent"] == "main"]

        def _n(kind: str, **match: Any) -> int:
            return sum(
                1 for e in evs if e["kind"] == kind and all(e.get(k) == v for k, v in match.items())
            )

        doc = {
            "schema": SCHEMA,
            "session": {
                **session,
                "startedAt": _iso(t0) if t0 else session.get("startedAt"),
                "durationMs": evs[-1]["t"] if evs else 0,
                "models": sorted({e["model"] for e in llm if e.get("model")}),
                "pathMode": self.path_mode,
                "truncated": self.truncated,
                "outcome": classify_outcome(evs, self.last_agent_text.get("main", "")),
            },
            "totals": {
                "calls": sum(1 for e in evs if e["kind"] not in _NON_CALL_KINDS),
                "requests": len(llm),
                "userTurns": sum(1 for e in evs if e["kind"] == "user"),
                "subagents": len(agents) - 1 if agents else 0,
                "errors": sum(1 for e in evs if e.get("err")),
                "tokens": sum(e["in"] + e["cw"] + e["cr"] + e["out"] for e in llm),
                "costUsd": round(sum(e.get("cost", 0.0) for e in llm), 4),
                "filesTouched": len(self.files),
                "prsCreated": len(prs),
                "filesCreated": sum(1 for f in self.files.values() if f.get("op") == "create"),
                "filesDeleted": sum(1 for f in self.files.values() if f.get("op") == "delete"),
                **_signal_totals(evs),
                "tasks": len(tasks),
                "commits": _n("commit"),
                "prsMerged": _n("pr_merge"),
                "prsClosed": _n("pr_close"),
                "prComments": _n("pr_comment"),
                "ticketsCreated": _n("ticket", action="create"),
                "ticketsUpdated": _n("ticket", action="update"),
                "artifacts": _n("artifact"),
                "skillsCreated": _n("skill_write", action="create"),
                "skillsUpdated": _n("skill_write", action="update"),
                "modelSwitches": _n("model_switch"),
                "promptChars": sum(int(e.get("chars") or 0) for e in users),
                "promptImages": sum(int(e.get("images") or 0) for e in users),
                "promptUrls": sum(int(e.get("urls") or 0) for e in users),
            },
            "repos": list(self.repos.values()),
            "files": sorted(self.files.values(), key=lambda f: f["id"]),
            "agents": agents,
            "prs": prs,
            "tasks": tasks,
            "events": evs,
        }
        return doc

    def _assign_models(self, evs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Stamp each event with the model driving its agent; return model_switch events.

        An agent's model is its latest LLM request's model (its first one for events
        before any request; the harness default — Cursor's ``"default"`` — when the
        harness never names one). A ``subagent`` launch keeps its own ``model``: that
        is the *requested child* model.
        """
        first: dict[str, str] = {}
        for e in evs:
            if e["kind"] == "llm" and e.get("model"):
                first.setdefault(e["agent"], e["model"])
        last: dict[str, str] = {}
        switches: list[dict[str, Any]] = []
        for e in evs:
            agent = e["agent"]
            if e["kind"] == "llm":
                if not e.get("model"):
                    e["model"] = last.get(agent) or first.get(agent) or self.default_model
                prev = last.get(agent)
                if e.get("model") and prev and prev != e["model"] and e.get("_ts") is not None:
                    switches.append(
                        {
                            "kind": "model_switch",
                            "agent": agent,
                            "err": False,
                            "from": prev,
                            "to": e["model"],
                            "model": e["model"],
                            "_ts": e["_ts"] - 1e-6,
                        }
                    )
                if e.get("model"):
                    last[agent] = e["model"]
            elif not (e["kind"] == "subagent" and e.get("model")):
                m = last.get(agent) or first.get(agent) or self.default_model
                if m:
                    e["model"] = m
        return switches

    def _assign_tasks(self, evs: list[dict[str, Any]], t0: float | None) -> list[dict[str, Any]]:
        """Split the session into tasks (time windows); stamp each event's ``task``.

        Source ``segments``: the CLI's own segmentation of this session, cached on
        disk. Source ``turns`` (fallback): one task per user prompt, since a
        task starts at a prompt and runs until the next one. Events before the first
        boundary belong to task 1.
        """
        if not evs:
            return []
        specs: list[dict[str, Any]] = []
        if self.task_specs and t0 is not None:
            seen: set[int] = set()
            for sp in self.task_specs:
                st = _parse_ts(sp.get("startAt"))
                if st is None:
                    continue
                start = max(0, int((st - t0) * 1000))
                if start in seen:
                    continue
                seen.add(start)
                specs.append(
                    {
                        "id": f"task:{sp.get('n')}",
                        "name": (str(sp["name"])[:120] if sp.get("name") else None),
                        "source": "segments",
                        "startT": start,
                        "firstTurn": sp.get("firstTurn"),
                        "lastTurn": sp.get("lastTurn"),
                    }
                )
            specs.sort(key=lambda x: x["startT"])
        if not specs:
            users = [e for e in evs if e["kind"] == "user" and e["agent"] == "main"]
            specs = [
                {"id": f"turn:{i + 1}", "name": None, "source": "turns", "startT": u["t"]}
                for i, u in enumerate(users)
            ] or [{"id": "turn:1", "name": None, "source": "turns", "startT": 0}]
        specs[0]["startT"] = 0
        k = 0
        turn = 0
        for e in evs:
            while k + 1 < len(specs) and e["t"] >= specs[k + 1]["startT"]:
                k += 1
            e["task"] = k + 1
            if e["kind"] == "user" and e["agent"] == "main":
                turn += 1
                spec = specs[k]
                if spec["source"] == "turns":
                    spec.setdefault("firstTurn", turn)
                    spec["lastTurn"] = turn
        by_task: dict[int, list[dict[str, Any]]] = {}
        for e in evs:
            by_task.setdefault(e["task"], []).append(e)
        out = []
        for i, spec in enumerate(specs):
            mine = by_task.get(i + 1)
            if not mine:
                continue  # a boundary with no events of its own (e.g. two prompts, no work)
            n = len(out) + 1
            for e in mine:
                e["task"] = n
            llm = [e for e in mine if e["kind"] == "llm"]
            out.append(
                {
                    "n": n,
                    "id": spec["id"],
                    "name": spec["name"],
                    "source": spec["source"],
                    "startSeq": mine[0]["seq"],
                    "endSeq": mine[-1]["seq"],
                    "startT": mine[0]["t"],
                    "endT": mine[-1]["t"],
                    "firstTurn": spec.get("firstTurn"),
                    "lastTurn": spec.get("lastTurn"),
                    "requests": len(llm),
                    "calls": sum(1 for e in mine if e["kind"] not in _NON_CALL_KINDS),
                    "costUsd": round(sum(e.get("cost", 0.0) for e in llm), 4),
                }
            )
        return out


# ---------------------------------------------------------------- Claude Code
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def _count_images(content: Any) -> int:
    """Image blocks in a prompt — a count only, never the image."""
    if not isinstance(content, list):
        return 0
    return sum(1 for x in content if isinstance(x, dict) and x.get("type") == "image")


def _user_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in content):
            return None
        texts = [
            x["text"] for x in content
            if isinstance(x, dict) and x.get("type") == "text" and isinstance(x.get("text"), str)
        ]
        return " ".join(texts) if texts else None
    return None


def user_turn_text(rec: Any, agent_id: str = "main") -> str | None:
    """Prompt text of a Claude Code record that becomes a ``user`` event, else None.

    The single rule for "is this a user turn". Anything that answers None here
    contributes no ``user`` event, so it must not take a slot in anything
    indexed by turn either: task titles are looked up by ``firstTurn``, which
    counts *emitted* user events, and one extra entry shifts every later label.

    Args:
        rec: one decoded transcript record.
        agent_id: the agent the record is being read into; only ``main`` turns
            are emitted, subagent prompts never are.

    Returns:
        The prompt text (unstripped, as the event's ``chars`` counts it), or
        None for tool results, meta and compact-summary records, interruptions,
        harness-only turns, and records with no usable timestamp.
    """
    if not isinstance(rec, dict) or rec.get("type") != "user":
        return None
    if rec.get("isCompactSummary") or rec.get("isMeta") or agent_id != "main":
        return None
    if _parse_ts(rec.get("timestamp")) is None:
        return None
    msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
    text = _user_text(msg.get("content"))
    if text is None:
        return None
    stripped = text.lstrip()
    if not stripped or stripped.startswith("[Request interrupted") or stripped.startswith("<"):
        return None
    if "[SYSTEM NOTIFICATION" in stripped:
        return None
    return text


def user_turns(lines: Iterable[str]) -> list[tuple[int, dict[str, Any], str]]:
    """Every record of a Claude Code transcript that becomes a ``user`` event, in turn order.

    Turn ``k`` (1-based, as ``firstTurn`` counts it) is element ``k - 1``. The
    builder numbers turns after a stable sort of its events by timestamp, not in
    file order: a resumed session replays earlier prompts with their original
    timestamps, so file order would point every later turn at the wrong prompt.
    Anything indexed by turn (task titles, prompt images) must order the same way.

    Args:
        lines: raw transcript lines; ones that are not JSON are skipped.

    Returns:
        ``(line_no, record, text)`` per emitted turn, ``line_no`` being the
        0-based position of the record's line within ``lines``.
    """
    # Holds every user record (pasted images included) until the sort; keeping only
    # (timestamp, line_no) and re-reading records would cut memory on huge transcripts.
    turns: list[tuple[int, dict[str, Any], str]] = []
    for line_no, line in enumerate(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        text = user_turn_text(rec)
        if text is not None:
            turns.append((line_no, rec, text))
    # `user_turn_text` only accepts records with a parseable timestamp.
    turns.sort(key=lambda t: _parse_ts(t[1]["timestamp"]) or 0.0)
    return turns


def _read_claude_transcript(
    b: MapBuilder, path: Path, agent_id: str, child_links: dict[str, str]
) -> None:
    """One Claude Code JSONL (main or subagent) into events.

    LLM requests are keyed by message id: a streamed response spans several
    lines, so usage/stop reason come from the last line and content-derived
    metadata (thinking, tool-call count, text length) is summed across lines.
    Error text is read only to classify it (``classify_*``); it is never stored.
    """
    msgs: dict[str, dict[str, Any]] = {}
    pending: dict[str, dict[str, Any]] = {}
    tool_events: dict[str, dict[str, Any]] = {}  # tool_use id -> event; hook errors link back to it
    ready_ts: float | None = None  # when the agent last had new input (user turn / tool result)
    last_text = ""  # last assistant text block; only its length feeds classify_outcome, never stored
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(d, dict):
                continue
            ts = _parse_ts(d.get("timestamp"))
            if ts is None:
                continue
            msg = d.get("message") if isinstance(d.get("message"), dict) else {}
            typ = d.get("type")
            if typ == "system":
                sub = d.get("subtype")
                if sub == "compact_boundary":
                    cm = (
                        d.get("compactMetadata")
                        if isinstance(d.get("compactMetadata"), dict)
                        else {}
                    )
                    ev = {"kind": "compaction", "agent": agent_id}
                    for src, dst in (
                        ("trigger", "trigger"),
                        ("preTokens", "preTokens"),
                        ("postTokens", "postTokens"),
                        ("durationMs", "ms"),
                    ):
                        v = cm.get(src)
                        if isinstance(v, (int, float)) or (src == "trigger" and isinstance(v, str)):
                            ev[dst] = v if src == "trigger" else int(v)
                    b.add(ev, ts)
                elif sub == "model_refusal_fallback":
                    b.signal(
                        agent=agent_id,
                        source="llm",
                        err_class="refusal_fallback",
                        ts=ts,
                        retry=d.get("direction") == "retry",
                    )
                elif sub == "api_error" or (sub and "error" in str(sub)):
                    b.signal(
                        agent=agent_id,
                        source="llm",
                        err_class=classify_api_error(d.get("error"), _flat_text(d.get("content"))),
                        ts=ts,
                    )
                continue
            if typ == "attachment":
                att = d.get("attachment") if isinstance(d.get("attachment"), dict) else {}
                at = att.get("type")
                if at in ("hook_blocking_error", "hook_non_blocking_error"):
                    b.signal(
                        agent=agent_id,
                        source="hook",
                        err_class="hook_blocking" if at == "hook_blocking_error" else "hook_error",
                        ts=ts,
                        hook=str(att.get("hookName") or att.get("hookEvent") or "")[:60] or None,
                        hookEvent=str(att.get("hookEvent") or "")[:60] or None,
                        # The record's random id, so a local viewer can look the message up.
                        srcUuid=d["uuid"][:64] if isinstance(d.get("uuid"), str) else None,
                        _ref=tool_events.get(str(att.get("toolUseID"))),
                    )
                continue
            if typ == "user":
                if d.get("isCompactSummary"):
                    continue
                content = msg.get("content")
                text = _user_text(content)
                if (
                    text is not None
                    and not d.get("isMeta")
                    and text.lstrip().startswith("[Request interrupted")
                ):
                    b.signal(
                        agent=agent_id, source="orchestration", err_class="interrupted", ts=ts
                    )
                else:
                    turn = user_turn_text(d, agent_id)
                    if turn is not None:
                        b.add(
                            {
                                "kind": "user",
                                "agent": agent_id,
                                "chars": len(turn),
                                "images": _count_images(content),
                                "urls": len(_URL_RE.findall(turn)),
                            },
                            ts,
                        )
                        ready_ts = ts
                if isinstance(content, list):
                    for x in content:
                        if not isinstance(x, dict) or x.get("type") != "tool_result":
                            continue
                        ready_ts = ts
                        ev = pending.pop(x.get("tool_use_id"), None)
                        if ev is None:
                            continue
                        body = x.get("content")
                        ev["err"] = bool(x.get("is_error"))
                        if ev["err"]:
                            cls, code = classify_tool_error(_flat_text(body))
                            ev["errClass"] = cls
                            if code is not None:
                                ev["exitCode"] = code
                        ev["outBytes"] = (
                            len(json.dumps(body, ensure_ascii=False)) if body is not None else 0
                        )
                        if ev.get("_ts") is not None:
                            ev["dur"] = max(0, int((ts - ev["_ts"]) * 1000))
                        intent = ev.pop("_prIntent", None)
                        if intent:
                            hit = None if ev["err"] else _pr_from_url(_flat_text(body, 4000))
                            if hit and (
                                not intent.get("repo")
                                or hit["repo"].endswith(intent["repo"].split("/")[-1])
                            ):
                                ev["vcs"] = {
                                    "provider": hit["provider"],
                                    "action": "pr_create",
                                    "repo": hit["repo"],
                                    "number": hit["number"],
                                }
                                b.prs_created.append(
                                    {
                                        "provider": hit["provider"],
                                        "repo": hit["repo"],
                                        "number": hit["number"],
                                        "url": hit["url"],
                                        "_ev": ev,
                                    }
                                )
                            else:
                                # Optimistic action set at tool_event time didn't resolve to a
                                # real PR/MR URL: the command errored, or its output didn't
                                # contain one (e.g. `gh pr create --web`, a dry run).
                                ev["vcs"]["action"] = (
                                    "pr_create_failed" if ev["err"] else "pr_create_attempt"
                                )
                        b.resolve_outputs(ev, _flat_text(body, 20_000), ev["err"], ts)
                        res = d.get("toolUseResult")
                        if ev.get("kind") == "subagent" and isinstance(res, dict):
                            if res.get("agentId"):
                                child = "sa-" + str(res["agentId"])[:12]
                                ev["child"] = child
                                child_links[str(res["agentId"])] = child
                            status = str(res.get("status") or "")
                            if status in ("failed", "error", "killed", "cancelled"):
                                ev["err"] = True
                                ev.setdefault("errClass", "subagent_" + status)
                continue
            if typ == "assistant":
                if d.get("isApiErrorMessage"):
                    b.signal(
                        agent=agent_id,
                        source="llm",
                        err_class=classify_api_error(
                            d.get("error"), _flat_text(msg.get("content"))
                        ),
                        ts=ts,
                    )
                    continue
                mid = msg.get("id")
                if mid and msg.get("model") != "<synthetic>":
                    m = msgs.get(mid)
                    if m is None:
                        latency = (
                            int((ts - ready_ts) * 1000)
                            if ready_ts is not None and ts >= ready_ts
                            else None
                        )
                        m = msgs[mid] = {
                            "ts": ts,
                            "model": msg.get("model"),
                            "usage": {},
                            "thinking": False,
                            "toolCalls": 0,
                            "textChars": 0,
                            "stop": None,
                            "latency": latency,
                        }
                    if isinstance(msg.get("usage"), dict):
                        m["usage"] = msg["usage"]
                    if msg.get("stop_reason"):
                        m["stop"] = str(msg["stop_reason"])[:32]
                    for c in msg.get("content") or []:
                        if not isinstance(c, dict):
                            continue
                        if c.get("type") in ("thinking", "redacted_thinking"):
                            m["thinking"] = True
                        elif c.get("type") == "text":
                            text = str(c.get("text") or "")
                            m["textChars"] += len(text)
                            if text.strip():
                                last_text = text
                        elif c.get("type") == "tool_use":
                            m["toolCalls"] += 1
                for c in msg.get("content") or []:
                    if not isinstance(c, dict) or c.get("type") != "tool_use":
                        continue
                    ev = b.tool_event(
                        tool=str(c.get("name") or "tool"),
                        inp=c.get("input") or {},
                        cwd=d.get("cwd"),
                        agent=agent_id,
                        ts=ts,
                    )
                    if c.get("id"):
                        pending[c["id"]] = ev
                        tool_events[c["id"]] = ev
    for m in msgs.values():
        if not m["usage"]:
            continue
        b.llm_event(
            agent=agent_id,
            model=m["model"],
            usage=m["usage"],
            ts=m["ts"],
            meta={
                "stop": m["stop"],
                "thinking": m["thinking"],
                "toolCalls": m["toolCalls"],
                "textChars": m["textChars"],
                "latency": m["latency"],
            },
        )
    b.last_agent_text[agent_id] = last_text
    for ev in pending.values():  # tool calls that never got a result (killed, interrupted, crash)
        ev["err"] = True
        ev.setdefault("errClass", "no_result")
        ev.pop("_prIntent", None)
        if (ev.get("vcs") or {}).get("action") == "pr_create":
            ev["vcs"]["action"] = "pr_create_failed"


def build_claude_code_map(
    transcript: Path,
    *,
    path_mode: str = "relative",
    hmac_key: bytes | None = None,
    cli_version: str = "",
    resolver: RepoResolver | None = None,
    tasks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a map for one Claude Code session file (plus its subagent transcripts)."""
    transcript = Path(transcript)
    b = MapBuilder(path_mode=path_mode, hmac_key=hmac_key, resolver=resolver)
    b.task_specs = tasks
    links: dict[str, str] = {}
    _read_claude_transcript(b, transcript, "main", links)
    sub_dir = transcript.with_suffix("") / "subagents"
    if sub_dir.is_dir():
        for f in sorted(sub_dir.glob("agent-*.jsonl")):
            raw_id = f.stem[len("agent-") :]
            if raw_id.startswith("acompact-"):
                # Auto-compaction snapshots re-record parent API messages, not an agent's
                # work: a subagent would be fake and its llm events would double-count them.
                # The compaction call itself stays billed on the parent.
                continue
            aid = links.get(raw_id) or ("sa-" + raw_id[:12])
            sub_type, model = "general", ""
            meta = f.with_suffix(".meta.json")
            if meta.exists():
                try:
                    m = json.loads(meta.read_text(encoding="utf-8"))
                    sub_type = str(m.get("agentType") or m.get("subagent_type") or sub_type)[:40]
                    model = str(m.get("model") or "")[:60]
                except (ValueError, OSError):
                    pass
            launch = next((e for e in b.events if e.get("child") == aid), None)
            if launch is not None:
                sub_type = launch.get("subagentType", sub_type)
            b.agents.append(
                {
                    "id": aid,
                    "parent": "main",
                    "kind": "subagent",
                    "subagentType": sub_type,
                    **({"model": model} if model else {}),
                }
            )
            _read_claude_transcript(b, f, aid, {})
    doc = b.finish(
        session={
            "id": f"local:claude-code:{transcript.stem}",
            "agentTool": "claude-code",
            "cliVersion": cli_version,
            "role": _claude_role(transcript),
            "parentSessionId": None,
        }
    )
    for a in doc["agents"]:
        launch = next((e for e in doc["events"] if e.get("child") == a["id"]), None)
        if launch is not None:
            a["launchSeq"] = launch["seq"]
    return doc


def _claude_role(transcript: Path) -> str | None:
    """root / subagent / helper - the transcript's own classification, reused so the
    sidebar and the recorded facts can never disagree. ``None`` when it can't be read."""
    try:
        if transcript.stat().st_size > ROLE_READ_MAX_BYTES:
            return "root"  # classifying needs the whole file; a transcript this big is real work
        from ._claude_role import claude_subagent_transcript as _claude_subagent_transcript

        data = transcript.read_bytes()
        if _claude_subagent_transcript(data):
            return "subagent"
        return "root"
    except Exception:  # noqa: BLE001 — a role is metadata; never fail the map over it
        return None


# ---------------------------------------------------------------- generic spans
def _span_ts(v: Any) -> float | None:
    """Epoch seconds from a span time (ISO string or datetime); ``None`` if unusable."""
    try:
        if isinstance(v, str):
            return _parse_ts(v)
        if hasattr(v, "timestamp"):
            return float(v.timestamp())
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return None


def build_map_from_spans(
    spans: Iterable[Any],
    *,
    session_id: str,
    agent_tool: str,
    path_mode: str = "relative",
    hmac_key: bytes | None = None,
    cli_version: str = "",
    resolver: RepoResolver | None = None,
    tasks: list[dict[str, Any]] | None = None,
    role: str | None = None,
    parent_session_id: str | None = None,
    started_at_hint: float | None = None,
) -> dict[str, Any]:
    """Build a map from the shared ``LocalSource`` span tree (Codex, Cursor, Kiro, …).

    Turn spans become one ``llm`` event (turn-level tokens) plus a ``user`` event;
    tool spans (children of turns) become tool events. Subagent traffic is not in
    the span tree, so these maps have only the main agent.

    ``started_at_hint`` (epoch seconds, typically the source file's own mtime) is
    the session's ``startedAt`` when no span carries a timestamp — a real gap seen
    on tiny Cursor sessions (a handful of untimestamped events). Without it
    ``finish()``'s own fallback (``session.get("startedAt")``) has nothing to fall
    back to, since this dict never set that key, so the map ships with no
    ``startedAt`` at all and readers cannot place it on the timeline, so the
    map is invalid rather than merely incomplete.
    """
    b = MapBuilder(path_mode=path_mode, hmac_key=hmac_key, resolver=resolver)
    b.task_specs = tasks
    spans = list(spans)
    turn_ids = {s.span_id for s in spans if str(getattr(s, "name", "")).startswith("Turn ")}
    root = next((s for s in spans if getattr(s, "parent_id", None) is None), None)
    if root is not None:
        # Cursor stamps its model ("default", unresolved) on the session root only.
        b.default_model = str((getattr(root, "attributes", {}) or {}).get("llm.model_name") or "")[
            :60
        ]
    for s in spans:
        attrs = getattr(s, "attributes", {}) or {}
        ts = _span_ts(getattr(s, "start_time", None))
        if s.span_id in turn_ids:
            prompt = attrs.get("input.value")
            if prompt:
                b.add(
                    {
                        "kind": "user",
                        "agent": "main",
                        "chars": len(str(prompt)),
                        "images": 0,
                        "urls": len(_URL_RE.findall(str(prompt))),
                    },
                    ts,
                )
            usage = {
                "input_tokens": attrs.get("llm.token_count.input_fresh") or 0,
                "cache_creation_input_tokens": attrs.get("llm.token_count.cache_write") or 0,
                "cache_read_input_tokens": attrs.get("llm.token_count.cache_read") or 0,
                "output_tokens": attrs.get("llm.token_count.completion") or 0,
            }
            # One request per turn even when the parser has no usage (Cursor stamps zeros).
            events = attrs.get("llm.cost_events")
            b.llm_event(
                agent="main",
                model=attrs.get("llm.model_name"),
                usage=usage,
                ts=ts,
                aggregate=True,
                cost_events=events if isinstance(events, list) else None,
            )
            continue
        if getattr(s, "parent_id", None) not in turn_ids:
            continue
        raw = attrs.get("input.value")
        tool_name = str(attrs.get("tool.name") or s.name)
        try:
            inp = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except ValueError:
            # Codex code mode: the input is a JS snippet wrapping the shell command.
            inp = (
                {"command": loose_command(raw)}
                if tool_name in SHELL_TOOLS or tool_name == "js"
                else {}
            )
        ev = b.tool_event(
            tool=tool_name,
            inp=inp if isinstance(inp, dict) else {},
            cwd=attrs.get("cwd"),
            agent="main",
            ts=ts,
        )
        ev["err"] = getattr(s, "status_code", "") == "ERROR"
        end_ts = _span_ts(getattr(s, "end_time", None))
        if ts is not None and end_ts is not None and end_ts >= ts:
            ev["dur"] = int((end_ts - ts) * 1000)
        out = attrs.get("output.value")
        if out is not None:
            ev["outBytes"] = len(str(out))
        b.resolve_outputs(
            ev, str(out or "")[:20_000], ev["err"], end_ts if end_ts is not None else ts
        )
    return b.finish(
        session={
            "id": session_id,
            "agentTool": agent_tool,
            "cliVersion": cli_version,
            "role": role,
            "parentSessionId": parent_session_id,
            "startedAt": _iso(started_at_hint) if started_at_hint else None,
        }
    )


# ---------------------------------------------------------------- serialization
def canonical_bytes(doc: dict[str, Any]) -> bytes:
    """Deterministic JSON bytes (sorted keys, compact) — the content hash input."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def content_hash(doc: dict[str, Any]) -> str:
    """sha256 over the map minus volatile fields, so rebuilding an unchanged session is a no-op."""
    stable = {
        **doc,
        "session": {k: v for k, v in doc.get("session", {}).items() if k != "cliVersion"},
    }
    return hashlib.sha256(canonical_bytes(stable)).hexdigest()


def summary(doc: dict[str, Any]) -> dict[str, Any]:
    """The small index-row summary stored alongside each map in the local index."""
    s, t = doc["session"], doc["totals"]
    return {
        "startedAt": s.get("startedAt"),
        "durationMs": s.get("durationMs"),
        "agentTool": s.get("agentTool"),
        "models": s.get("models", []),
        "repos": [r["name"] for r in doc.get("repos", [])],
        "calls": t["calls"],
        "requests": t["requests"],
        "subagents": t["subagents"],
        "errors": t["errors"],
        "filesTouched": t["filesTouched"],
        "costUsd": t["costUsd"],
        "tokens": t["tokens"],
        "userTurns": t["userTurns"],
        "truncated": bool(s.get("truncated")),
        "errorsBySource": t.get("errorsBySource", {}),
        "retries": t.get("retries", 0),
        "delays": t.get("delays", 0),
        "maxContext": t.get("maxContext", 0),
        "compactions": t.get("compactions", 0),
        # The sidebar filters on role (main sessions only) and shows
        # outputs/PR state without opening the map.
        "role": s.get("role"),
        "parentSessionId": s.get("parentSessionId"),
        "tasks": t.get("tasks", 0),
        "outputs": {
            "commits": t.get("commits", 0),
            "prsCreated": t.get("prsCreated", 0),
            "prsMerged": t.get("prsMerged", 0),
            "prsClosed": t.get("prsClosed", 0),
            "prComments": t.get("prComments", 0),
            "ticketsCreated": t.get("ticketsCreated", 0),
            "ticketsUpdated": t.get("ticketsUpdated", 0),
            "artifacts": t.get("artifacts", 0),
            "skillsCreated": t.get("skillsCreated", 0),
            "skillsUpdated": t.get("skillsUpdated", 0),
        },
        "prs": [
            {k: pr.get(k) for k in ("repo", "number", "url", "state")}
            for pr in doc.get("prs", [])[:20]
        ],
    }
