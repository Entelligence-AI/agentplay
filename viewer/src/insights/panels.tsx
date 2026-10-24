import { useState, type ReactNode } from 'react';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import { Badge } from '@/components/ui/badge';
import { SERIES_OTHER, seriesColor } from '@/components/dataviz';
import { cn } from '@/lib/utils';
import { fmtDur, fmtUsd, OUTCOME_META, type SessionOutcome } from '@/core/sessionMapModel';
import {
  compactionSummary,
  dayRange,
  failuresBySource,
  headline,
  MIN_SAMPLE,
  OUTCOME_ORDER,
  outcomesByWeek,
  spendByDay,
  topFailingCommands,
  WASTE_BUCKETS,
  wasteSummary,
  weekStart,
  type InsightsSession,
  type PrRate,
  type WasteBucket,
} from '@/core/insights';
import { InfoButton } from './InfoButton';
import type { TermKey } from './glossary';

// ------------------------------------------------------------------ shared

/** Inline failure message, announced to screen readers. */
export function ErrorText({ children }: { children: ReactNode }) {
  return (
    <p role="alert" className="rounded-md bg-ds-danger-subtlest px-3 py-2 ds-text-body-xs-medium text-ds-danger-darkest">
      {children}
    </p>
  );
}

export function Panel({
  title,
  term,
  question,
  action,
  className,
  children,
}: {
  title: string;
  term?: TermKey;
  question?: string;
  action?: ReactNode;
  className?: string;
  children: ReactNode;
}) {
  const id = `insights-${title.toLowerCase().replace(/[^a-z]+/g, '-')}`;
  return (
    <section
      aria-labelledby={id}
      className={cn(
        'flex min-w-0 flex-col gap-3 rounded-xl border border-ds-border-subtlest bg-ds-surface-white p-4',
        className
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h2 id={id} className="flex items-center gap-1 ds-text-body-sm-semibold text-ds-text-darker">
            {title}
            {term ? <InfoButton term={term} /> : null}
          </h2>
          {question ? <p className="ds-text-body-xs-medium text-ds-text-subtle">{question}</p> : null}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

export const pct = (x: number | null): string => (x == null ? '–' : `${Math.round(x * 100)}%`);

/** A rate with its sample size; greyed below `MIN_SAMPLE`. */
function Rate({ r, className }: { r: PrRate; className?: string }) {
  const thin = r.edited < MIN_SAMPLE;
  return (
    <span
      // The grey wins over the caller's ink: a thin rate must never read as a firm one.
      className={cn(className, thin && 'text-ds-text-subtle')}
      title={thin ? `Only ${r.edited} sessions edited code: too few for a reliable rate` : undefined}
    >
      {pct(r.rate)}
    </span>
  );
}

function Swatch({ color }: { color: string }) {
  return <span aria-hidden className="inline-block size-2.5 shrink-0 rounded-sm" style={{ background: color }} />;
}

function Legend({ items }: { items: { label: string; color: string }[] }) {
  return (
    <ul className="flex flex-wrap gap-x-3 gap-y-1 ds-text-body-xs-medium text-ds-text-default">
      {items.map((i) => (
        <li key={i.label} className="flex items-center gap-1.5">
          <Swatch color={i.color} />
          {i.label}
        </li>
      ))}
    </ul>
  );
}

const shortDate = (iso: string) =>
  new Date(`${iso}T00:00:00Z`).toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' });

// ------------------------------------------------------------------ tiles

function Tile({ label, term, value, foot }: { label: string; term: TermKey; value: ReactNode; foot: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1 rounded-xl border border-ds-border-subtlest bg-ds-surface-white p-4">
      <div className="flex items-center justify-between gap-2 ds-text-body-xs-medium text-ds-text-subtle">
        {label}
        <InfoButton term={term} />
      </div>
      <div className="text-2xl font-semibold tracking-tight text-ds-text-darker">{value}</div>
      <div className="ds-text-body-xs-medium text-ds-text-subtle">{foot}</div>
    </div>
  );
}

export function KpiTiles({ rows }: { rows: InsightsSession[] }) {
  const h = headline(rows);
  return (
    <div className="grid grid-cols-[repeat(auto-fit,minmax(200px,1fr))] gap-3">
      <Tile label="Sessions" term="sessions" value={h.sessions} foot="root sessions in the window" />
      <Tile
        label="Spend"
        term="spend"
        value={fmtUsd(h.spend)}
        foot={h.noCost ? `${h.noCost} Cursor ${h.noCost === 1 ? 'session' : 'sessions'} without cost data left out` : 'at list API prices'}
      />
      <Tile
        label="Cost per PR"
        term="costPerPr"
        value={h.costPerPr == null ? '–' : fmtUsd(h.costPerPr)}
        foot={`spend ÷ ${h.prsOpened} ${h.prsOpened === 1 ? 'PR' : 'PRs'} opened`}
      />
      <Tile
        label="PR rate"
        term="prRate"
        value={<Rate r={h.pr} />}
        foot={`${h.pr.opened} of ${h.pr.edited} code-changing sessions · ${h.prsMerged} merged`}
      />
    </div>
  );
}

// ------------------------------------------------------------------ outcomes

export const OUTCOME_COLOR: Record<SessionOutcome, string> = {
  pr_created: 'var(--ds-series-1)',
  verified: 'var(--ds-series-6)',
  completed: 'var(--ds-series-other)',
  interrupted: 'var(--ds-series-4)',
  blocked: 'var(--ds-series-5)',
  unresolved_errors: 'var(--ds-danger-default)',
  incomplete: 'var(--primitive-gray-300)',
};

const MAX_WEEKS = 16;

export function OutcomeMix({ rows }: { rows: InsightsSession[] }) {
  const weeks = outcomesByWeek(rows).slice(-MAX_WEEKS);
  const max = Math.max(1, ...weeks.map((w) => w.total));
  return (
    <Panel
      title="Outcome mix"
      term="outcomes"
      question="Weekly session outcomes, and the trend in PR rate"
      className="flex-[7_1_520px]"
    >
      {weeks.length ? (
        <>
          <div className="flex h-48 items-end gap-2 border-b border-ds-border-subtlest pt-5">
            {weeks.map((w) => (
              <div key={w.week} className="flex h-full min-w-0 flex-1 flex-col items-center justify-end gap-1">
                <Rate r={w.pr} className="ds-text-body-xs-semibold text-ds-text-darker" />
                <div
                  role="img"
                  aria-label={`Week of ${shortDate(w.week)}: ${w.total} sessions, PR rate ${pct(w.pr.rate)}`}
                  className="flex w-full max-w-10 flex-col-reverse overflow-hidden rounded-t"
                  style={{ height: `${(w.total / max) * 100}%` }}
                >
                  {OUTCOME_ORDER.map((o) =>
                    w.counts[o] ? (
                      <div key={o} style={{ height: `${(w.counts[o] / w.total) * 100}%`, background: OUTCOME_COLOR[o] }} />
                    ) : null
                  )}
                </div>
              </div>
            ))}
          </div>
          <div className="-mt-2 flex gap-2">
            {weeks.map((w) => (
              <span key={w.week} className="min-w-0 flex-1 truncate text-center ds-text-body-xs-medium text-ds-text-subtle">
                {shortDate(w.week)}
              </span>
            ))}
          </div>
          <Legend items={OUTCOME_ORDER.map((o) => ({ label: OUTCOME_META[o].label, color: OUTCOME_COLOR[o] }))} />
          <p className="ds-text-body-xs-medium text-ds-text-subtle">
            Number above each bar: that week&rsquo;s PR rate. Grey means too few sessions to read much into it.
          </p>
        </>
      ) : (
        <Empty>No sessions in this window.</Empty>
      )}
    </Panel>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return <p className="py-6 text-center ds-text-body-xs-medium text-ds-text-subtle">{children}</p>;
}

// ------------------------------------------------------------------ spend

export function SpendPanel({ rows }: { rows: InsightsSession[] }) {
  const [by, setBy] = useState<'agent' | 'model'>('agent');
  const { days, series } = spendByDay(rows, by);
  const h = headline(rows);
  const colorOf = (key: string) => {
    const i = series.findIndex((s) => s.key === key);
    return key === 'Other' ? SERIES_OTHER : seriesColor(i);
  };
  // Daily bars up to three months; weekly beyond that so bars stay readable.
  const range = days.length ? dayRange(days[0].day, days[days.length - 1].day) : [];
  const weekly = range.length > 92;
  const buckets = new Map<string, Record<string, number>>();
  for (const d of range) buckets.set(weekly ? weekStart(d) : d, buckets.get(weekly ? weekStart(d) : d) ?? {});
  for (const d of days) {
    const key = weekly ? weekStart(d.day) : d.day;
    const b = buckets.get(key) ?? {};
    buckets.set(key, b);
    for (const [k, v] of Object.entries(d.parts)) b[k] = (b[k] ?? 0) + v;
  }
  const bars = [...buckets.entries()].map(([key, parts]) => ({
    key,
    parts,
    total: Object.values(parts).reduce((s, v) => s + v, 0),
  }));
  const max = Math.max(0.01, ...bars.map((b) => b.total));
  return (
    <Panel
      title="Spend"
      term="spend"
      question={`Where does the money go? ${fmtUsd(h.spend)} in this window${weekly ? ', by week' : ', by day'}.`}
      className="flex-[7_1_520px]"
      action={
        <ToggleGroup
          value={[by]}
          onValueChange={(v: unknown[]) => {
            const next = v.find((x): x is 'agent' | 'model' => x === 'agent' || x === 'model');
            if (next) setBy(next);
          }}
          aria-label="Split spend by"
        >
          <ToggleGroupItem value="agent">By agent</ToggleGroupItem>
          <ToggleGroupItem value="model">By model</ToggleGroupItem>
        </ToggleGroup>
      }
    >
      {bars.length ? (
        <>
          <div className="flex gap-2">
            <div className="flex h-40 w-12 flex-col justify-between text-right font-mono text-[11px] text-ds-text-subtle">
              <span>{fmtUsd(max)}</span>
              <span>$0</span>
            </div>
            <div className="flex h-40 min-w-0 flex-1 items-end gap-px border-b border-ds-border-subtlest">
              {bars.map((b) => (
                <div
                  key={b.key}
                  role="img"
                  aria-label={`${shortDate(b.key)}: ${fmtUsd(b.total)}`}
                  title={`${shortDate(b.key)} · ${fmtUsd(b.total)}`}
                  className="flex min-w-0 flex-1 flex-col-reverse overflow-hidden rounded-t-sm"
                  style={{ height: `${(b.total / max) * 100}%` }}
                >
                  {series.map((s) =>
                    b.parts[s.key] ? (
                      <div key={s.key} style={{ height: `${(b.parts[s.key] / b.total) * 100}%`, background: colorOf(s.key) }} />
                    ) : null
                  )}
                </div>
              ))}
            </div>
          </div>
          <div className="flex justify-between pl-14 ds-text-body-xs-medium text-ds-text-subtle">
            <span>{shortDate(bars[0].key)}</span>
            <span>{shortDate(bars[bars.length - 1].key)}</span>
          </div>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <Legend items={series.map((s) => ({ label: `${s.key} · ${fmtUsd(s.total)}`, color: colorOf(s.key) }))} />
            {h.noCost ? (
              <span className="ds-text-body-xs-medium text-ds-text-subtle">
                {h.noCost} Cursor {h.noCost === 1 ? 'session has' : 'sessions have'} no cost data and {h.noCost === 1 ? 'is' : 'are'} left out.
              </span>
            ) : null}
          </div>
        </>
      ) : (
        <Empty>No sessions with cost data in this window.</Empty>
      )}
    </Panel>
  );
}

// ------------------------------------------------------------------ failures

const ERR_CLASS_ORDER = ['exit_code', 'timeout', 'permission_denied', 'not_found', 'blocked'];
const classColor = (cls: string) => {
  const i = ERR_CLASS_ORDER.indexOf(cls);
  return i < 0 ? SERIES_OTHER : seriesColor(i);
};

export function FailingCommands({ rows }: { rows: InsightsSession[] }) {
  const cmds = topFailingCommands(rows);
  const max = Math.max(1, ...cmds.map((c) => c.fails));
  const classes = [...new Set(cmds.flatMap((c) => Object.keys(c.byClass)))].sort(
    (a, b) => (ERR_CLASS_ORDER.indexOf(a) + 99) % 99 - ((ERR_CLASS_ORDER.indexOf(b) + 99) % 99)
  );
  return (
    <Panel
      title="Top failing commands"
      term="failingCommands"
      question="Which commands fail most? Command names only; arguments are never stored."
      className="flex-[7_1_520px]"
    >
      {cmds.length ? (
        <>
          <ul className="flex flex-col gap-2">
            {cmds.map((c) => (
              <li key={c.verb} className="flex items-center gap-3">
                <span className="w-36 shrink-0 truncate font-mono text-xs text-ds-text-darker" title={c.verb}>
                  {c.verb}
                </span>
                <span className="flex min-w-0 flex-1 items-center gap-2">
                  <span
                    role="img"
                    aria-label={`${c.fails} failures`}
                    className="flex h-3.5 overflow-hidden rounded-sm"
                    style={{ width: `${(c.fails / max) * 100}%` }}
                  >
                    {Object.entries(c.byClass)
                      .sort((a, b) => b[1] - a[1])
                      .map(([cls, n]) => (
                        <span key={cls} title={`${cls}: ${n}`} style={{ width: `${(n / c.fails) * 100}%`, background: classColor(cls) }} />
                      ))}
                  </span>
                  <span className="ds-text-body-xs-semibold text-ds-text-darker">{c.fails}</span>
                </span>
                <span className="w-40 shrink-0 text-right ds-text-body-xs-medium text-ds-text-subtle">
                  {pct(c.failRate)} fail · {c.retries} {c.retries === 1 ? 'retry' : 'retries'}
                </span>
              </li>
            ))}
          </ul>
          <Legend items={classes.map((cls) => ({ label: cls, color: classColor(cls) }))} />
        </>
      ) : (
        <Empty>No failed commands in this window.</Empty>
      )}
    </Panel>
  );
}

const SOURCE_LABEL: Record<string, string> = {
  tool: 'Tool',
  mcp: 'MCP',
  llm: 'LLM',
  hook: 'Hook',
  orchestration: 'Orchestration',
};

export function WhatFails({ rows }: { rows: InsightsSession[] }) {
  const groups = failuresBySource(rows);
  const max = Math.max(1, ...groups.flatMap((g) => g.classes.map((c) => c.n)));
  return (
    <Panel
      title="What fails"
      term="whatFails"
      question="Which kinds of failure dominate? By error class, grouped by source."
      className="flex-[5_1_360px]"
    >
      {groups.length ? (
        <div className="flex flex-col gap-3">
          {groups.map((g) => (
            <div key={g.source} className="flex flex-col gap-1">
              <div className="flex justify-between ds-text-body-xs-semibold uppercase tracking-wide text-ds-text-subtle">
                <span>{SOURCE_LABEL[g.source] ?? g.source}</span>
                <span>{g.total}</span>
              </div>
              {g.classes.map((c) => (
                <div key={c.cls} className="flex items-center gap-2 ds-text-body-xs-medium">
                  <span className="w-36 shrink-0 truncate font-mono text-ds-text-default">{c.cls}</span>
                  <span className="min-w-0 flex-1">
                    <span
                      className="block h-2.5 rounded-sm bg-ds-brand-subtler"
                      style={{ width: `${Math.max(2, (c.n / max) * 100)}%` }}
                    />
                  </span>
                  <span className="w-10 text-right font-mono text-ds-text-darker">{c.n}</span>
                </div>
              ))}
            </div>
          ))}
        </div>
      ) : (
        <Empty>No failures in this window.</Empty>
      )}
    </Panel>
  );
}

// ------------------------------------------------------------------ waste

const WASTE_LABEL: Record<WasteBucket, { label: string; note: string }> = {
  abandoned: { label: 'Abandoned spend', note: 'unresolved or stopped mid-work' },
  retry: { label: 'Retry loops', note: 'failed calls and their retries' },
  cacheMiss: { label: 'Cache misses', note: 'fresh input past a warm cache' },
  reread: { label: 'Re-reads', note: 'files read again unchanged' },
  deadSubagent: { label: 'Dead subagents', note: 'no result, failed or killed' },
};

export function WastePanel({ rows }: { rows: InsightsSession[] }) {
  const w = wasteSummary(rows);
  return (
    <Panel
      title="Waste"
      term="waste"
      question="Where is spend wasted?"
      action={<Badge variant="warning">estimate</Badge>}
      className="flex-[5_1_360px]"
    >
      <div className="flex items-baseline gap-2">
        <span className="text-2xl font-semibold tracking-tight text-ds-text-darker">{fmtUsd(w.total)}</span>
        <span className="ds-text-body-xs-medium text-ds-text-subtle">{pct(w.share)} of spend</span>
      </div>
      {w.total > 0 ? (
        <div role="img" aria-label="Waste split by bucket" className="flex h-5 gap-0.5 overflow-hidden rounded-md">
          {WASTE_BUCKETS.map((b, i) =>
            w.buckets[b] > 0 ? (
              <span key={b} style={{ width: `${(w.buckets[b] / w.total) * 100}%`, background: seriesColor(i) }} />
            ) : null
          )}
        </div>
      ) : null}
      <ul className="flex flex-col gap-1">
        {WASTE_BUCKETS.map((b, i) => (
          <li key={b} className="flex items-center gap-2 ds-text-body-xs-medium">
            <Swatch color={seriesColor(i)} />
            <span className="min-w-0 flex-1 text-ds-text-default">
              {WASTE_LABEL[b].label}
              <span className="text-ds-text-subtle"> · {WASTE_LABEL[b].note}</span>
            </span>
            <span className="font-mono text-ds-text-darker">{fmtUsd(w.buckets[b])}</span>
          </li>
        ))}
      </ul>
      <p className="border-t border-ds-border-subtlest pt-2 ds-text-body-xs-medium text-ds-text-subtle">
        Stalls (time, not money): {w.stallMs ? fmtDur(w.stallMs) : 'none'}
      </p>
    </Panel>
  );
}

// ------------------------------------------------------------------ compaction

function RateBar({ label, r, color }: { label: string; r: PrRate; color: string }) {
  return (
    <div className="flex items-center gap-3 ds-text-body-xs-medium">
      <span className="w-28 shrink-0 text-ds-text-default">{label}</span>
      <span className="h-3 min-w-0 flex-1 overflow-hidden rounded-sm bg-ds-gray-subtlest">
        <span className="block h-full" style={{ width: `${(r.rate ?? 0) * 100}%`, background: color }} />
      </span>
      <span className="w-24 shrink-0 text-right">
        <Rate r={r} className="ds-text-body-xs-semibold text-ds-text-darker" />{' '}
        <span className="text-ds-text-subtle">n={r.edited}</span>
      </span>
    </div>
  );
}

const fmtTokens = (n: number | null) => (n == null ? '–' : n >= 1000 ? `${Math.round(n / 1000)}k` : String(n));

export function CompactionPanel({ rows }: { rows: InsightsSession[] }) {
  const c = compactionSummary(rows);
  const stat = (value: string, label: string) => (
    <div className="rounded-lg bg-ds-gray-subtlest p-3">
      <div className="text-lg font-semibold text-ds-text-darker">{value}</div>
      <div className="ds-text-body-xs-medium text-ds-text-subtle">{label}</div>
    </div>
  );
  return (
    <Panel
      title="Compaction"
      term="compaction"
      question="How often does context fill up, and does it hurt?"
      className="flex-[5_1_360px]"
    >
      <div className="grid grid-cols-3 gap-2">
        {stat(c.perSession.toFixed(1), 'avg per session')}
        {stat(pct(c.share), 'of sessions compact')}
        {stat(fmtTokens(c.medianPreTokens), 'median tokens at compaction')}
      </div>
      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-1 ds-text-body-xs-semibold text-ds-text-default">
          PR rate
          <InfoButton term="prRate" />
        </div>
        <RateBar label="No compaction" r={c.without} color="var(--ds-series-1)" />
        <RateBar label="Compacted" r={c.withCompaction} color="var(--ds-series-5)" />
      </div>
      <p className="border-t border-ds-border-subtlest pt-2 ds-text-body-xs-medium text-ds-text-subtle">
        {c.rereads} {c.rereads === 1 ? 'file' : 'files'} re-read after a compaction with no change in between.
      </p>
    </Panel>
  );
}
