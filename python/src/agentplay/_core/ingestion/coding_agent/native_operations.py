"""Decode literal, serial Codex tool bundles without evaluating JavaScript.

Only statements that emit their own awaited tool result are supported. Branches,
loops, concurrency, computed arguments and extra output remain unsupported: their
textual call order cannot prove which result belongs to which operation.
"""

from __future__ import annotations

import json
import re
from typing import Any

from agentplay._core.ingestion.coding_agent.produced import is_pr_creation_tool

_TOKEN = re.compile(
    r'\s*("(?:\\.|[^"\\])*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[A-Za-z_$][\w$]*|[^\s])'
)
_NAME = re.compile(r"[A-Za-z_$][\w$]*")
_TOOLS = {"exec_command", "write_stdin", "apply_patch"}
# `text(typeof R === "string" ? R : JSON.stringify(R))` for a binding R emits
# exactly what `text(R)` emits, once, so it proves no more than the plain form.
_STRINGIFIED = [m[1] for m in _TOKEN.finditer('typeof R === "string" ? R : JSON.stringify(R)')]


def literal_operations(code: str) -> list[tuple[str, Any]]:
    """Return ordered literal calls only when the entire emitted script is safe.

    Supports ``text(await tools.NAME(JSON))`` and an immediately emitted local
    binding. A ``const NAME = <literal>;`` binding may stand in for a literal
    argument (Codex binds a patch before ``tools.apply_patch(NAME)``); it emits
    nothing, so it cannot shift the result order. Unquoted object keys are
    accepted; expressions are not. Unsupported input returns an empty list and
    must retain its original native tool record.
    """
    if not isinstance(code, str) or len(code) > 2_000_000:
        return []
    code = re.sub(r"\A\s*// @exec:[^\n]*\n", "", code)
    tokens = [match[1] for match in _TOKEN.finditer(code)]
    pos = 0
    literals: dict[str, Any] = {}

    def take(expected: str | None = None) -> str:
        nonlocal pos
        token = tokens[pos]
        pos += 1
        if expected is not None and token != expected:
            raise ValueError("unsupported expression")
        return token

    def value(depth: int = 0) -> Any:
        if depth > 20:
            raise ValueError("nested literal limit")
        token = take()
        if token in literals:
            return literals[token]
        if token == "{":
            result = {}
            while tokens[pos] != "}":
                key = take()
                key = json.loads(key) if key.startswith('"') else key
                if not isinstance(key, str) or not _NAME.fullmatch(key):
                    raise ValueError("unsupported key")
                if key in result or key in {"__proto__", "constructor", "prototype"}:
                    raise ValueError("ambiguous object")
                take(":")
                result[key] = value(depth + 1)
                if tokens[pos] != "}":
                    take(",")
            take("}")
            return result
        if token == "[":
            result = []
            while tokens[pos] != "]":
                result.append(value(depth + 1))
                if tokens[pos] != "]":
                    take(",")
            take("]")
            return result
        return json.loads(token)

    operations = []
    try:
        while pos < len(tokens):
            binding = None
            if tokens[pos] in {"const", "let"}:
                take()
                binding = take()
                if (
                    not _NAME.fullmatch(binding)
                    or binding in {"tools", "text", "true", "false", "null"}
                    or binding in literals
                ):
                    return []
                take("=")
                if tokens[pos] != "await":
                    literals[binding] = value()
                    if pos < len(tokens) and tokens[pos] == ";":
                        take()
                    continue
            else:
                take("text")
                take("(")
            take("await")
            take("tools")
            take(".")
            tool = take()
            if tool not in _TOOLS and not is_pr_creation_tool(tool):
                return []
            take("(")
            args = value()
            take(")")
            if binding is None:
                take(")")
            else:
                if pos < len(tokens) and tokens[pos] == ";":
                    take()
                take("text")
                take("(")
                stringified = [binding if t == "R" else t for t in _STRINGIFIED]
                if tokens[pos : pos + len(stringified)] == stringified:
                    pos += len(stringified)
                else:
                    take(binding)
                take(")")
            if pos < len(tokens) and tokens[pos] == ";":
                take()
            if tool == "apply_patch":
                if not isinstance(args, str):
                    return []
            elif not isinstance(args, dict):
                return []
            operations.append((tool, args))
            if len(operations) > 100:
                return []
    except (ValueError, IndexError, RecursionError):
        return []
    return operations


