/**
 * Plain-language definitions behind every Insights info button. One list, so
 * a term reads the same wherever it appears.
 */
import { OUTCOME_META, type SessionOutcome } from '@/core/sessionMapModel';
import { MIN_SAMPLE } from '@/core/insights';

export interface Term {
  title: string;
  paras: string[];
  rows?: [string, string][];
  formula?: string;
}

const OUTCOME_HELP: Record<SessionOutcome, string> = {
  pr_created: 'The agent opened a pull request. Wins over everything else, even errors afterwards.',
  verified: 'Tests, a build or a linter passed, and nothing was edited after that.',
  completed: 'No errors at the end and the agent gave a final reply, but no PR and no passing check.',
  interrupted: 'You stopped the agent near the end. Often the right call, so it is not treated as a failure.',
  blocked: 'A command or tool was refused near the end: blocked by a hook or setting, or permission denied.',
  unresolved_errors: 'Two or more of the last 12 actions failed and the session ended there.',
  incomplete: 'Stopped mid-work: the last step was an edit or command and the agent never replied.',
};

export const GLOSSARY = {
  sessions: {
    title: 'Sessions',
    paras: [
      'A session is one conversation with a coding agent, from your first prompt to the end.',
      'Only root sessions are counted: the ones you started. Subagents and helper runs an agent launches belong to their parent session.',
    ],
  },
  spend: {
    title: 'Spend',
    paras: [
      'What the model requests in these sessions would cost at list API prices, worked out from their token counts.',
      'It is an estimate, not your bill. Cursor records no tokens or cost, so its sessions are left out of every spend figure.',
    ],
  },
  costPerPr: {
    title: 'Cost per PR',
    paras: [
      'Total spend divided by the number of PRs opened.',
      'Spend on sessions that opened no PR is charged to the ones that did, so this falls when fewer sessions are wasted, not only when sessions get cheaper.',
    ],
    formula: 'spend ÷ PRs opened',
  },
  prRate: {
    title: 'PR rate',
    paras: [
      'The share of sessions that edited code and opened a pull request. Sessions that only answered a question or explored are left out, so they neither raise nor lower it.',
      'Merged is how many of those PRs agentplay later saw merged in an agent session. Merges done outside an agent are not visible yet.',
      `Rates over fewer than ${MIN_SAMPLE} sessions are shown greyed.`,
    ],
    formula: 'sessions that opened a PR ÷ sessions with at least one edit',
  },
  outcomes: {
    title: 'How sessions end',
    paras: [
      'Every session gets exactly one outcome, read from how it ended: its last actions, any PR, and whether checks passed. agentplay never reads what was said. The first match in this order wins.',
    ],
    rows: (Object.keys(OUTCOME_HELP) as SessionOutcome[]).map((o) => [OUTCOME_META[o].label, OUTCOME_HELP[o]]),
    formula: 'Number above each bar: that week’s PR rate (sessions that opened a PR ÷ sessions that edited code).',
  },
  failingCommands: {
    title: 'Failing commands',
    paras: [
      'Fail rate is failed runs of a command divided by all its runs. Retries are runs that repeat one that just failed. Only the command name is stored (pnpm test), never its arguments.',
      'Each bar is split by why the command failed:',
    ],
    rows: [
      ['exit_code', 'It ran and exited with an error, e.g. failing tests or a compile error.'],
      ['timeout', 'It ran too long and was stopped.'],
      ['permission_denied', 'The agent was not allowed to run it.'],
      ['not_found', 'The command, or a file it needed, does not exist.'],
    ],
  },
  whatFails: {
    title: 'What fails',
    paras: ['Every failed action, grouped by where it failed. The names under each group are error classes.'],
    rows: [
      ['Tool', 'Shell commands and file tools: running tests, reading or editing files.'],
      ['MCP', 'Calls to MCP servers, the external tools an agent can use, like Linear or GitHub.'],
      ['LLM', 'The model request itself: rate limits, overloaded servers, or a prompt too long for the context.'],
      ['Hook', 'Your own hook scripts that run before or after tool calls.'],
      ['Orchestration', 'Subagents that failed or were killed.'],
    ],
  },
  searchHeatmap: {
    title: 'Search heatmap',
    paras: [
      'Where agents spend effort finding code. Each tile is a file, grouped by folder. Size is how often it came up in search results; colour is how many sessions touched it, or how often it is searched compared with edited.',
      'A few heavily searched files are normal. The useful signal is a folder agents search through in many sessions before reading anything.',
    ],
    rows: [
      ['Search hits', 'Times the file came up in a search result (grep, find, glob).'],
      ['Reads', 'Times it was opened.'],
      ['Edits', 'Times it was changed.'],
    ],
  },
  waste: {
    title: 'Waste',
    paras: ['Spend on work that did not lead to a PR or a passing check, split into five buckets. Each dollar is counted once, in this order:'],
    rows: [
      ['Retry loops', 'Failed tool or MCP calls and their retries.'],
      ['Abandoned spend', 'The rest of the cost of sessions that ended with unresolved errors or stopped mid-work.'],
      ['Cache misses', 'Paying full price for input that could have come from the prompt cache at about a tenth of the cost.'],
      ['Re-reads', 'Reading a file again when nothing had changed it.'],
      ['Dead subagents', 'Subagents that ended with no result, failed or were killed.'],
      ['Stalls', 'Long pauses with no progress. Time lost, not money, so shown separately.'],
    ],
    formula: 'Cost is only known per model request, so a failed command is charged the request that read its result. Every bucket is an estimate.',
  },
  compaction: {
    title: 'Compaction',
    paras: [
      'When a conversation gets close to the model’s context limit, the agent summarises earlier turns and drops the detail to make room. That is a compaction.',
      'Detail is lost, so agents often re-read files afterwards, and sessions that compact can open PRs less often.',
    ],
    rows: [
      ['Tokens at compaction', 'How full the context was when it happened.'],
      ['Re-reads after compaction', 'Files read again after a compaction with no change to them in between.'],
    ],
  },
  runHealth: {
    title: 'Run health',
    paras: [
      'A 0–100 score for how well your agents are set up and run, not how hard the work was. It adds five parts worth 20 points each.',
      `It compares you only with your own previous window of the same length. No total is shown until the window has ${MIN_SAMPLE} sessions.`,
    ],
  },
  components: {
    title: 'Run health components',
    paras: ['Each part scores 0–20 from a formula over your sessions in the window. Select one to see its inputs and the sessions that cost the most points.'],
    rows: [
      ['Shipping', 'Sessions that opened a PR ÷ sessions that edited code.'],
      ['Verification', 'Sessions where tests, a build or a linter passed after the last edit ÷ sessions that edited code.'],
      ['Efficiency', 'Half how few calls failed or were retried, half the share of input read from the prompt cache.'],
      ['Context hygiene', 'How rarely sessions reach the context limit, and how few files are re-read after they do.'],
      ['Environment health', 'How rarely calls fail on configuration: permissions, blocked or missing commands, hooks and MCP servers.'],
    ],
  },
  pointsOnTable: {
    title: 'Points on the table',
    paras: [
      'Patterns in your sessions that cost Run health points, each with a fix. A fix only appears when it can name the command, tool or server behind it.',
      'Points are what the score would gain if the pattern stopped: the component recomputed without the failures, or with the rate raised to a target. They are estimates; Autotune checks them against what happens next.',
    ],
  },
  autotune: {
    title: 'Autotune',
    paras: [
      'Once a day agentplay reruns the Run health rules on your last 30 days, writes the fixes it is allowed to, and removes earlier ones that stopped helping.',
      'Anything that grants an agent access or runs code (permissions, hooks) waits here for your OK.',
    ],
  },
  scopeFiles: {
    title: 'The files it can change',
    paras: ['Agents read these files at the start of each session:'],
    rows: [
      ['CLAUDE.local.md', 'Personal instructions, git-ignored. Autotune writes here by default.'],
      ['CLAUDE.md', 'Shared instructions, committed with the repo. Only when you allow it for that repo.'],
      ['.claude/settings.local.json', 'Commands the agent may run without asking. Always asks you first.'],
      ['.claude/settings.json hooks', 'Scripts that run automatically, e.g. tests before the agent stops. Always asks you first.'],
    ],
  },
  managedBlock: {
    title: 'The managed block',
    paras: ['agentplay only writes between <!-- agentplay:start --> and <!-- agentplay:end --> markers. Anything outside them is yours and never touched. Each line carries a marker, so Undo removes exactly what was added.'],
  },
} satisfies Record<string, Term>;

export type TermKey = keyof typeof GLOSSARY;
