# Task fit and self-improving setups for multi-agent developers

Research date: 2026-10-24. Scope: developers who mix Claude Code (Opus, Sonnet, Haiku, Fable), Codex, Cursor, Gemini CLI.

Source caveats up front:
- reddit.com is blocked to our fetcher. Every "Reddit consensus" claim below is secondhand, via SEO blogs that paraphrase threads. HN items are primary.
- Model versions in third-party sources conflict badly (Opus 4.6 / 4.8 / 5 / 5.5, Fable 5 reported both as pulled and as available). Treat any version-specific claim as date-stamped to its source, not current.

---

## A. Which agent/model for which task

### A1. Public benchmarks vs real-world reports

- Benchmarks disagree with each other by task shape. Terminal-Bench 2.x measures model plus harness plus shell; SWE-bench Pro is contamination-controlled issue resolution. Rankings flip between them (e.g. GLM-5.2 beats GPT-5.5 on SWE-bench Pro, loses on Terminal-Bench 2.1). SWE-bench Verified is considered contaminated; OpenAI stopped reporting it in Sept 2025. [morphllm Terminal-Bench](https://www.morphllm.com/terminal-bench-2), [morphllm SWE benchmark](https://www.morphllm.com/swe-benchmark), [codersera roundup](https://codersera.com/blog/ai-agent-benchmarks-state-of-leaderboard-may-2026/amp/)
- Augment's routing guide admits SWE-bench "likely overstates the quality difference" for navigation and small edits, and that no public benchmark measures those. [Augment](https://www.augmentcode.com/guides/ai-model-routing-guide)
- The strongest real-world measurement found: Insight APAC's "cost to a merged feature". Same feature, ~12 models x 3 harnesses (Claude Code, Copilot, OpenCode), constant Opus reviewer gate plus 38 hidden unit tests. Findings: $3 to $33 per merge; same model varies ~2.5x by harness (Sonnet 4.6: $9.78 on Claude Code vs $29.38 on OpenCode, mostly a default reasoning-effort setting); per-token price does not predict cost to merge (cheap coders push cost onto review rounds); the reviewer approved code passing as few as 12/38 tests ("merged is not correct"); a stronger planner lifted a capable coder from ~12 to 27-29/38; single runs did not replicate. Recommendation: measure on your own shipped work. [Insight APAC](https://blog.apac.insight.com/2026/07/06/cost-to-a-merged-feature)
- Agentic PR studies (MSR 2026 mining challenge, AIDev dataset): of rejected agent PRs only 35.7% show clear agent failure, 31.2% are workflow constraints, 33.1% have no visible rationale. So "PR rejected" overstates agent error. Codex and Cursor PRs tend to merge with little interaction; Copilot and Devin are more reviewer-mediated. AIDev labels task type with a GPT classifier (Conventional Commits categories), not humans. [MSR paper](https://www.alphaxiv.org/abs/2605.22534), [Why Agentic-PRs Get Rejected](https://arxiv.org/html/2602.04226v1), [AIDev](https://www.alphaxiv.org/abs/2602.09185.md)

### A2. Practitioner rules of thumb (as of 2026)

| Task | Common practitioner pick | Notes |
|---|---|---|
| UI / frontend | Claude Code | Attributed to richer MCP/skill ecosystem (Figma, browser screenshot loops). [bito](https://bito.ai/ai-tools/claude-code-vs-codex/), [duply Reddit summary](https://duply.ai/blog/claude-code-vs-codex-reddit) |
| Large refactors, renames across files | Claude Code (in the loop); Codex for long unattended migrations | [firecrawl](https://www.firecrawl.dev/blog/claude-code-vs-codex), [emergent](https://emergent.sh/learn/claude-code-vs-codex) |
| Debugging, precise instruction following | Codex more often; Claude praised for working from symptoms/logs | [techbloat Reddit summary](https://www.techbloat.com/claude-code-vs-codex-2026-what-500-reddit-developers-really-think.html) |
| Code review | Codex as "strong but pedantic reviewer" of Claude's diffs; or fresh-context subagent | [HN 46365105](https://news.ycombinator.com/item?id=46365105), Anthropic writer/reviewer pattern (below) |
| Planning / architecture | Opus (or strongest model) plans, Sonnet executes, Haiku reads/searches | `opusplan` alias; "if Haiku misses twice, go to Sonnet; if Sonnet misses, Opus". [buildwithai](https://buildwithainewsletter.beehiiv.com/p/stop-paying-opus-to-do-intern-work), [ayautomate](https://www.ayautomate.com/blog/opus-vs-sonnet-vs-haiku), [HN 44746621](https://news.ycombinator.com/item?id=44746621) |
| Web research, long context | Gemini CLI | [HN 46060508](https://news.ycombinator.com/item?id=46060508) |
| Fallback when stuck | Switch agent (Codex when Claude loops, and vice versa) | [HN 46060508](https://news.ycombinator.com/item?id=46060508), [HN 49393051](https://news.ycombinator.com/item?id=49393051) |
| Long hard autonomous runs | Codex | [HN 45648422](https://news.ycombinator.com/item?id=45648422) |

The dominant pattern is a hybrid: "Claude for architecture, scaffolding and UI, Codex for review, debugging and fine-detail omissions." Evidence is anecdotal, conflicting, and decays every model release.

### A3. Routers that exist today

| Tool | What decides the route | Learns from outcomes? |
|---|---|---|
| [claude-code-router](https://www.claudelog.com/addons/claude-code-router) (`ccr`) | Static request type: `default`, `background`, `think`, `longContext` (token threshold, e.g. 60k), `webSearch` | No |
| [Cursor Router](https://cursor.com/help/models-and-usage/cursor-router) (was Auto) | ML classifier per request on task type and complexity; user picks Cost / Balance / Intelligence; Teams/Enterprise | Not documented; no per-user learning mentioned; model used is opaque ([forum](https://forum.cursor.com/t/im-trying-to-figure-out-what-model-was-used-when-i-select-auto/158834)) |
| [OpenRouter auto](https://openrouter.helicone.ai/docs/model-routing) (Not Diamond) | Prompt complexity, task type, model capability; session pinning | Not per-user |
| [LangChain Open SWE harness router](https://www.langchain.com/blog/how-to-build-a-model-router-in-the-harness) | Classifier on first human message to pick Fast / Balanced / Performance tier for the whole thread | Validated by live A/B on outcomes, but not adaptive |
| Arize advisor pattern | Cheap model runs main loop, strong model as advisor; $100 to $15-20 per run | Hand-tuned. [Arize](https://arize.com/blog/how-i-cut-coding-agent-costs-with-model-harness-routing/) |

LangChain result is the most rigorous public outcome data: 973 threads, merged-PR rate 29.2% routed vs 27.3% always-strongest (p=0.49), median cost $2.61 to $0.94 (-64%). Always-cheapest arm was killed within a day because engineers complained. Lessons: routing belongs in the harness (it has task context), measure before routing, subagents and mid-thread re-routing are unsolved, "user sentiment" is a planned signal.

### A4. Does anyone route from the user's OWN outcome history?

No. Closest items:
- [marvin issue #621](https://github.com/G-Eskayo/marvin/issues/621): load per-run costs to compute cost per merged PR. Measurement intent, not a router.
- [Arize](https://arize.com/blog/how-i-cut-coding-agent-costs-with-model-harness-routing/) and [agentastic](https://www.agentastic.dev/blog/ai-coding-cost-per-accepted-change) define cost per accepted change = (attempts + retries + review + human validation) / tasks meeting acceptance criteria.
- [Folkman](https://tylerfolkman.substack.com/p/i-tested-6-ai-models-across-3-providers) lists "cost per successful task" and eval-based promotion as a planned addition to a personal router.
- [TDS: hidden cost of routing](https://towardsdatascience.com/the-hidden-cost-of-llm-routing-why-switching-models-costs-more/): switching mid-session loses prompt cache; route per task, not per turn.

---

## B. A setup that improves itself

### B1. What people do manually

- Boris Cherny / Claude Code team: shared, checked-in CLAUDE.md; every mistake becomes a line ("Update your CLAUDE.md so you don't make that mistake again"); tag @claude on PRs to add learnings via the GitHub action; keep it around 2.5k tokens. [karozieminski](https://karozieminski.substack.com/p/boris-cherny-claude-code-workflow), [jitendrazaa](https://www.jitendrazaa.com/blog/others/tips/10-claude-code-tips-from-the-creator-boris-cherny-february/)
- Periodic self-mining: ask Claude to read 30 days of your own messages, find recurring corrections never written down. [barazany](https://barazany.dev/blog/mining-claude-history-ultracode), [raymondpeck](https://medium.com/@raymondpeck/unlocking-your-claude-history-part-3-let-claude-analyze-your-claude-conversations-a-users-guide-0797fed94c34)
- `/insights` (Claude Code built-in, 30 days of local sessions to `~/.claude/usage-data/report.html`): project areas, interaction style, friction by root cause (misunderstood request, wrong approach, buggy code, rejected actions, excessive changes, tool failures), suggested CLAUDE.md additions. Users' "did it help" check is "wait a week and rerun". [chatwithgpt](https://chatwithgpt.substack.com/p/the-self-improving-loop-how-claude), [vincentqiao](https://blog.vincentqiao.com/en/posts/claude-code-insights/)
- `/fewer-permission-prompts`-style allowlist mining from transcripts (bundled skill in current Claude Code).

### B2. Anthropic's own guidance (official docs)

From [best practices](https://code.claude.com/docs/en/best-practices) and [memory](https://code.claude.com/docs/en/memory):
- For each CLAUDE.md line ask "Would removing this cause Claude to make mistakes?" If not, cut. Bloat causes rules to be ignored. Target under 200 lines; imports do not reduce context cost; use path-scoped `.claude/rules/` and on-demand skills for situational content.
- "Treat CLAUDE.md like code: review it when things go wrong, prune it regularly, and test changes by observing whether Claude's behavior actually shifts." `/doctor` proposes cuts for content derivable from code.
- If Claude does something despite a rule: file probably too long. If Claude asks questions answered in CLAUDE.md: phrasing ambiguous. If Claude already does it right without the rule: delete it or convert to a hook.
- Hooks are deterministic, CLAUDE.md is advisory. Use hooks for "must happen every time"; PreToolUse to block.
- Auto memory (on by default): Claude writes notes typed `user`, `feedback` (corrections and confirmed approaches), `project`, `reference` to `~/.claude/projects/<project>/memory/`, first 200 lines / 25KB of MEMORY.md loaded each session. Toggle via `/memory`.
- Course correction: Esc, Esc+Esc / `/rewind`, "undo that". "If you've corrected Claude more than twice on the same issue in one session... `/clear` and start fresh." This is itself a usable threshold.
- Verification: give a pass/fail check; `/goal`, Stop hook gate, adversarial review subagent.
- Evals: [Demystifying evals for AI agents](https://anthropic.com/engineering/demystifying-evals-for-ai-agents) separates transcript (full record) from outcome (final environment state); grade outcomes, read transcripts to debug graders. skill-creator now has Eval / Improve / Benchmark modes with A/B comparison for skills. [guvi summary](https://www.guvi.in/blog/ai-agent-skill-guide/)

### B3. Evidence that rules are not free

- [Evaluating AGENTS.md (ETH / LogicStar, ICLR 2026)](https://arxiv.org/abs/2602.11988v1): context files tend to reduce task success vs no context and raise inference cost >20%. LLM-generated files: marginal negative. Developer-written: marginal positive. Neither significant. Agents do follow the instructions (broader exploration, more testing), so unnecessary requirements make tasks harder. Recommendation: minimal requirements only.
- [When "Do Not" Is Not Deny](https://arxiv.org/pdf/2608.23550): of 481 public CLAUDE.md files, only ~4-16% of security rules matched an enforcing built-in control.
- Single-operator on/off tests: [CLAUDE.md Under Test](https://leanpub.com/claude-md-under-test) (ban rule: 0/22 attempts with rule, 17/17 without); [Mnilax](https://x.com/Mnilax/status/2053116311132155938) (mistake rate 41% none, 11% 4 rules, 3% 12 rules, self-reported). [Future AGI](https://futureagi.com/blog/claude-md-instruction-governance/) argues for per-rule adherence scoring on real sessions and deleting rules that score the same with or without.

### B4. Tools that mine sessions and propose changes

| Tool | Mines | Writes | Measures effect / retires? |
|---|---|---|---|
| [Blume](https://dev.to/pederaa/i-built-blume-it-turns-your-coding-agent-corrections-into-rules-3oa5) (Claude Code, Codex, Cursor; local) | Correction / steering / frustration signals, clustered | Rules and skills, previewed diffs, undo | No. Trend view "still in progress"; retirement asked in comments, unanswered |
| [agent-knowledge](https://github.com/inhahe/agent-knowledge) | Operator corrections, tool traps, self-fixed mistakes (local LLM) | Nightly headless Opus rewrites an owned CLAUDE.md section, size-budgeted (15 rules / 4k chars global) | Retires by displacement plus recurrence evidence, not measured effect. Closest to a lifecycle |
| [claude-doctor](https://github.com/millionco/claude-doctor) | Structural + behavioral + lexical signals (definitions in C) | `--rules` paste-ready CLAUDE.md/AGENTS.md rules; `--save` baselines | Stores baselines, no before/after claim |
| `/insights` (Anthropic) | 30 days of sessions | Suggested CLAUDE.md additions | No |
| [Insight Miner skill](https://mcpmarket.com/tools/skills/insight-miner), [analyze-session-transcripts skill](https://www.skillsdirectory.com/skills/kylemit-analyze-session-transcripts-splotch) | Repeated manual commands, permission prompts, wrong first moves | Proposals at review time | No |
| `fewer-permission-prompts` (bundled) | Read-only Bash/MCP calls | Project allowlist | No |

**Nobody measures whether a change helped and retires the ones that did not.** agent-knowledge comes closest with recurrence-based retirement; claude-doctor has the baseline storage that would enable it.

---

## C. Steering and frustration signals

### C1. Research grounding

[How Coding Agents Fail Their Users (arXiv 2605.29442)](https://arxiv.org/html/2605.29442): 20,574 real sessions, 1,639 repos, IDE + CLI. LLM extractor records misalignment only when made visible by developer pushback, second LLM pass validates (precision 0.93). Forms: constraint violation 38.3%, misread intent 27.0%, inaccurate self-reporting 22.6%, faulty implementation 17.8%, wrong project diagnosis 11.6%, self-initiated overreach 10.2%, operational execution error 2.9%. 90.5% cost only effort/trust. Of visibly resolved episodes, 91.49% needed explicit user correction (3% self-corrected). Persistence: P(misalignment next session | misalignment now) = 0.519 vs 0.336 otherwise. Constraint violations and inaccurate self-reporting are growing as a share. Implication: corrections recur across sessions, so a rule that sticks has real value; and "agent said it's done" is unreliable as a success signal.

### C2. How existing tools define it

- Blume: correction = "a direct reversal of something the agent did" ("use X instead of Y"); steering/nudge = softer repeated guidance that is not a correction ("remember to run the tests"); frustration = short, repeated, escalating messages, high token spend on simple tasks, "why did you do that again". Suggest after 5 occurrences in a cluster, or 2 classified as pain (frustration or unusual token use). Improve tab shows correction, steering and frustration gauges; computation undisclosed.
- claude-doctor: correction-heavy if >=20% of user messages start with no/wrong/wait; repeated-instructions = rephrase within 5 turns with Jaccard >0.6; rapid-corrections = reply <10s after agent output; high-turn-ratio >=1.5 user msgs per agent response; negative-drift (messages get shorter and more corrective); keep-going-loop; edit-thrashing (same file edited 5+ times); error-loop (3+ consecutive tool failures without approach change); excessive-exploration (read:edit >10:1); restart-cluster (multiple sessions within 30 min); abandonment (<3 user messages); AFINN (165 version) sentiment plus tokens undo/revert/wrong/broken.
- Claude Code itself (leaked `userPromptKeywords.ts`, March 2026): regex over each prompt for wtf, wth, ffs, omfg, "this sucks", "screw this", horrible, awful, "so frustrating", insults; logs negativity to telemetry. [PCWorld](https://www.pcworld.com/article/3104748/claude-code-is-scanning-your-messages-for-curse-words.html), [Scientific American](https://scientificamerican.com/article/anthropic-leak-reveals-claude-code-tracking-user-frustration-and-raises-new), [Slashdot](https://tech.slashdot.org/story/26/04/05/2339243/)
- Anthropic docs: >2 corrections on the same issue in a session = context polluted, restart.

### C3. Concrete signal definitions computable from local transcripts

Markers verified against this machine's own logs (80 recent Claude Code sessions; ~450 Codex turns).

**Claude Code (`~/.claude/projects/<proj>/*.jsonl`)**

| Signal | Definition |
|---|---|
| Interrupt | user message text starts with `[Request interrupted by user]` or `[Request interrupted by user for tool use]` (seen 10 and 8 times) |
| Permission rejection | tool_result text starts `The user doesn't want to proceed with this tool use. The tool use was rejected` (seen 6) |
| Rejection with reason | same, followed by user text (the "why", best single source of rule candidates) |
| Tool failure | tool_result `is_error: true` (seen 155) |
| Compaction | `isCompactSummary: true` (context churn; long-session proxy) |
| Subagent work | `isSidechain: true` or subagent transcript files |
| Mode | `permissionMode` field per message |
| Cost | `message.usage` on assistant entries x model price |

**Codex (`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`)**

| Signal | Definition |
|---|---|
| Interrupt | `event_msg` type `turn_aborted`, `reason: "interrupted"` |
| Turn boundaries | `task_started` / `task_complete` |
| Command failure | `exec_command_end` with `exit_code != 0` (local: 1389 zero, 85 one, 17 two, ...) |
| Edit applied | `patch_apply_end` |
| Compaction | `context_compacted` |
| Approval / safety | `guardian_assessment` events |
| Cost | `token_count` events |

**Cross-agent derived signals**

1. Correction: user message within N turns after an agent edit/action whose first clause matches `^(no|nope|wrong|wait|stop|don't|do not|not that|undo|revert|actually|instead)\b` or "use X instead of Y", "I said", "I told you", "again". Rate = corrections / user messages.
2. Steering (nudge): imperative user message mid-task that adds a constraint without negation ("also run the tests", "remember to..."). Recurring if the same normalized instruction appears in >=2 sessions in the same repo (Jaccard >0.6 on content words or embedding cosine).
3. Repeated instruction: rephrase within 5 turns, Jaccard >0.6.
4. Frustration: leaked-regex profanity list OR (message length < 25% of user's median AND reply latency <10s AND follows a correction) OR all-caps / repeated punctuation; escalation = 3 such in a row.
5. Revert: Bash matching `git (checkout --|restore|revert|reset --hard|stash)`, `/rewind`, "undo that", or an Edit whose new_string equals an earlier old_string on the same file. Edit-thrash: same file edited >=5 times.
6. Error loop: >=3 consecutive tool failures with no change in command/approach.
7. Re-prompt / restart: new session in same cwd within 30 min whose first message has high similarity to the previous session's first message (abandon and retry).
8. Success proxies (strongest first): test/build command exit 0 after the last edit and no subsequent correction; `git commit` / `gh pr create` in session; PR later merged (external join via `gh pr view --json mergedAt`; caveat: only 35.7% of rejections are agent fault, and reviewer approval is not correctness); session ends without interrupt/correction in final 3 user turns; no revert of the session's files within 24h in git history.
9. Task type: classify the first user message (LangChain approach) and cross-check with touched-file profile and commands: test files and test runner = test fixing; `.tsx/.css/.html` plus screenshot/browser tools = UI; `.tf/.yaml/Dockerfile/.github/workflows` = infra/config; `.md` only = docs; many renames/moves across files with small per-file diffs = refactor; stack trace or log paste in first message = debugging; new directories/files dominate = greenfield; `gh pr diff`/review language with no edits = review.
10. Cost per success: sum tokens x price over all attempts (including restarted sessions linked by signal 7) / sessions passing signal 8.

---

## The gap no tool fills

Two loops are open:

1. **Routing from your own outcomes.** Every router classifies the request (static rules, ML classifier, LLM tier picker). None derives per-user routing from that user's history of outcomes (task type x agent/model -> success proxy, corrections, cost per success). The only outcome-validated routing (LangChain) is an org-level A/B, not adaptive, and not cross-vendor (Claude Code vs Codex vs Cursor). Insight APAC shows harness and settings matter as much as model, and that results only hold on your own repeated work.
2. **Measured setup changes.** Blume, agent-knowledge, claude-doctor and `/insights` all propose rules from transcripts. None measures whether a rule reduced the signal it was created for (e.g. corrections in its cluster before vs after, adherence when the triggering context recurs), and none retires rules on measured null effect. Anthropic's docs tell you to do exactly this by eye. The AGENTS.md paper shows unmeasured rules can cost success and >20% tokens, so retiring dead rules has real value.

A tool that owns both loops would: tag each session with task type, agent, model, harness settings; compute correction/steering/frustration/revert/success signals; recommend agent+model per task type from the user's own cost per success (with minimum sample sizes and replication, since single runs swing widely); and attach every CLAUDE.md/AGENTS.md/hook/allowlist change to the signal cluster that motivated it, then report the before/after rate on that cluster and propose removal when it shows no effect.