def literal_results(output: object, count: int) -> list[dict[str, Any]]:
    """Validate one structured host result per emitted operation, in order."""
    if not isinstance(output, list):
        return []
    if (
        output
        and isinstance(output[0], dict)
        and re.fullmatch(
            r"Script completed\nWall time [\d.]+ seconds\nOutput:\n?",
            str(output[0].get("text", "")),
        )
    ):
        output = output[1:]
    if len(output) != count:
        return []
    results = []
    for block in output:
        if not isinstance(block, dict) or block.get("type") not in {
            "text",
            "input_text",
            "output_text",
        }:
            return []
        try:
            result = json.loads(block["text"])
        except (KeyError, TypeError, ValueError):
            return []
        if not isinstance(result, dict):
            return []
        results.append(result)
    return results


def shell_result(output: str) -> dict[str, Any] | None:
    """Decode structured or native textual host metadata before the output body."""
    try:
        result = json.loads(output)
    except (ValueError, TypeError):
        result = None
    if isinstance(result, dict):
        return result
    if not isinstance(output, str):
        return None
    parts = re.split(r"\n(?:Final output|Output):\n", output, maxsplit=1)
    if len(parts) != 2:
        return None
    header, body = parts
    exited = re.search(r"^Process exited with code (-?\d{1,10})$", header, re.MULTILINE)
    running = re.search(r"^Process running with session ID (\d+)$", header, re.MULTILINE)
    if bool(exited) == bool(running):
        return None
    return {
        "output": body,
        "wall_time_seconds": 0,
        "exit_code": int(exited[1]) if exited else None,
        "session_id": int(running[1]) if running else None,
    }


class ProcessEvidence:
    """Correlate native shell completion with its launching tool within a session.

    Retains references to original tool records so later polls update the launch
    evidence without attributing creation to a poll or duplicating tool events.
    Interactive input invalidates the pending command; process reuse replaces it.
    The map is private to a single transcript parse, never shared across sessions.
    """

    def __init__(self) -> None:
        self.pending: dict[int, dict] = {}

    def observe(self, tool: dict, args: Any, result: dict) -> None:
        """Attach complete successful output to its original launch record."""
        from agentplay._core.ingestion.coding_agent.produced import creation_output_attributes

        name = tool["name"]
        if name not in {"exec_command", "write_stdin"} or not isinstance(args, dict):
            return
        process_id = result.get("session_id") if name == "exec_command" else args.get("session_id")
        origin = tool
        if name == "write_stdin":
            if type(process_id) is not int:
                return
            origin = self.pending.get(process_id)
            if origin is None:
                return
            if args.get("chars", "") or args.get("terminate"):
                self.pending.pop(process_id, None)
                return
        elif type(process_id) is int:
            self.pending.pop(process_id, None)
        if result.get("isError") is True:
            if type(process_id) is int:
                self.pending.pop(process_id, None)
            origin["pr.create.output_url"] = ""
            origin["is_error"] = True
            origin["provenance.capture"] = "failed_process"
            return
        exit_code = result.get("exit_code")
        output = result.get("output")
        if not isinstance(output, str) or "wall_time_seconds" not in result:
            return
        output = origin.get("_process_output", "") + output
        if len(output) > 2_000_000:
            if type(process_id) is int:
                self.pending.pop(process_id, None)
            origin["pr.create.output_url"] = ""
            origin["is_error"] = True
            return
        origin["_process_output"] = output
        # A running chunk is never a completed create, even if it prints a URL.
        origin["pr.create.output_url"] = ""
        origin["is_error"] = True
        if exit_code is None and type(process_id) is int:
            origin["provenance.capture"] = "pending_process"
            if len(self.pending) < 1000:
                self.pending[process_id] = origin
            return
        if type(process_id) is int:
            self.pending.pop(process_id, None)
        if type(exit_code) is int:
            origin["provenance.capture"] = "completed_process"
            # Code mode keeps the exit code out of the output text, so carry it
            # separately for the error class.
            origin["process.exit_code"] = exit_code
        if type(exit_code) is int and exit_code == 0:
            origin.update(
                creation_output_attributes(
                    output, origin.get("pr.create.provider"), origin.get("pr.create.repository")
                )
            )
            origin["output"] = output[:2000]
            origin["end_ts"] = tool["end_ts"]
            origin["is_error"] = False
