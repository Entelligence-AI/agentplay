/**
 * Insights: cross-session aggregates over per-session facts.
 *
 * Facts are computed once per session by the CLI (`agentplay/facts.py`) and
 * served by `GET /api/stats` (or read off the demo index). Everything here is
 * a pure function of those rows, so the dashboard and its tests share one
 * definition of every number. No React, no fetchers.
 */
import { z } from 'zod';
import type { SessionOutcome } from './sessionMapModel';

const count = z.number().catch(0);

const cmdSchema = z.object({
  calls: count,
  fails: count,
  retries: count,
  byClass: z.record(z.string(), z.number()).catch({}),
});

const fileSchema = z.object({ hits: count, reads: count, edits: count });

export const sessionFactsSchema = z.object({
  outcome: z.string().catch('completed'),
  edited: z.boolean().catch(false),
  prsCreated: count,
  prsMerged: count,
  costUsd: count,
  hasCost: z.boolean().catch(false),
  costByModel: z.record(z.string(), z.number()).catch({}),
  calls: count,
  errors: count,
  retries: count,
  cmd: z.record(z.string(), cmdSchema).catch({}),
  errorsBySource: z.record(z.string(), z.record(z.string(), z.number())).catch({}),
  files: z.record(z.string(), fileSchema).catch({}),
  compactions: count,
  preTokens: z.array(z.number()).catch([]),
  rereadsAfterCompaction: count,
  verifyAfterLastEdit: z.boolean().catch(false),
  waste: z
    .object({
      retry: count,
      abandoned: count,
      cacheMiss: count,
      reread: count,
      deadSubagent: count,
    })
    .catch({ retry: 0, abandoned: 0, cacheMiss: 0, reread: 0, deadSubagent: 0 }),
  stallMs: count,
  firstPromptChars: count,
  userTurns: count,
});
export type SessionFacts = z.infer<typeof sessionFactsSchema>;

const isoDay = z
  .string()
  .regex(/^\d{4}-\d{2}-\d{2}/)
  .refine((v) => !Number.isNaN(Date.parse(`${v.slice(0, 10)}T00:00:00Z`)))
  .nullable()
  .catch(null);

export const insightsSessionSchema = z.object({
  sessionId: z.string(),
  // An imported map can carry any string here; a date that doesn't parse is treated as missing.
  sessionDate: isoDay,
  startedAt: isoDay,
  agentTool: z.string().catch('unknown'),
  models: z.array(z.string()).catch([]),
  repos: z.array(z.string()).catch([]),
  facts: sessionFactsSchema,
});
export type InsightsSession = z.infer<typeof insightsSessionSchema>;

export const insightsStatsSchema = z.object({
  sessions: z
    .array(z.unknown())
    .transform((rows) =>
      rows.flatMap((r) => {
        const parsed = insightsSessionSchema.safeParse(r);
        return parsed.success ? [parsed.data] : [];
      })
    ),
});
export type InsightsStats = z.infer<typeof insightsStatsSchema>;

/** Rates over fewer sessions than this are shown greyed, with their counts. */
export const MIN_SAMPLE = 10;

export interface InsightsFilter {
  agent?: string | null;
  repo?: string | null;
  model?: string | null;
}

export function filterSessions(rows: InsightsSession[], f: InsightsFilter): InsightsSession[] {
  return rows.filter(
    (r) =>
      (!f.agent || r.agentTool === f.agent) &&
      (!f.repo || r.repos.includes(f.repo)) &&
      (!f.model || r.models.includes(f.model))
  );
}

/** The values each filter can take, most common first. */
export function filterOptions(rows: InsightsSession[]): {
  agents: string[];
  repos: string[];
  models: string[];
} {
  const rank = (values: string[]) => {
    const n = new Map<string, number>();
    for (const v of values) n.set(v, (n.get(v) ?? 0) + 1);
    return [...n.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([k]) => k);
  };
  return {
    agents: rank(rows.map((r) => r.agentTool)),
    repos: rank(rows.flatMap((r) => r.repos)),
    models: rank(rows.flatMap((r) => r.models)),
  };
}

