import { useState } from 'react';
import { parseAsString, useQueryStates } from 'nuqs';
import { Check, Copy, Plus } from '@phosphor-icons/react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { useHealth, useWrite } from '@/data/hooks';
import { useSessionMapSource } from '@/data/SourceContext';
import { cn } from '@/lib/utils';
import {
  actionText,
  addedLines,
  type Fix,
  type FixAction,
  type FixPreview,
  type HealthComponent,
  type HealthReport,
} from '@/core/health';
import { ErrorText, Panel } from './panels';

type Range = { from?: string; to?: string };

/** Navigate to one session (and event) in the explorer. */
function useOpenSession() {
  const [, setNav] = useQueryStates({ tab: parseAsString, session: parseAsString, at: parseAsString, sq: parseAsString });
  // sq is cleared: a leftover search could filter the evidence session out of the list.
  return (sessionId: string) => void setNav({ tab: null, session: sessionId, at: null, sq: null });
}

export function RunHealthView({ range }: { range: Range }) {
  const source = useSessionMapSource();
  const health = useHealth(range);
  if (!source.health) {
    return (
      <Note>
        Run health reads your local store through the CLI. Run <code className="font-mono">uvx agentplay@latest</code> to see
        yours; the demo has too few sessions to score.
      </Note>
    );
  }
  if (health.isPending) return <Skeleton className="h-96 rounded-xl" />;
  if (health.isError || !health.data) {
    return <Note>Run health could not be loaded. If the CLI is older than this viewer, update it.</Note>;
  }
  const h = health.data;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-3">
        <ScoreCard h={h} />
        <Components h={h} />
      </div>
      {/* While a new window's report loads, the previous one stays on screen but its
          actions are off: they would apply fixes against the wrong range. */}
      <fieldset disabled={health.isPlaceholderData} aria-busy={health.isPlaceholderData} className="min-w-0">
        <legend className="sr-only">{health.isPlaceholderData ? 'Updating fixes' : 'Fixes'}</legend>
        <PointsOnTheTable h={h} range={range} />
      </fieldset>
    </div>
  );
}

function Note({ children }: { children: React.ReactNode }) {
  return (
    <div role="note" className="rounded-xl border border-ds-border-subtlest bg-ds-surface-white p-6 text-center ds-text-body-sm-medium text-ds-text-default">
      {children}
    </div>
  );
}

// ------------------------------------------------------------------ score

function ScoreCard({ h }: { h: HealthReport }) {
  const v = h.score;
  const delta = v != null && h.prevScore != null ? Math.round((v - h.prevScore) * 10) / 10 : null;
  const C = 2 * Math.PI * 52;
  return (
    <Panel title="Run health" term="runHealth" className="flex-[4_1_320px]">
      <div className="flex items-center gap-5">
        <div className="relative size-32 shrink-0" role="img" aria-label={v == null ? 'Not enough sessions to score' : `Run health ${Math.round(v)} out of 100`}>
          <svg viewBox="0 0 120 120" className="size-32" aria-hidden>
            <circle cx="60" cy="60" r="52" fill="none" stroke="var(--primitive-gray-100)" strokeWidth="12" />
            {v != null ? (
              <circle
                cx="60"
                cy="60"
                r="52"
                fill="none"
                stroke="var(--ds-series-1)"
                strokeWidth="12"
                strokeLinecap="round"
                strokeDasharray={`${(v / 100) * C} ${C}`}
                transform="rotate(-90 60 60)"
              />
            ) : null}
          </svg>
          <div className="absolute inset-0 flex flex-col items-center justify-center">
            <span className="text-4xl font-semibold tracking-tight text-ds-text-darker">{v == null ? '–' : Math.round(v)}</span>
            <span className="ds-text-body-xs-medium text-ds-text-subtle">of 100</span>
          </div>
        </div>
        <div className="flex flex-col gap-2">
          {delta != null ? (
            <Badge variant={delta >= 0 ? 'success' : 'warning'} className="self-start">
              {delta >= 0 ? '+' : ''}
              {delta} vs previous window
            </Badge>
          ) : null}
          <p className="ds-text-body-xs-medium text-ds-text-default">
            {v == null
              ? `Needs ${h.minSessions} sessions in the window to score; ${h.sessions} so far. The parts below still show.`
              : 'How well your agents are set up and run, not how hard the work was. Every input is something you can change.'}
          </p>
        </div>
      </div>
      <p className="border-t border-ds-border-subtlest pt-2 ds-text-body-xs-medium text-ds-text-subtle">
        {h.sessions} root sessions in this window. Cursor sessions count without cost or cache data.
      </p>
    </Panel>
  );
}

