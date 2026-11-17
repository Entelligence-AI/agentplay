"""Cohort regressions: literal wrappers, process completion and shell bodies."""

import json

import pytest

from agentplay._core.ingestion.coding_agent._sources.local.parsers.codex import parse_codex
from agentplay._core.ingestion.coding_agent.edit_lines import edit_line_counts
from agentplay._core.ingestion.coding_agent.file_paths import extract_all_file_paths
from agentplay._core.ingestion.coding_agent.native_operations import literal_operations
from agentplay._core.ingestion.coding_agent.produced import (
    creation_command_attributes,
    extract_created_prs,
)
from agentplay.builder import build_map_from_spans


@pytest.mark.parametrize(
    "code",
    [
        'text(await tools.exec_command({cmd:"gh pr create --fill"}));',
        'const r = await tools.exec_command({"cmd":"gh pr create --fill"}); text(r);',
    ],
)
def test_literal_wrapper(code):
    assert literal_operations(code) == [("exec_command", {"cmd": "gh pr create --fill"})]


@pytest.mark.parametrize(
    "code",
    [
        'if (false) { text(await tools.exec_command({cmd:"gh pr create"})); }',
        "text(await tools.exec_command({cmd:command}));",
        'text(await Promise.all([tools.exec_command({cmd:"gh pr create"})]));',
        'text(await tools.exec_command({cmd:"gh pr create"})); text("fake");',
        'text(await tools.exec_command({cmd:"gh pr create", cmd:"echo fake"}));',
        'const tools = await tools.exec_command({cmd:"gh pr create"}); text(tools);',
    ],
)
def test_unsupported_scripts_cannot_claim_operations(code):
    assert literal_operations(code) == []


@pytest.mark.parametrize(
    "command",
    [
        "cd /repo\ngh pr create --body \"$(cat <<'EOF'\nbody `not code`\nEOF\n)\" 2>&1",
        "cat > /tmp/body.md <<'EOF'\nbody\nEOF\ngh pr create --body-file /tmp/body.md",
        "git push -u origin feature && gh pr create --fill",
        "cd /repo && git add -A && git commit -m \"$(cat <<'EOF'\nfix: x\n\nbody\nEOF\n)\" "
        "&& git push -u origin HEAD && gh pr create --title t --body b",
    ],
)
def test_supported_cohort_shell_shapes(command):
    assert creation_command_attributes(command)["pr.create.provider"] == "github"


@pytest.mark.parametrize(
    "command",
    [
        "gh auth switch --user user1 2>&1 | tail -1\ngh pr create --fill\ngh auth switch --user user2 2>&1",
        # git preparation is accepted only as `&&`-joined add/commit/push
        # BEFORE the one create, with no redirect, pipe or fallback of its own.
        "git push; gh pr create --fill",
        "git push\ngh pr create --fill",
        "git push || true && gh pr create --fill",
        "git push 2>&1 | tail -1 && gh pr create --fill",
        "git push > /tmp/out && gh pr create --fill",
        "gh pr create --fill && git push",
        "git log && gh pr create --fill",
        "git push && gh pr create --web",
        "git push && gh pr create --fill | cat",
        "git push && gh pr create --fill && gh pr create --fill",
        "git diff && gh pr create --fill | head -n0",
        "echo 'gh pr create'",
        "false || gh pr create",
        "gh pr create; echo https://github.com/acme/repo/pull/1",
        'gh pr create --body "$(arbitrary-command)"',
        "cat <<EOF\ngh pr create\nEOF",
        "gh pr create && gh pr view 2",
    ],
)
def test_noncausal_shell_shapes(command):
    assert "pr.create.provider" not in creation_command_attributes(command)


