"""opencode's patch tools pass the body as `patchText`; it must read like Codex's apply_patch."""

import json

import pytest

from agentplay._core.ingestion.coding_agent._sources.local.parsers.opencode import parse_opencode
from agentplay._core.ingestion.coding_agent.edit_lines import edit_line_counts
from agentplay._core.ingestion.coding_agent.file_paths import extract_all_file_paths
from agentplay._core.ingestion.coding_agent.metrics import compute_task_counters
from agentplay.builder import build_map_from_spans

T0 = 1793000000000


def _patch(root: str, filler: int = 0) -> str:
    body = "".join(f"+line {i}\n" for i in range(filler))
    return (
        "*** Begin Patch\n"
        f"*** Update File: {root}/src/app.py\n"
        "@@\n"
        "-old = 1\n"
        "+new = 2\n"
        f"*** Add File: {root}/src/new.py\n"
        "+x = 1\n"
        f"{body}"
        "*** End Patch"
    )


def _spans(tool: str, patch: str, cwd: str = "/repo"):
    records = [
        {
            "id": "m1",
            "sessionID": "s1",
            "message": {"role": "user", "time": {"created": T0}},
            "parts": [{"type": "text", "text": "fix it"}],
        },
        {
            "id": "m2",
            "sessionID": "s1",
            "message": {
                "role": "assistant",
                "modelID": "gpt-5",
                "time": {"created": T0 + 1000},
                "path": {"cwd": cwd},
            },
            "parts": [
                {
                    "type": "tool",
                    "tool": tool,
                    "callID": "c1",
                    "state": {
                        "status": "completed",
                        "input": {"patchText": patch},
                        "output": "Success.",
                        "time": {"start": T0 + 1100, "end": T0 + 1200},
                    },
                }
            ],
        },
    ]
    data = ("\n".join(json.dumps(r) for r in records) + "\n").encode()
    return parse_opencode(data, "local:opencode:s1")[1]


def _tool_span(spans):
    (span,) = [s for s in spans if ":tool:" in s.span_id]
    return span


@pytest.mark.parametrize("tool", ["apply_patch", "patch"])
def test_patch_text_gives_edit_evidence_files_and_line_counts(tool):
    span = _tool_span(_spans(tool, _patch("/repo")))
    attrs = span.attributes

    assert span.name == "Edit"
    evidence = attrs["code.edit_evidence"]
    assert evidence["status"] == "captured"
    assert sorted(e["path"] for e in evidence["edits"]) == ["/repo/src/app.py", "/repo/src/new.py"]
    assert extract_all_file_paths(attrs["input.value"]) == {"/repo/src/app.py", "/repo/src/new.py"}
    assert edit_line_counts(span.name, attrs["input.value"]) == (2, 1)
    assert compute_task_counters(_spans(tool, _patch("/repo")), start_turn=1, end_turn=1).edit_count == 1


def test_long_patch_keeps_its_files_and_counts_past_the_input_cap():
    span = _tool_span(_spans("apply_patch", _patch("/repo", filler=400)))
    attrs = span.attributes

    assert len(attrs["input.value"]) == 2000
    assert extract_all_file_paths(attrs["input.value"]) == {"/repo/src/app.py", "/repo/src/new.py"}
    assert edit_line_counts(span.name, attrs["input.value"]) == (402, 1)


def test_patch_edit_reaches_the_map(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    patch = (
        "*** Begin Patch\n"
        f"*** Update File: {repo}/src/app.py\n"
        "@@\n"
        "-old = 1\n"
        "+new = 2\n"
        "*** End Patch"
    )

    doc = build_map_from_spans(_spans("apply_patch", patch, cwd=str(repo)), session_id="s", agent_tool="opencode")

    assert [f["path"] for f in doc["files"]] == ["src/app.py"]
    (edit,) = [e for e in doc["events"] if e["kind"] == "edit"]
    assert edit["targets"][0]["touch"] == "edit"