function Components({ h }: { h: HealthReport }) {
  const [sel, setSel] = useState(h.components[1]?.key ?? h.components[0]?.key);
  const open = useOpenSession();
  const c = h.components.find((x) => x.key === sel) ?? h.components[0];
  const when: Record<string, string> = {
    shipping: 'Sessions that edit code and open a PR',
    verification: 'Edits followed by a passing test, build or lint',
    efficiency: 'Low failure and retry rate; high cache reuse',
    context: 'Few context compactions and re-reads',
    environment: 'Few permission, configuration and integration errors',
  };
  return (
    <Panel title="Components" term="components" question="20 points each" className="flex-[7_1_520px]">
      <div className="flex flex-col gap-1" role="listbox" aria-label="Run health components">
        {h.components.map((x) => (
          <ComponentRow key={x.key} c={x} when={when[x.key] ?? ''} selected={x.key === c?.key} onSelect={() => setSel(x.key)} />
        ))}
      </div>
      {c ? (
        <div className="flex flex-col gap-2 rounded-lg border border-ds-border-subtlest bg-ds-gray-subtlest p-3">
          <div className="ds-text-body-xs-semibold text-ds-text-darker">
            {c.name} · {c.points == null ? '–' : c.points.toFixed(1)} / 20
          </div>
          <dl className="grid grid-cols-[repeat(auto-fit,minmax(140px,1fr))] gap-2">
            {c.inputs.map(([k, v]) => (
              <div key={k} className="rounded-md border border-ds-border-subtlest bg-ds-surface-white px-2 py-1.5">
                <dt className="ds-text-body-xs-medium text-ds-text-subtle">{k}</dt>
                <dd className="ds-text-body-sm-semibold text-ds-text-darker">{v}</dd>
              </div>
            ))}
          </dl>
          {c.worst.length ? (
            <>
              <div className="ds-text-body-xs-semibold text-ds-text-default">Sessions that cost the most points</div>
              <ul className="flex flex-col gap-1">
                {c.worst.map((w) => (
                  <li key={w.sessionId}>
                    <button
                      type="button"
                      onClick={() => open(w.sessionId)}
                      className="flex w-full items-center gap-2 text-left ds-text-body-xs-medium text-ds-text-default hover:underline"
                    >
                      <span className="min-w-0 flex-1 truncate">{h.titles[w.sessionId] ?? w.sessionId}</span>
                      <span className="text-ds-text-subtle">{w.why}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </>
          ) : null}
        </div>
      ) : null}
    </Panel>
  );
}

function ComponentRow({ c, when, selected, onSelect }: { c: HealthComponent; when: string; selected: boolean; onSelect: () => void }) {
  const delta = c.points != null && c.prevPoints != null ? Math.round((c.points - c.prevPoints) * 10) / 10 : null;
  return (
    <button
      type="button"
      role="option"
      aria-selected={selected}
      onClick={onSelect}
      className={cn(
        'flex items-center gap-4 rounded-lg px-3 py-2 text-left',
        selected ? 'bg-ds-brand-lightest ring-1 ring-ds-brand-subtlest' : 'hover:bg-ds-gray-subtlest'
      )}
    >
      <span className="w-60 shrink-0">
        <span className="block ds-text-body-sm-semibold text-ds-text-darker">{c.name}</span>
        <span className="block ds-text-body-xs-medium text-ds-text-subtle">{when}</span>
      </span>
      <span className="h-2.5 min-w-0 flex-1 overflow-hidden rounded-full bg-ds-gray-subtlest">
        <span
          className="block h-full rounded-full"
          style={{ width: `${((c.points ?? 0) / 20) * 100}%`, background: (c.points ?? 20) < 13 ? 'var(--ds-series-5)' : 'var(--ds-series-1)' }}
        />
      </span>
      <span className="w-16 shrink-0 text-right">
        <span className="text-lg font-semibold text-ds-text-darker">{c.points == null ? '–' : c.points.toFixed(1)}</span>
        <span className="ds-text-body-xs-medium text-ds-text-subtle"> /20</span>
      </span>
      <span className={cn('w-10 shrink-0 text-right ds-text-body-xs-semibold', delta == null ? 'text-ds-text-subtle' : delta >= 0 ? 'text-ds-success-darker' : 'text-ds-warning-darker')}>
        {delta == null ? '' : `${delta >= 0 ? '+' : ''}${delta}`}
      </span>
    </button>
  );
}

// ------------------------------------------------------------------ fixes

const COMPONENT_LABEL: Record<string, string> = {
  shipping: 'Shipping',
  verification: 'Verification',
  efficiency: 'Efficiency',
  context: 'Context',
  environment: 'Environment',
};

export function PointsOnTheTable({ h, range }: { h: HealthReport; range: Range }) {
  const write = useWrite();
  const [showAll, setShowAll] = useState(false);
  // Own flag: write.isPending drops between sequential calls and would re-enable the button mid-run.
  const [applying, setApplying] = useState(false);
  const [batchError, setBatchError] = useState<string | null>(null);
  const applied = new Map(h.applied.map((a) => [a.fixId, a]));
  const repos = new Map(h.repos.map((r) => [r.name, r]));
  const fixes = showAll ? h.fixes : h.fixes.slice(0, 5);
  const pending = h.fixes.filter((f) => !applied.has(f.id) && f.actions.length && repos.get(f.repo)?.granted);
  const applyAll = async () => {
    setApplying(true);
    setBatchError(null);
    let done = 0;
    try {
      // One at a time: the server serializes writes, and we stop at the first failure.
      for (const f of pending) {
        await write.mutateAsync({ path: '/api/fix', body: { action: 'apply', fix: f.id, i: 0, ...range } });
        done++;
      }
    } catch (e) {
      setBatchError(`Applied ${done} of ${pending.length}. "${pending[done].title}" failed: ${(e as Error).message}`);
    } finally {
      setApplying(false);
    }
  };
  const gain = h.fixes.filter((f) => !applied.has(f.id)).reduce((s, f) => s + f.points, 0);
  return (
    <Panel
      title="Points on the table"
      term="pointsOnTable"
      question="Specific fixes, ranked by the points each would recover. Each names the command, tool or server behind it."
      action={
        pending.length ? (
          <Button
            size="sm"
            disabled={applying}
            onClick={() => void applyAll()}
          >
            Apply {pending.length} {pending.length === 1 ? 'fix' : 'fixes'}
          </Button>
        ) : null
      }
    >
      {batchError ? <ErrorText>{batchError}</ErrorText> : null}
      {h.fixes.length ? (
        <>
          <p className="ds-text-body-xs-medium text-ds-text-subtle">
            Up to +{gain.toFixed(1)} points if every open fix works. Overlapping fixes are not double counted in the score itself.
          </p>
          <ol className="flex flex-col gap-2">
            {fixes.map((f) => (
              <FixCard
                key={f.id}
                fix={f}
                applied={applied.get(f.id)}
                repo={repos.get(f.repo)}
                range={range}
                titles={h.titles}
              />
            ))}
          </ol>
          {h.fixes.length > 5 ? (
            <Button variant="ghost" size="sm" className="self-start" onClick={() => setShowAll((v) => !v)}>
              {showAll ? 'Show top 5' : `Show all ${h.fixes.length}`}
            </Button>
          ) : null}
        </>
      ) : (
        <p className="py-6 text-center ds-text-body-xs-medium text-ds-text-subtle">
          Nothing specific stands out in this window. Fixes appear when a pattern can be traced to a named command, tool or server.
        </p>
      )}
    </Panel>
  );
}

function FixCard({
  fix,
  applied,
  repo,
  range,
  titles,
}: {
  fix: Fix;
  applied?: { id: string; file: string };
  repo?: { name: string; root: string | null; granted: boolean };
  range: Range;
  titles: Record<string, string | null>;
}) {
  const write = useWrite();
  const source = useSessionMapSource();
  const open = useOpenSession();
  const [preview, setPreview] = useState<{ i: number; p: FixPreview } | null>(null);
  const [copied, setCopied] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const showPreview = async (i: number) => {
    if (preview?.i === i) return setPreview(null);
    if (!source.fixPreview) return;
    setPreviewing(true);
    setPreviewError(null);
    try {
      setPreview({ i, p: await source.fixPreview({ fix: fix.id, i, ...range }) });
    } catch (e) {
      setPreview(null);
      setPreviewError(`Could not load the preview: ${(e as Error).message}`);
    } finally {
      setPreviewing(false);
    }
  };
  // A preview (and its hash) is stale once the file has been written or restored.
  const onWritten = { onSuccess: () => setPreview(null) };
  return (
    <li className={cn('flex flex-col gap-2 rounded-lg border p-3', applied ? 'border-ds-brand-subtlest bg-ds-brand-lightest' : 'border-ds-border-subtlest')}>
      <div className="flex gap-3">
        <span
          className={cn(
            'flex size-12 shrink-0 flex-col items-center justify-center rounded-lg border',
            applied ? 'border-transparent bg-ds-brand-default text-ds-brand-subtlest' : 'border-ds-brand-subtlest bg-ds-brand-lightest text-ds-success-darker'
          )}
        >
          <span className="text-base font-semibold">{fix.points ? `+${fix.points.toFixed(1)}` : '–'}</span>
          <span className="text-[10px]">pts</span>
        </span>
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="ds-text-body-sm-semibold text-ds-text-darker">{fix.title}</span>
            <Badge variant="secondary">{COMPONENT_LABEL[fix.component] ?? fix.component}</Badge>
            {fix.repo ? <Badge variant="outline">{fix.repo}</Badge> : null}
          </div>
          <p className="ds-text-body-xs-medium text-ds-text-default">{fix.pattern}</p>
          <p className="ds-text-body-xs-medium text-ds-text-subtle">Expected: {fix.effect}</p>
          {fix.sessions.length ? (
            <button
              type="button"
              onClick={() => open(fix.sessions[0])}
              className="self-start ds-text-body-xs-medium text-ds-text-default underline-offset-2 hover:underline"
              title={titles[fix.sessions[0]] ?? undefined}
            >
              {fix.sessions.length} {fix.sessions.length === 1 ? 'session' : 'sessions'} →
            </button>
          ) : null}
          <div className="mt-1 flex flex-wrap items-center gap-2">
            {applied ? (
              <>
                <span className="inline-flex items-center gap-1.5 ds-text-body-xs-semibold text-ds-success-darker">
                  <Check size={14} aria-hidden /> Added to {applied.file}
                </span>
                <Button
                  variant="link"
                  size="sm"
                  className="h-7 px-1"
                  disabled={write.isPending}
                  onClick={() => write.mutate({ path: '/api/fix', body: { action: 'undo', entry: applied.id } }, onWritten)}
                >
                  Undo
                </Button>
              </>
            ) : !fix.actions.length ? (
              <span className="ds-text-body-xs-medium text-ds-text-subtle">Nothing to write: this names an integration to fix by hand.</span>
            ) : repo?.granted ? (
              <>
                {fix.actions.map((a, i) => (
                  <ActionButton
                    key={a.label}
                    a={a}
                    primary={i === 0}
                    disabled={write.isPending}
                    onClick={() =>
                      write.mutate(
                        {
                          path: '/api/fix',
                          body: { action: 'apply', fix: fix.id, i, hash: preview?.i === i ? preview.p.hash : undefined, ...range },
                        },
                        onWritten
                      )
                    }
                  />
                ))}
                <Button variant="link" size="sm" className="h-7 px-1 text-xs" disabled={previewing} onClick={() => void showPreview(0)}>
                  {previewing ? 'Loading preview' : preview ? 'Hide change' : 'Preview change'}
                </Button>
                <span className="ds-text-body-xs-medium text-ds-text-subtle">
                  or run <code className="rounded bg-ds-gray-subtlest px-1.5 py-0.5 font-mono text-ds-text-darker">agentplay fix apply {fix.id}</code>
                </span>
              </>
            ) : repo?.root ? (
              <>
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 text-xs"
                  disabled={write.isPending}
                  onClick={() => write.mutate({ path: '/api/repos', body: { action: 'grant', repo: fix.repo } })}
                >
                  Allow writes to {fix.repo}
                </Button>
                <span className="ds-text-body-xs-medium text-ds-text-subtle">agentplay writes only to the four agent files, and every change can be undone.</span>
              </>
            ) : (
              <>
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 gap-1 text-xs"
                  onClick={() => {
                    void navigator.clipboard?.writeText(actionText(fix.actions[0]));
                    setCopied(true);
                  }}
                >
                  <Copy size={12} aria-hidden /> {copied ? 'Copied' : 'Copy'}
                </Button>
                <span className="ds-text-body-xs-medium text-ds-text-subtle">No checkout of {fix.repo || 'this repo'} on this machine, so agentplay can&rsquo;t write it.</span>
              </>
            )}
          </div>
        </div>
      </div>
      {write.isError ? <ErrorText>{(write.error as Error).message}</ErrorText> : null}
      {previewError ? <ErrorText>{previewError}</ErrorText> : null}
      {preview ? (
        <div className="ml-[3.75rem] overflow-hidden rounded-md border border-ds-border-subtlest">
          <div className="flex justify-between bg-ds-gray-subtlest px-3 py-1 font-mono text-xs text-ds-text-default">
            <span>{preview.p.file}</span>
          </div>
          <pre className="max-h-64 overflow-auto py-1 font-mono text-xs leading-relaxed">
            {addedLines(preview.p.before, preview.p.after).map((l, i) => (
              <div key={i} className={cn('whitespace-pre-wrap px-3', l.added ? 'bg-ds-success-subtlest text-ds-success-darker' : 'text-ds-text-subtle')}>
                {l.added ? '+ ' : '  '}
                {l.text}
              </div>
            ))}
          </pre>
        </div>
      ) : null}
    </li>
  );
}

function ActionButton({ a, primary, disabled, onClick }: { a: FixAction; primary: boolean; disabled: boolean; onClick: () => void }) {
  return (
    <Button
      variant="outline"
      size="sm"
      disabled={disabled}
      onClick={onClick}
      className={cn('h-7 gap-1 px-2.5 text-xs', primary ? 'border-ds-brand-subtler text-ds-text-darker' : 'text-ds-text-default')}
    >
      {primary ? <Plus size={12} aria-hidden /> : null}
      {a.label}
    </Button>
  );
}

