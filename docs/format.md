# The `agentplay/v1` format

A **session map** is a redacted, chronological replay of one coding-agent session. It records every tool call, file touch, MCP call, skill, subagent and model request, in order, with repo-relative paths. It does not contain the content of any of them.

One map is one JSON object. Its machine-readable definition is [`schema/agentplay-v1.schema.json`](../schema/agentplay-v1.schema.json) (JSON Schema 2020-12). This page explains the meaning of each part.

- **Media type:** `application/json`. It is commonly stored gzipped as `*.json.gz`.
- **Schema id:** `"schema": "agentplay/v1"`. Readers must also accept `"entelligence.session-map/v1"`, the format's earlier name. The shape is identical.
- **Evolution:** fields are only ever added inside v1, never removed or repurposed. Readers must ignore fields they do not know. A breaking change would be `agentplay/v2`.

## Contents

- [Redaction contract](#redaction-contract)
- [Top level](#top-level)
- [`session`](#session)
- [`totals`](#totals)
- [`repos`, `files`](#repos-files)
- [`agents`](#agents)
- [`tasks`](#tasks)
- [`prs`](#prs)
- [`events`](#events)
- [Outcomes](#outcomes)
- [Content hash](#content-hash)
- [Writing a producer](#writing-a-producer)

## Redaction contract

These rules are hard rules. A producer that breaks one of them is not producing a valid session map.

1. **No text content.** A map never carries prompt text, assistant or reasoning text, tool inputs, tool outputs, file contents, or command arguments.
2. **Shell commands contribute a verb only.** The verb is the program plus a subcommand for multi-verb tools such as `git log`, `npm run`, `pytest` or `gh pr view`. Never flags, paths or values.
3. **Subagents contribute only `subagentType` and `model`.** Never their description or prompt.
4. **User turns contribute only shape:** `chars` (the length), plus counts of `images` and `urls`. The `images` count is the only trace of an image; producers must never embed image data.
5. **Model responses contribute only metadata:** token counts, latency, stop reason, whether there was thinking, the number of tool calls requested, and the length of text blocks (`textChars`).
6. **Paths.** Inside a git repository, a path is repo-relative. Outside any repository, it collapses to a bucket label: `/tmp (scratch)`, `~/Documents`, `~/Downloads`, `~/Desktop`, `~/.claude/<area>`, `~ (home)` or `outside-repo`.
7. **Path modes** (`session.pathMode`):
   - `relative` (default): repo-relative paths as above.
   - `dirs_only`: each file name becomes `•<sha256(path)[:6]>` (the repo-relative path); directories are kept. The hash is short and unkeyed, so a common name in a known directory (`README.md`, `package.json`) can be guessed by hashing candidates; use `hashed` with a key for stronger protection.
   - `hashed`: every path segment becomes `HMAC-SHA256(key, segment)[:10]`. Without a key, this mode falls back to `dirs_only`.

Identifiers that may appear are repository names and remotes, MCP server and tool names, skill names, subagent types, model names, web host names, hook names, and PR or ticket numbers and URLs. These are needed to make a map useful. Producers should treat them as metadata the user can see and remove.

The reference producer ([`python/src/agentplay/builder.py`](../python/src/agentplay/builder.py)) is tested against this contract. [`tests/test_builder.py::test_builder_never_leaks_free_text`](../python/tests/test_builder.py) builds a map from a transcript seeded with secret prompt text, secret arguments and secret subagent descriptions, and asserts that none of it appears in the output.

## Top level

```jsonc
{
  "schema": "agentplay/v1",
  "session": { … },
  "totals":  { … },
  "repos":   [ … ],
  "files":   [ … ],
  "agents":  [ … ],
  "tasks":   [ … ],   // optional
  "prs":     [ … ],
  "events":  [ … ]    // sorted by t; events[i].seq === i
}
```

## `session`

| field | type | meaning |
|---|---|---|
| `id` | string | Stable session id, conventionally `local:<agent>:<native id>` |
| `agentTool` | string | `claude-code`, `codex`, `cursor`, `copilot`, `copilot-vscode`, `opencode`, `kiro`, … |
| `cliVersion` | string | Version of the producer. Excluded from the content hash |
| `startedAt` | ISO-8601 UTC | Time of the first event |
| `durationMs` | number | Time from the first event to the last |
| `models` | string[] | Every model that served a request |
| `pathMode` | `relative` \| `dirs_only` \| `hashed` | See the redaction contract |
| `truncated` | bool | `true` when the producer stopped at its event cap (20,000 in the reference producer) |
| `outcome` | string | See [Outcomes](#outcomes) |
| `role` | `root` \| `subagent` \| `helper` \| `unknown` \| null | `subagent` and `helper` sessions are usually hidden from lists; their work already appears inside their parent |
| `parentSessionId` | string \| null | Set when `role` is `subagent` |

## `totals`

These are session-wide counters, so a list or dashboard never has to scan `events`.

| field | meaning |
|---|---|
| `calls` | tool calls. Excludes `llm`, `user`, `compaction`, `error` and `delay` events |
| `requests` | model requests |
| `userTurns` | user messages |
| `subagents` | subagent lanes |
| `errors` | events with `err: true` |
| `tokens` | all input, cache-write, cache-read and output tokens |
| `costUsd` | estimated cost at list prices. Advisory only |
| `filesTouched`, `filesCreated`, `filesDeleted` | distinct files |
| `errorsBySource` | `{tool, mcp, llm, hook, orchestration: n}` |
| `errorsByClass` | `{exit_code, timeout, rate_limit, …: n}` |
| `retries` | events with `retryOf` |
| `delays`, `delayMs` | delay events and their total duration |
| `maxContext` | largest main-agent context (`in + cw + cr`) seen |
| `compactions` | compaction events |
| `llmLatencyP50`, `llmLatencyP95` | model latency percentiles, in ms |
| `outputTokens` | output tokens only |
| `tasks` | number of task segments |
| `prsCreated`, `prsMerged`, `prsClosed`, `prComments`, `commits`, `ticketsCreated`, `ticketsUpdated`, `artifacts`, `skillsCreated`, `skillsUpdated`, `modelSwitches` | outputs the session produced (confirmed only) |
| `promptChars`, `promptImages`, `promptUrls` | the shape of what the user sent |

## `repos`, `files`

```jsonc
"repos": [{ "id": "r0", "name": "api", "remote": "acme/api" }],
"files": [{ "id": 0, "repo": "r0", "path": "src/orders.ts", "reads": 2, "edits": 2, "hits": 1, "first": 17, "op": "modify" }]
```

- **`files[].first`** is the `seq` of the event that first touched the file.
- **`files[].op`** is the last file operation seen for it: `create`, `modify`, `delete`, `rename-from` or `rename-to`.
- **Touch levels** (used by `targets[].touch`): `hit` means the file was named or listed, for example matched by a search. `read` means its contents were read. `edit` means it was changed.

## `agents`

```jsonc
"agents": [
  { "id": "main", "parent": null, "kind": "main", "start": 0, "end": 250000,
    "calls": 20, "requests": 16, "tokens": 900000, "cost": 1.61, "outcome": "pr_created" },
  { "id": "sa-ae824d54211f", "parent": "main", "kind": "subagent", "subagentType": "Explore",
    "model": "claude-haiku-4-5", "launchSeq": 14, "start": 31000, "end": 70000, "…": "same counters" }
]
```

Every event carries `agent`, which is `main` or one of these ids. `launchSeq` points at the `subagent` event that started the lane. A subagent has no further nesting.

## `tasks`

This field is optional. It records how the session divides into units of work:

```jsonc
"tasks": [{ "n": 1, "id": "t1", "name": "Explore the request pipeline", "source": "segments",
            "startSeq": 0, "endSeq": 12, "startT": 0, "endT": 70000, "firstTurn": 0, "lastTurn": 1 }]
```

- **`source`** is either `segments` or `turns`. `segments` means the tasks came from a task segmenter, which may use a model and produce names. `turns` means one task per user turn.
- **`startSeq` and `endSeq`** are inclusive.
- **`events[].task`** holds the task number `n`.
- **Fallback:** a malformed `tasks` block must never fail a reader. Drop it and fall back to one task per user turn.

## `prs`

```jsonc
"prs": [{ "provider": "github", "repo": "acme/api", "number": 142, "url": "https://github.com/acme/api/pull/142", "seq": 51, "state": "open" }]
```

A PR or MR is listed only once its URL is confirmed in a tool result. `seq` points at the event that created it.

## `events`

Every event has these fields:

| field | meaning |
|---|---|
| `seq` | index into `events` |
| `t` | ms since the first event |
| `kind` | see the table below |
| `agent` | `main` or a subagent id |
| `err` | the event failed |
| `task` | task number (when `tasks` is present) |
| `model` | the model driving the agent at this point |
| `cat` | tool category: `file`, `shell`, `vcs`, `test`, `cloud`, `http`, `fs`, `package`, `build`, `mcp`, `subagent`, `skill`, `web` or `tool` |

| kind | extra fields |
|---|---|
| `read`, `search`, `edit`, `exec`, `verify` | `tool`, `verb` (shell only), `targets[]`, `outBytes`, `dur`, `vcs?` |
| `mcp` | `server`, `tool`, `outBytes`, `dur` |
| `skill` | `skill` |
| `subagent` | `subagentType`, `model?`, `child` (agent id) |
| `web` | `host` (a hostname, or `search`) |
| `tool` | `tool`: any other tool (ToolSearch, AskUserQuestion, TodoWrite, …) |
| `llm` | `model`, `in` (fresh input), `cw` (cache write), `cr` (cache read), `out`, `cost?`, `latency`, `stop`, `thinking`, `toolCalls`, `textChars` |
| `user` | `chars`, `images`, `urls` |
| `error` | `source` (the builder writes `llm`, `hook` or `orchestration`; the schema also accepts `tool`, `mcp` and null, which a builder could use for a standalone error event, but a failed tool or MCP call is instead flagged on its own `tool`/`mcp` event and counted under that source in `totals.errorsBySource`), `errClass`, `hook?`, `hookEvent?`, `srcUuid?`, `ref?`, `retry?` |
| `delay` | `cause` (`tool`, `subagent`, `llm` or `stall`), `ms`, `ref?` (the causing event) |
| `compaction` | `trigger` (`auto` or `manual`), `preTokens`, `postTokens`, `ms` |
| `commit` | `repo`, `sha?`, `files?`, `ins?`, `del?` (git prints no sha or stats for `git commit -q`, so a quiet commit confirmed by its clean exit has neither) |
| `pr_merge`, `pr_close`, `pr_comment` | `repo`, `number`, `url?` |
| `ticket` | `tracker`, `action` (`create` or `update`), `id` |
| `artifact` | `url`, `title?`, `version?` |
| `skill_write` | `name`, `action` (`create` or `update`) |
| `model_switch` | `from`, `to` |

`verify` is a shell command recognised as a test, build, lint or type check (for example `pytest`, `pnpm test`, `tsc`, `cargo clippy`). Output kinds such as `commit` and `pr_merge` are emitted only when confirmed in a tool result.

### Targets

```jsonc
"targets": [{ "file": 0, "touch": "edit", "op": "modify" }, { "scope": "outside", "label": "/tmp (scratch)", "touch": "read" }]
```

### Signals

- **Failures.** A failed tool, MCP or subagent call can carry the following:
  - `errClass`: one of `exit_code`, `timeout`, `not_found`, `validation`, `permission_denied`, `blocked`, `stale_file`, `model_unavailable`, `rate_limit`, `api_error`, `interrupted`, `no_result`, `subagent_failed`, `subagent_killed` or `other`;
  - `exitCode`;
  - `retryOf`: the `seq` of the failed attempt it repeats (same agent, tool, verb or server, and first target, within the last 8 calls);
  - `attempt`: 2 or higher.
- **`error` events** record failures that are not tied to one tool call. Their `errClass` is one of `rate_limit`, `usage_limit`, `spend_limit`, `overloaded`, `server_error`, `auth`, `bad_request`, `api_error`, `refusal_fallback`, `hook_blocking`, `hook_error` or `interrupted`.
  - For a hook failure, `hook` is the hook's name (such as `PostToolUse:Bash`) and `hookEvent` the event it ran on (such as `PostToolUse`). `ref` is the `seq` of the tool call the hook ran after, when there is one. `srcUuid` is the id of the transcript record, an opaque random value that lets a viewer on the machine that recorded the session look the hook's message and command up; neither is ever stored.
- **`delay` events** are produced at these thresholds in the reference producer: a tool call of 30 s or more, model latency of 60 s or more, or a stall where the main agent was idle for 120 s or more while not waiting on the user.
- **`vcs`** is present on any VCS operation: `{provider: git|github|gitlab|bitbucket, action, repo?, number?}`.
  - For git, `action` is the subcommand. For gh or glab it is `<noun>_<verb>`.
  - `pr_create` is set only once the PR URL is confirmed. `pr_create_attempt` and `pr_create_failed` cover the rest.

## Outcomes

`session.outcome` and `agents[].outcome` take one of the values below. The first rule that matches wins.

1. `pr_created`: a confirmed PR or MR creation in the lane.
2. `interrupted`: an `interrupted` error in the last 12 events.
3. `blocked`: a `blocked` or `permission_denied` error in the last 12 events.
4. `verified`: the last `verify` event succeeded, with no edit after it.
5. `unresolved_errors`: 2 or more errors in the last 12 events.
6. `incomplete`: the lane ends on an edit or exec with no assistant text after it.
7. `completed`: none of the above.

## Content hash

`contentHash` is the SHA-256 of the canonical JSON of the map, with `session.cliVersion` removed. Canonical JSON means sorted keys, compact separators and UTF-8. Two producers that agree on a map produce the same hash, so independent builders can be checked against each other.

## Writing a producer

To make your agent's sessions work with the viewer:

1. Emit one map per session following this page. Start with the required fields: `schema`, `session.id`, `session.startedAt`, `totals` and `events[].t` and `kind`. Everything else is additive.
2. Validate your maps against the JSON Schema.
3. Keep to the redaction contract. If your agent's own logs contain text, drop it at the producer. Readers assume maps are safe to show.
4. Open a map by dragging the file onto the viewer, or add it to your local store with `agentplay import map.json.gz` so it shows in your session list. For a new agent, the better path is a parser in this repo; see [CONTRIBUTING.md](../CONTRIBUTING.md#adding-an-agent).