def _parse(exit_code=0, chars="", launch_output="", final_output=None, host_error=False):
    url = "https://github.com/acme/repo/pull/7"
    rows = [
        {"type": "session_meta", "payload": {"id": "native", "cwd": "/repo"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "make a change"}],
            },
        },
    ]
    for call_id, code, result in [
        (
            "launch",
            'text(await tools.exec_command({cmd:"gh pr create --fill"}));',
            {"output": launch_output, "session_id": 42, "wall_time_seconds": 1},
        ),
        (
            "poll",
            "text(await tools.write_stdin("
            + json.dumps({"session_id": 42, "chars": chars})
            + "));",
            {
                "output": final_output if final_output is not None else url,
                "exit_code": exit_code,
                "isError": host_error,
                "wall_time_seconds": 1,
            },
        ),
    ]:
        rows.extend(
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "call_id": call_id,
                        "input": code,
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": call_id,
                        "output": [{"type": "text", "text": json.dumps(result)}],
                    },
                },
            ]
        )
    for index, row in enumerate(rows):
        row["timestamp"] = f"2026-09-21T01:00:{index:02d}Z"
    _, spans = parse_codex(
        data="\n".join(json.dumps(r) for r in rows).encode(), trace_id="local:codex:native"
    )
    return extract_created_prs(spans)


def test_async_completion_keeps_launch_identity():
    result = _parse()
    assert len(result) == 1
    assert result[0].source_event_id.endswith(":tool:launch:op:0")
    assert result[0].number == 7
    assert result[0].command_sha256


@pytest.mark.parametrize(
    "kwargs",
    [
        {"exit_code": 1},
        {"host_error": True},
        {"chars": "echo fake\n"},
        {"exit_code": None},
        {"launch_output": "error: creation failed\n"},
        {"launch_output": "https://github.com/acme/repo/pull/8\n"},
    ],
)
def test_async_failure_or_ambiguity_never_creates_link(kwargs):
    assert _parse(**kwargs) == []


def test_output_printed_before_exit_requires_successful_completion():
    assert len(_parse(launch_output="https://github.com/acme/repo/pull/7\n", final_output="")) == 1


def test_native_text_process_headers_are_not_read_from_stdout():
    from agentplay._core.ingestion.coding_agent.native_operations import shell_result

    result = shell_result("Chunk ID: abc\nProcess running with session ID 17\nOutput:\nwaiting")
    assert result["session_id"] == 17
    assert result["exit_code"] is None
    assert shell_result("Chunk ID: abc\nOutput:\nProcess exited with code 0") is None


def test_serial_native_input_text_result_header():
    from agentplay._core.ingestion.coding_agent.native_operations import literal_results

    blocks = [
        {"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"},
        {"type": "input_text", "text": '{"exit_code":0,"wall_time_seconds":0.1,"output":"done"}'},
    ]
    assert literal_results(blocks, 1)[0]["exit_code"] == 0
    assert literal_results([*blocks, {"type": "input_text", "text": "extra"}], 1) == []


def test_json_exponent_literal_preserves_operation():
    assert literal_operations(
        'text(await tools.exec_command({"cmd":"pwd","yield_time_ms":1e3}));'
    ) == [("exec_command", {"cmd": "pwd", "yield_time_ms": 1000})]


def _literal_patch_spans(patch, result):
    records = [
        {"type": "session_meta", "payload": {"id": "native", "cwd": "/repo"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "edit"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "patch",
                "input": "text(await tools.apply_patch(" + json.dumps(patch) + "));",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "patch",
                "output": [{"type": "text", "text": json.dumps(result)}],
            },
        },
    ]
    for index, row in enumerate(records):
        row["timestamp"] = f"2026-09-21T01:00:{index:02d}Z"
    _, spans = parse_codex(
        data="\n".join(json.dumps(row) for row in records).encode(), trace_id="native"
    )
    return spans


@pytest.mark.parametrize(
    "result",
    [
        {"exit_code": 1, "output": "failed"},
        {"isError": True, "output": "failed"},
        {"metadata": {"exit_code": 1}, "output": "failed"},
    ],
)
def test_failed_literal_patch_never_captures_edits(result):
    patch = "*** Begin Patch\n*** Update File: /repo/app.py\n@@\n-old\n+new\n*** End Patch"
    spans = _literal_patch_spans(patch, result)
    operation = next(span for span in spans if span.span_id.endswith(":op:0"))
    assert operation.status_code == "ERROR"
    assert operation.attributes["code.edit_evidence"]["edits"] == []


def test_literal_patch_names_its_files_like_a_direct_patch():
    patch = (
        "*** Begin Patch\n"
        "*** Update File: /repo/app.py\n@@\n-old\n+new\n"
        "*** Add File: /repo/new.py\n+x = 1\n"
        "*** End Patch"
    )
    spans = _literal_patch_spans(patch, {"exit_code": 0, "output": "Success."})
    operation = next(span for span in spans if span.span_id.endswith(":op:0"))
    value = operation.attributes["input.value"]

    assert extract_all_file_paths(value) == {"/repo/app.py", "/repo/new.py"}
    assert edit_line_counts(operation.name, value) == (2, 1)