// ------------------------------------------------------------------ headline

export interface PrRate {
  /** sessions with at least one edit */
  edited: number;
  /** of those, sessions that opened a PR */
  opened: number;
  /** opened / edited, or null with no edited sessions */
  rate: number | null;
}

/** PR rate: sessions that opened a PR over sessions that edited code. A
 * session that only answered a question or explored neither raises nor
 * lowers it. */
export function prRate(rows: InsightsSession[]): PrRate {
  const edited = rows.filter((r) => r.facts.edited);
  const opened = edited.filter((r) => r.facts.prsCreated > 0).length;
  return { edited: edited.length, opened, rate: edited.length ? opened / edited.length : null };
}

export interface Headline {
  sessions: number;
  /** USD over sessions that report cost */
  spend: number;
  /** sessions with no cost data (Cursor), left out of every spend figure */
  noCost: number;
  pr: PrRate;
  prsOpened: number;
  prsMerged: number;
  costPerPr: number | null;
}

export function headline(rows: InsightsSession[]): Headline {
  const priced = rows.filter((r) => r.facts.hasCost);
  const spend = priced.reduce((s, r) => s + r.facts.costUsd, 0);
  const prsOpened = rows.reduce((s, r) => s + r.facts.prsCreated, 0);
  const prsMerged = rows.reduce((s, r) => s + r.facts.prsMerged, 0);
  const pricedPrs = priced.reduce((s, r) => s + r.facts.prsCreated, 0);
  return {
    sessions: rows.length,
    spend,
    noCost: rows.length - priced.length,
    pr: prRate(rows),
    prsOpened,
    prsMerged,
    costPerPr: pricedPrs ? spend / pricedPrs : null,
  };
}

// ------------------------------------------------------------------ outcomes

export const OUTCOME_ORDER: SessionOutcome[] = [
  'pr_created',
  'verified',
  'completed',
  'interrupted',
  'blocked',
  'unresolved_errors',
  'incomplete',
];

