# Contributing to agentplay

Thanks for helping. This page covers the most useful kinds of contribution, the rules every change has to keep, and how to run everything locally.

## Before you build a feature

agentplay is maintained by Entelligence on a product roadmap. Parsers for new agents, bug fixes and documentation are always welcome as pull requests. For a new feature, please open an issue first so we can agree it fits before you spend time on it; we may decline a good feature that doesn't.

## Ground rules

1. **Privacy is the product.** A change must never cause a map, a log line, or the viewer to contain prompt text, model output, tool input or output, file contents, or command arguments. The full rules are in [docs/format.md](docs/format.md#redaction-contract). If you are not sure, ask in the PR.
2. **No new network calls.** The only one is the opt-in email signup in `account.py`, and the README promises that. Session data never goes over the network, and a feature that needs the network does not belong in the core tool. `tests/test_account.py` fails if `urllib.request` appears in any other file.
3. **No runtime dependencies** for the Python package. It runs on the standard library so that `uvx agentplay` starts instantly and installs cleanly everywhere. Dev and test dependencies are fine.
4. **Keep internal references out.** `make check` runs `scripts/check_clean.py`, which rejects ticket ids, private links and credential shapes.

## Setup

```bash
make setup        # python/.venv (pytest, ruff, jsonschema) + viewer/node_modules
make test         # everything
make check        # lint + typecheck + tests + hygiene guard, what CI runs
```

**On Windows**, `make` works under Git Bash or MSYS2, or in WSL. Without `make`, follow the PowerShell steps under "Windows" in the [README](README.md#install). To run the Python tests directly:

```powershell
cd python
.venv\Scripts\python -m pytest -q
```

Python code lives in `python/src/agentplay`, and its tests are in `python/tests`, with parser tests in `python/tests/core`. The viewer lives in `viewer/`; see [viewer/README.md](viewer/README.md).

## Adding an agent

A parser turns one agent's transcript into the shared span shape: a root Session span, one Turn span per turn, and one tool span per tool call. Everything downstream (the builder, the viewer) then works without further changes.

1. **Get real transcripts.** Use two or three real sessions from the agent, and include one with a failed tool call if you can. Strip any personal content before committing them as fixtures. Keep them small.
2. **Write the parser** as `python/src/agentplay/_core/ingestion/coding_agent/_sources/local/parsers/<agent>.py`, exposing `parse_<agent>(data, trace_id, user_id=None, project_name=None) -> (TraceData, list[SpanData])`. [`codex.py`](python/src/agentplay/_core/ingestion/coding_agent/_sources/local/parsers/codex.py) is a good one to copy. Map the agent's tool names onto the canonical taxonomy in [`tool_taxonomy.py`](python/src/agentplay/_core/ingestion/coding_agent/tool_taxonomy.py) so that reads, edits and shell calls are classified correctly.
3. **Register it:**
   - add a value to `AgentType` and a sniffing rule to `detect_agent` in `parsers/base.py`;
   - export it from `parsers/__init__.py`;
   - add an indexer for the agent's home layout in `_sources/local/source.py`.
4. **If the agent stores sessions in a database** rather than files, add a decoder to [`materialize.py`](python/src/agentplay/materialize.py) that writes one NDJSON transcript per session into the cache, following the OpenCode one. Open databases read-only (`mode=ro&immutable=1`), or copy them first if they are WAL-mode and may be live.
5. **Tell discovery where it lives.** Add the default home, and a `AGENTPLAY_<AGENT>_HOMES` override, to `Homes` in [`discover.py`](python/src/agentplay/discover.py). Add the agent to `AGENTS`, and to the tables in the README.
6. **Test it.** Add parser tests in `python/tests/core/`. Also add one test that builds a map from your fixture and asserts that no text from it appears in the map (see `test_builder_never_leaks_free_text`).

## Changing the format

`agentplay/v1` only grows:

- add fields, never rename or repurpose one;
- update [docs/format.md](docs/format.md), [schema/agentplay-v1.schema.json](schema/agentplay-v1.schema.json) and the viewer's zod schema (`viewer/src/core/schema.ts`) in the same PR;
- keep new fields optional for readers, since older maps will not have them.

## Using AI tools

AI coding tools are welcome. You stay responsible for everything you submit.

- **A human must be in the loop.** Agents may open pull requests, but a person must have reviewed the diff, understand every line and answer review questions. We close pull requests with no one behind them.
- **The description must be true and clear.** You or your agent can write it. Either way, it says what changed and why, matches the diff, and stays short: no filler, no restating the code.
- **Never claim a check you did not run.** The test plan says what you actually ran and saw, and what you did not test.

## Pull requests

- Keep each PR to one change, and include tests that fail without it.
- Run `make check` before pushing.
- Fill in the PR template: a short summary of what changed and why, the linked issue, and a test plan.
- For UI changes, attach before and after screenshots in light and dark (`pnpm screenshots` in `viewer/`).
- By contributing, you agree that your contribution is licensed under the Apache License 2.0.

## Releasing

Maintainers: see [docs/RELEASING.md](docs/RELEASING.md) for the release steps.

## Code of conduct

Participation in this project is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).