def test_literal_patch_edit_reaches_the_map(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    patch = f"*** Begin Patch\n*** Update File: {repo}/app.py\n@@\n-old\n+new\n*** End Patch"
    spans = _literal_patch_spans(patch, {"exit_code": 0, "output": "Success."})

    doc = build_map_from_spans(spans, session_id="native", agent_tool="codex")

    assert [f["path"] for f in doc["files"]] == ["app.py"]


def test_nested_arguments_do_not_abort_codex_transcript():
    rows = [
        {"type": "session_meta", "payload": {"id": "native", "cwd": "/repo"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "work"}]}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "call_id": "nested", "arguments": "[" * 2000 + "0" + "]" * 2000}},
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "nested", "output": '{"output":"done","exit_code":0}'}},
    ]
    _, spans = parse_codex("\n".join(json.dumps(row) for row in rows).encode(), trace_id="native")
    assert any(span.span_id.endswith(":tool:nested") for span in spans)
    assert extract_created_prs(spans) == []


_PATCH = "*** Begin Patch\n*** Update File: /repo/app.py\n@@\n-old\n+new\n*** End Patch"


@pytest.mark.parametrize(
    "code",
    [
        "const patch = PATCH; text(await tools.apply_patch(patch));",
        "const patch=PATCH;text(await tools.apply_patch(patch))",
        "const patch = PATCH; const r = await tools.apply_patch(patch); "
        'text(typeof r === "string" ? r : JSON.stringify(r));',
        "const p=PATCH;const r=await tools.apply_patch(p);"
        'text(typeof r==="string"?r:JSON.stringify(r))',
    ],
)
def test_codex_bound_patch_is_a_literal_operation(code):
    assert literal_operations(code.replace("PATCH", json.dumps(_PATCH))) == [
        ("apply_patch", _PATCH)
    ]


@pytest.mark.parametrize(
    "code",
    [
        "let patch = PATCH; patch = PATCH; text(await tools.apply_patch(patch));",
        "const patch = PATCH + PATCH; text(await tools.apply_patch(patch));",
        "text(await tools.apply_patch(patch)); const patch = PATCH;",
        "const patch = PATCH; const patch = PATCH; text(await tools.apply_patch(patch));",
        "const true = PATCH; text(await tools.apply_patch(true));",
        "const p = PATCH; for (const x of [1]) { text(await tools.apply_patch(p)); }",
        # Emits something other than the awaited result: order proves nothing.
        "const p = PATCH; const r = await tools.apply_patch(p); "
        'text(typeof r === "string" ? p : JSON.stringify(r));',
        "const p = PATCH; const r = await tools.apply_patch(p);",
    ],
)
def test_codex_bound_literal_shapes_that_prove_nothing(code):
    assert literal_operations(code.replace("PATCH", json.dumps(_PATCH))) == []


def test_codex_bound_patch_captures_edit_evidence():
    code = "const patch = " + json.dumps(_PATCH) + "; text(await tools.apply_patch(patch));"
    records = [
        {"type": "session_meta", "payload": {"id": "native", "cwd": "/repo"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "edit"}],
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "c", "input": code},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "c",
                "output": [
                    {
                        "type": "input_text",
                        "text": "Script completed\nWall time 0.1 seconds\nOutput:\n",
                    },
                    {"type": "input_text", "text": "{}"},
                ],
            },
        },
    ]
    for index, row in enumerate(records):
        row["timestamp"] = f"2026-09-21T01:00:{index:02d}Z"
    _, spans = parse_codex(
        data="\n".join(json.dumps(row) for row in records).encode(), trace_id="native"
    )
    operation = next(span for span in spans if span.span_id.endswith(":op:0"))
    assert operation.attributes["provenance.capture"] == "literal_operation"
    evidence = operation.attributes["code.edit_evidence"]
    assert evidence["status"] == "captured"
    assert [(e["path"], e["operation"]) for e in evidence["edits"]] == [("/repo/app.py", "replace")]