/** Monday (UTC) of the week holding `iso`, as `YYYY-MM-DD` (empty when `iso` is not a date). */
export function weekStart(iso: string): string {
  const d = new Date(`${iso.slice(0, 10)}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return '';
  const dow = (d.getUTCDay() + 6) % 7;
  d.setUTCDate(d.getUTCDate() - dow);
  return d.toISOString().slice(0, 10);
}

export interface OutcomeWeek {
  week: string;
  total: number;
  counts: Record<SessionOutcome, number>;
  pr: PrRate;
}

export function outcomesByWeek(rows: InsightsSession[]): OutcomeWeek[] {
  const byWeek = new Map<string, InsightsSession[]>();
  for (const r of rows) {
    const day = r.sessionDate ?? r.startedAt;
    if (!day) continue;
    const w = weekStart(day);
    if (!w) continue;
    byWeek.set(w, [...(byWeek.get(w) ?? []), r]);
  }
  return [...byWeek.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([week, list]) => {
      const counts = Object.fromEntries(OUTCOME_ORDER.map((o) => [o, 0])) as Record<SessionOutcome, number>;
      for (const r of list) {
        const o = (OUTCOME_ORDER as string[]).includes(r.facts.outcome)
          ? (r.facts.outcome as SessionOutcome)
          : 'completed';
        counts[o] += 1;
      }
      return { week, total: list.length, counts, pr: prRate(list) };
    });
}

// ------------------------------------------------------------------ spend

export interface SpendDay {
  day: string;
  parts: Record<string, number>;
  total: number;
}

export interface SpendSeries {
  key: string;
  total: number;
}

/** Spend per day split by agent or model. Series past `maxSeries` fold into "Other". */
export function spendByDay(
  rows: InsightsSession[],
  by: 'agent' | 'model',
  maxSeries = 5
): { days: SpendDay[]; series: SpendSeries[] } {
  const raw = new Map<string, Map<string, number>>();
  const totals = new Map<string, number>();
  for (const r of rows) {
    if (!r.facts.hasCost || !r.sessionDate) continue;
    const parts: [string, number][] =
      by === 'agent'
        ? [[r.agentTool, r.facts.costUsd]]
        : Object.entries(r.facts.costByModel);
    const day = raw.get(r.sessionDate) ?? new Map<string, number>();
    for (const [k, v] of parts) {
      day.set(k, (day.get(k) ?? 0) + v);
      totals.set(k, (totals.get(k) ?? 0) + v);
    }
    raw.set(r.sessionDate, day);
  }
  const ranked = [...totals.entries()].sort((a, b) => b[1] - a[1]);
  const keep = new Set(ranked.slice(0, maxSeries).map(([k]) => k));
  const fold = (k: string) => (keep.has(k) ? k : 'Other');
  const series = new Map<string, number>();
  for (const [k, v] of ranked) series.set(fold(k), (series.get(fold(k)) ?? 0) + v);
  const days = [...raw.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([day, m]) => {
      const parts: Record<string, number> = {};
      for (const [k, v] of m) parts[fold(k)] = (parts[fold(k)] ?? 0) + v;
      return { day, parts, total: Object.values(parts).reduce((s, v) => s + v, 0) };
    });
  return { days, series: [...series.entries()].map(([key, total]) => ({ key, total })) };
}

/**
 * Every UTC day from `first` through `last`, so a chart shows quiet days as gaps.
 * Uncapped on purpose: callers bucket every day `spendByDay` returns, so the range must cover them all.
 */
export function dayRange(first: string, last: string): string[] {
  const out: string[] = [];
  const d = new Date(`${first}T00:00:00Z`);
  const end = new Date(`${last}T00:00:00Z`);
  while (d <= end) {
    out.push(d.toISOString().slice(0, 10));
    d.setUTCDate(d.getUTCDate() + 1);
  }
  return out;
}

// ------------------------------------------------------------------ failures

export interface CommandStat {
  verb: string;
  calls: number;
  fails: number;
  retries: number;
  failRate: number;
  byClass: Record<string, number>;
  sessions: number;
}

export function topFailingCommands(rows: InsightsSession[], n = 8): CommandStat[] {
  const m = new Map<string, CommandStat>();
  for (const r of rows) {
    for (const [verb, c] of Object.entries(r.facts.cmd)) {
      const s = m.get(verb) ?? { verb, calls: 0, fails: 0, retries: 0, failRate: 0, byClass: {}, sessions: 0 };
      s.calls += c.calls;
      s.fails += c.fails;
      s.retries += c.retries;
      if (c.fails) s.sessions += 1;
      for (const [k, v] of Object.entries(c.byClass)) s.byClass[k] = (s.byClass[k] ?? 0) + v;
      m.set(verb, s);
    }
  }
  return [...m.values()]
    .filter((s) => s.fails > 0)
    .map((s) => ({ ...s, failRate: s.calls ? s.fails / s.calls : 0 }))
    .sort((a, b) => b.fails - a.fails || b.calls - a.calls)
    .slice(0, n);
}

export const ERROR_SOURCES = ['tool', 'mcp', 'llm', 'hook', 'orchestration'] as const;

export interface FailureGroup {
  source: string;
  total: number;
  classes: { cls: string; n: number }[];
}

export function failuresBySource(rows: InsightsSession[]): FailureGroup[] {
  const m = new Map<string, Map<string, number>>();
  for (const r of rows) {
    for (const [src, classes] of Object.entries(r.facts.errorsBySource)) {
      const g = m.get(src) ?? new Map<string, number>();
      for (const [cls, n] of Object.entries(classes)) g.set(cls, (g.get(cls) ?? 0) + n);
      m.set(src, g);
    }
  }
  const order = (s: string) => {
    const i = (ERROR_SOURCES as readonly string[]).indexOf(s);
    return i < 0 ? ERROR_SOURCES.length : i;
  };
  return [...m.entries()]
    .map(([source, g]) => ({
      source,
      total: [...g.values()].reduce((s, v) => s + v, 0),
      classes: [...g.entries()].map(([cls, n]) => ({ cls, n })).sort((a, b) => b.n - a.n),
    }))
    .filter((g) => g.total > 0)
    .sort((a, b) => order(a.source) - order(b.source));
}

// ------------------------------------------------------------------ files

export interface FileHeat {
  /** `repo:path` */
  key: string;
  repo: string;
  path: string;
  dir: string;
  name: string;
  sessions: number;
  hits: number;
  reads: number;
  edits: number;
}

export function fileHeat(rows: InsightsSession[]): FileHeat[] {
  const m = new Map<string, FileHeat>();
  for (const r of rows) {
    for (const [key, f] of Object.entries(r.facts.files)) {
      const cut = key.indexOf(':');
      const repo = cut >= 0 ? key.slice(0, cut) : '';
      const path = cut >= 0 ? key.slice(cut + 1) : key;
      const slash = path.lastIndexOf('/');
      const h = m.get(key) ?? {
        key,
        repo,
        path,
        dir: slash >= 0 ? path.slice(0, slash + 1) : '',
        name: slash >= 0 ? path.slice(slash + 1) : path,
        sessions: 0,
        hits: 0,
        reads: 0,
        edits: 0,
      };
      h.sessions += 1;
      h.hits += f.hits;
      h.reads += f.reads;
      h.edits += f.edits;
      m.set(key, h);
    }
  }
  return [...m.values()].sort((a, b) => b.hits - a.hits || b.sessions - a.sessions);
}

// ------------------------------------------------------------------ waste

export const WASTE_BUCKETS = ['abandoned', 'retry', 'cacheMiss', 'reread', 'deadSubagent'] as const;
export type WasteBucket = (typeof WASTE_BUCKETS)[number];

export interface WasteSummary {
  buckets: Record<WasteBucket, number>;
  total: number;
  /** share of priced spend, or null with no spend */
  share: number | null;
  stallMs: number;
}

export function wasteSummary(rows: InsightsSession[]): WasteSummary {
  const buckets = Object.fromEntries(WASTE_BUCKETS.map((b) => [b, 0])) as Record<WasteBucket, number>;
  let spend = 0;
  let stallMs = 0;
  for (const r of rows) {
    stallMs += r.facts.stallMs;
    if (!r.facts.hasCost) continue;
    spend += r.facts.costUsd;
    for (const b of WASTE_BUCKETS) buckets[b] += r.facts.waste[b];
  }
  const total = WASTE_BUCKETS.reduce((s, b) => s + buckets[b], 0);
  return { buckets, total, share: spend ? total / spend : null, stallMs };
}

// ------------------------------------------------------------------ compaction

export interface CompactionSummary {
  perSession: number;
  /** share of sessions with at least one compaction */
  share: number | null;
  medianPreTokens: number | null;
  rereads: number;
  withCompaction: PrRate;
  without: PrRate;
}

const median = (xs: number[]): number | null => {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
};

export function compactionSummary(rows: InsightsSession[]): CompactionSummary {
  const compacted = rows.filter((r) => r.facts.compactions > 0);
  return {
    perSession: rows.length ? rows.reduce((s, r) => s + r.facts.compactions, 0) / rows.length : 0,
    share: rows.length ? compacted.length / rows.length : null,
    medianPreTokens: median(rows.flatMap((r) => r.facts.preTokens)),
    rereads: rows.reduce((s, r) => s + r.facts.rereadsAfterCompaction, 0),
    withCompaction: prRate(compacted),
    without: prRate(rows.filter((r) => r.facts.compactions === 0)),
  };
}
