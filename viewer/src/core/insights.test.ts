import { describe, expect, it } from 'vitest';
import {
  compactionSummary,
  failuresBySource,
  fileHeat,
  filterSessions,
  headline,
  insightsStatsSchema,
  outcomesByWeek,
  prRate,
  spendByDay,
  topFailingCommands,
  wasteSummary,
  weekStart,
  type InsightsSession,
  type SessionFacts,
} from './insights';
import { squarify } from './treemap';

const facts = (over: Partial<SessionFacts> = {}): SessionFacts => ({
  outcome: 'completed',
  edited: false,
  prsCreated: 0,
  prsMerged: 0,
  costUsd: 1,
  hasCost: true,
  costByModel: { 'claude-sonnet-4-5': 1 },
  calls: 10,
  errors: 0,
  retries: 0,
  cmd: {},
  errorsBySource: {},
  files: {},
  compactions: 0,
  preTokens: [],
  rereadsAfterCompaction: 0,
  verifyAfterLastEdit: false,
  waste: { retry: 0, abandoned: 0, cacheMiss: 0, reread: 0, deadSubagent: 0 },
  stallMs: 0,
  firstPromptChars: 0,
  userTurns: 1,
  ...over,
});

const row = (id: string, over: Partial<SessionFacts> = {}, rest: Partial<InsightsSession> = {}): InsightsSession => ({
  sessionId: id,
  sessionDate: '2026-10-14',
  startedAt: '2026-10-14T10:00:00Z',
  agentTool: 'claude-code',
  models: ['claude-sonnet-4-5'],
  repos: ['acme/api'],
  facts: facts(over),
  ...rest,
});

describe('PR rate', () => {
  it('counts only sessions that edited code', () => {
    const rows = [
      row('a', { edited: true, prsCreated: 1 }),
      row('b', { edited: true }),
      row('c'), // answered a question: neither helps nor hurts
    ];
    expect(prRate(rows)).toEqual({ edited: 2, opened: 1, rate: 0.5 });
    expect(prRate([row('x')]).rate).toBeNull();
  });

  it('keeps sessions with no cost out of spend and cost per PR', () => {
    const h = headline([
      row('a', { edited: true, prsCreated: 2, prsMerged: 1, costUsd: 4 }),
      row('b', { hasCost: false, costUsd: 0, edited: true, prsCreated: 1 }, { agentTool: 'cursor' }),
    ]);
    expect(h.spend).toBe(4);
    expect(h.noCost).toBe(1);
    expect(h.costPerPr).toBe(2); // $4 over the 2 PRs from priced sessions
    expect(h.prsOpened).toBe(3);
    expect(h.prsMerged).toBe(1);
  });
});

describe('filters and weeks', () => {
  it('filters by agent, repo and model', () => {
    const rows = [row('a'), row('b', {}, { agentTool: 'codex', repos: ['acme/web'] })];
    expect(filterSessions(rows, { agent: 'codex' }).map((r) => r.sessionId)).toEqual(['b']);
    expect(filterSessions(rows, { repo: 'acme/api' }).map((r) => r.sessionId)).toEqual(['a']);
    expect(filterSessions(rows, { model: 'gpt-5' })).toEqual([]);
  });

  it('buckets weeks from Monday and counts outcomes', () => {
    expect(weekStart('2026-10-14')).toBe('2026-10-12');
    expect(weekStart('2026-10-12')).toBe('2026-10-12');
    expect(weekStart('2026-10-18')).toBe('2026-10-12');
    const weeks = outcomesByWeek([
      row('a', { outcome: 'pr_created', edited: true, prsCreated: 1 }),
      row('b', { outcome: 'weird' }),
      row('c', {}, { sessionDate: '2026-10-05' }),
    ]);
    expect(weeks.map((w) => w.week)).toEqual(['2026-10-05', '2026-10-12']);
    expect(weeks[1].counts.pr_created).toBe(1);
    expect(weeks[1].counts.completed).toBe(1); // unknown outcomes fold into completed
    expect(weeks[1].pr.rate).toBe(1);
  });
});

describe('spend', () => {
  it('splits by agent or model and folds the tail into Other', () => {
    const rows = [
      row('a', { costUsd: 3, costByModel: { m1: 2, m2: 1 } }),
      row('b', { costUsd: 1, costByModel: { m3: 1 } }, { agentTool: 'codex', sessionDate: '2026-10-15' }),
    ];
    const byAgent = spendByDay(rows, 'agent');
    expect(byAgent.series).toEqual([
      { key: 'claude-code', total: 3 },
      { key: 'codex', total: 1 },
    ]);
    const byModel = spendByDay(rows, 'model', 1);
    expect(byModel.series.map((s) => s.key)).toEqual(['m1', 'Other']);
    expect(byModel.days[0].parts).toEqual({ m1: 2, Other: 1 });
  });
});

describe('failures', () => {
  it('ranks commands by failures with their error classes', () => {
    const rows = [
      row('a', { cmd: { pytest: { calls: 4, fails: 2, retries: 1, byClass: { exit_code: 2 } } } }),
      row('b', { cmd: { pytest: { calls: 2, fails: 1, retries: 0, byClass: { timeout: 1 } }, ls: { calls: 9, fails: 0, retries: 0, byClass: {} } } }),
    ];
    const [top, ...rest] = topFailingCommands(rows);
    expect(rest).toEqual([]); // commands that never failed are left out
    expect(top).toMatchObject({ verb: 'pytest', calls: 6, fails: 3, retries: 1, sessions: 2 });
    expect(top.failRate).toBeCloseTo(0.5);
    expect(top.byClass).toEqual({ exit_code: 2, timeout: 1 });
  });

  it('groups failures by source in a fixed order', () => {
    const groups = failuresBySource([
      row('a', { errorsBySource: { llm: { rate_limit: 2 }, tool: { exit_code: 3, timeout: 1 } } }),
    ]);
    expect(groups.map((g) => g.source)).toEqual(['tool', 'llm']);
    expect(groups[0]).toEqual({ source: 'tool', total: 4, classes: [{ cls: 'exit_code', n: 3 }, { cls: 'timeout', n: 1 }] });
  });
});

describe('files, waste and compaction', () => {
  it('sums file touches across sessions', () => {
    const heat = fileHeat([
      row('a', { files: { 'acme/api:src/lib/auth.ts': { hits: 3, reads: 2, edits: 1 } } }),
      row('b', { files: { 'acme/api:src/lib/auth.ts': { hits: 1, reads: 1, edits: 0 }, 'acme/api:README.md': { hits: 0, reads: 1, edits: 0 } } }),
    ]);
    expect(heat[0]).toMatchObject({ key: 'acme/api:src/lib/auth.ts', repo: 'acme/api', dir: 'src/lib/', name: 'auth.ts', sessions: 2, hits: 4, reads: 3, edits: 1 });
    expect(heat[1]).toMatchObject({ dir: '', name: 'README.md' });
  });

  it('sums waste over priced sessions and reports its share', () => {
    const w = wasteSummary([
      row('a', { costUsd: 10, waste: { retry: 1, abandoned: 2, cacheMiss: 0, reread: 0, deadSubagent: 0 }, stallMs: 5000 }),
      row('b', { hasCost: false, costUsd: 0, waste: { retry: 9, abandoned: 0, cacheMiss: 0, reread: 0, deadSubagent: 0 } }),
    ]);
    expect(w.total).toBe(3);
    expect(w.share).toBeCloseTo(0.3);
    expect(w.stallMs).toBe(5000);
  });

  it('compares PR rate with and without compaction', () => {
    const c = compactionSummary([
      row('a', { compactions: 2, preTokens: [100, 300], edited: true }),
      row('b', { edited: true, prsCreated: 1 }),
    ]);
    expect(c.perSession).toBe(1);
    expect(c.share).toBe(0.5);
    expect(c.medianPreTokens).toBe(200);
    expect(c.withCompaction.rate).toBe(0);
    expect(c.without.rate).toBe(1);
  });
});

describe('stats schema', () => {
  it('drops rows it cannot read instead of failing the page', () => {
    const parsed = insightsStatsSchema.parse({ sessions: [{ sessionId: 'a', facts: {} }, { nope: 1 }] });
    expect(parsed.sessions.map((s) => s.sessionId)).toEqual(['a']);
    expect(parsed.sessions[0].facts.waste.retry).toBe(0);
  });
});

describe('squarify', () => {
  it('fills the box with areas proportional to value', () => {
    const placed = squarify([6, 6, 4, 3, 2, 2, 1], (v) => v, { x: 0, y: 0, w: 6, h: 4 });
    expect(placed).toHaveLength(7);
    const area = placed.reduce((s, p) => s + p.w * p.h, 0);
    expect(area).toBeCloseTo(24);
    for (const p of placed) expect(p.w * p.h).toBeCloseTo(p.item);
  });

  it('skips empty items and an empty box', () => {
    expect(squarify([0, 0], (v) => v, { x: 0, y: 0, w: 1, h: 1 })).toEqual([]);
    expect(squarify([1], (v) => v, { x: 0, y: 0, w: 0, h: 1 })).toEqual([]);
  });
});
