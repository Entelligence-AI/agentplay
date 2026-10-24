import { Check } from '@phosphor-icons/react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { useAutotune, useWrite } from '@/data/hooks';
import { useSessionMapSource } from '@/data/SourceContext';
import { cn } from '@/lib/utils';
import type { AppliedEffect, AutotuneStatus } from '@/core/health';
import { InfoButton } from './InfoButton';
import { ErrorText, Panel } from './panels';

export function AutotuneView() {
  const source = useSessionMapSource();
  const q = useAutotune();
  if (!source.autotune) {
    return (
      <div role="note" className="rounded-xl border border-ds-border-subtlest bg-ds-surface-white p-6 text-center ds-text-body-sm-medium">
        Autotune writes to your own repos, so it runs from the CLI on your machine. Run{' '}
        <code className="font-mono">uvx agentplay</code> to set it up.
      </div>
    );
  }
  if (q.isPending) return <Skeleton className="h-96 rounded-xl" />;
  if (q.isError || !q.data) {
    return <p className="ds-text-body-sm-medium text-ds-text-default">Autotune could not be loaded. Update the CLI if it is older than this viewer.</p>;
  }
  const s = q.data;
  return (
    <div className="flex flex-col gap-3">
      <Summary s={s} />
      <div className="flex flex-wrap items-start gap-3">
        <div className="flex min-w-0 flex-[5_1_400px] flex-col gap-3">
          <Repos s={s} />
          <Schedule s={s} />
        </div>
        <div className="flex min-w-0 flex-[7_1_520px] flex-col gap-3">
          <Waiting s={s} />
          <Log s={s} />
        </div>
      </div>
    </div>
  );
}

function WriteError({ write }: { write: ReturnType<typeof useWrite> }) {
  if (!write.isError) return null;
  return <ErrorText>{(write.error as Error).message}</ErrorText>;
}

function Summary({ s }: { s: AutotuneStatus }) {
  const on = s.repos.filter((r) => r.enabled && r.granted);
  const write = useWrite();
  return (
    <section className="flex flex-wrap items-center gap-4 rounded-xl border border-ds-border-subtlest bg-ds-surface-white p-4">
      <div className="min-w-0 flex-1">
        <h2 className="flex items-center gap-1 ds-text-title-sm-semibold text-ds-text-darker">
          Autotune daily <InfoButton term="autotune" />
        </h2>
        <p className="ds-text-body-xs-medium text-ds-text-subtle">
          {on.length
            ? `On for ${on.map((r) => r.name).join(', ')}. Last run ${s.lastRun ? new Date(s.lastRun).toLocaleString() : 'never'}.`
            : 'Off. Allow writes to a repo and turn it on below.'}
        </p>
      </div>
      <Badge variant={on.length ? 'success' : 'secondary'}>{on.length ? 'On' : 'Off'}</Badge>
      <Button
        variant="outline"
        size="sm"
        disabled={!on.length || write.isPending}
        onClick={() => write.mutate({ path: '/api/autotune', body: { action: 'run' } })}
      >
        Run now
      </Button>
      {write.isError ? (
        <div className="basis-full">
          <WriteError write={write} />
        </div>
      ) : null}
    </section>
  );
}

function Repos({ s }: { s: AutotuneStatus }) {
  const write = useWrite();
  return (
    <Panel title="Repository Access" question="Repositories found in your sessions. Autotune writes nowhere else.">
      <WriteError write={write} />
      {s.repos.length ? (
        <ul className="flex flex-col gap-2">
          {s.repos.map((r) => (
            <li key={r.name} className="flex flex-col gap-2 rounded-lg border border-ds-border-subtlest p-3">
              <div className="flex flex-wrap items-center gap-2">
                <div className="min-w-0 flex-1">
                  <div className="ds-text-body-sm-semibold text-ds-text-darker">{r.name}</div>
                  <div className="truncate font-mono text-xs text-ds-text-subtle">{r.root ?? 'no checkout on this machine'}</div>
                </div>
                {r.granted ? (
                  <>
                    <span className="inline-flex items-center gap-1 ds-text-body-xs-semibold text-ds-success-darker">
                      <Check size={13} aria-hidden /> Can write
                    </span>
                    <Button variant="link" size="sm" className="h-7 px-1 text-xs" disabled={write.isPending} onClick={() => write.mutate({ path: '/api/repos', body: { action: 'revoke', repo: r.name } })}>
                      Revoke
                    </Button>
                  </>
                ) : r.root ? (
                  <Button size="sm" className="h-7 text-xs" disabled={write.isPending} onClick={() => write.mutate({ path: '/api/repos', body: { action: 'grant', repo: r.name } })}>
                    Allow writes
                  </Button>
                ) : (
                  <span className="ds-text-body-xs-medium text-ds-text-subtle">Copy-only</span>
                )}
              </div>
              {r.granted ? (
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1 ds-text-body-xs-medium text-ds-text-default">
                  <label className="inline-flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={r.enabled}
                      disabled={write.isPending}
                      onChange={(e) => write.mutate({ path: '/api/autotune', body: { action: e.target.checked ? 'enable' : 'disable', repo: r.name } })}
                      className="size-4 accent-[var(--ds-brand-default)]"
                    />
                    Autotune this repo
                  </label>
                  <label className="inline-flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={r.claudeMd === 'auto'}
                      disabled={!r.enabled || write.isPending}
                      onChange={(e) => write.mutate({ path: '/api/autotune', body: { action: 'shared', repo: r.name, value: e.target.checked } })}
                      className="size-4 accent-[var(--ds-brand-default)]"
                    />
                    Write to shared CLAUDE.md instead of CLAUDE.local.md
                    <InfoButton term="scopeFiles" />
                  </label>
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="ds-text-body-xs-medium text-ds-text-subtle">No repos yet. Build your sessions with the CLI first.</p>
      )}
    </Panel>
  );
}

function Schedule({ s }: { s: AutotuneStatus }) {
  return (
    <Panel title="Run Schedule">
      <p className="ds-text-body-xs-medium text-ds-text-default">
        {s.hookInstalled
          ? 'At the first Claude Code session each day, from a SessionStart hook in your user settings.'
          : s.when && /^\d{1,2}:\d{2}$/.test(s.when)
            ? `Daily at ${s.when}, from the cron line \`agentplay autotune on --at ${s.when}\` printed.`
            : 'Not scheduled yet. Turn it on from a terminal:'}
      </p>
      <pre className="overflow-x-auto rounded-lg border border-ds-border-subtlest bg-ds-gray-subtlest px-3 py-2 font-mono text-xs leading-relaxed text-ds-text-darker">
        {`$ agentplay autotune on            # at session start\n$ agentplay autotune on --at 09:00  # or a set time, via cron\n$ agentplay autotune status`}
      </pre>
      <p className="flex items-center gap-1 ds-text-body-xs-medium text-ds-text-subtle">
        It only edits its own block in each file.
        <InfoButton term="managedBlock" />
      </p>
    </Panel>
  );
}

function Waiting({ s }: { s: AutotuneStatus }) {
  const write = useWrite();
  return (
    <Panel
      title="Pending Approval"
      question="Changes that grant access or run code. Autotune never makes these on its own."
      action={s.pending.length ? <Badge variant="warning">{s.pending.length} waiting</Badge> : null}
    >
      <WriteError write={write} />
      {s.pending.length ? (
        <ul className="flex flex-col gap-2">
          {s.pending.map((p) => (
            <li key={p.fixId} className="flex flex-col gap-2 rounded-lg border border-ds-border-subtlest p-3">
              <div className="flex flex-wrap items-baseline gap-2">
                <span className="ds-text-body-sm-semibold text-ds-text-darker">{p.title}</span>
                {p.points ? <span className="ds-text-body-xs-semibold text-ds-success-darker">+{p.points.toFixed(1)} pts</span> : null}
              </div>
              <p className="ds-text-body-xs-medium text-ds-text-default">{p.pattern}</p>
              <div className="rounded-md border border-ds-border-subtlest font-mono text-xs">
                <div className="bg-ds-gray-subtlest px-2 py-1 text-ds-text-default">{p.action.file}</div>
                <div className="bg-ds-success-subtlest px-2 py-1 text-ds-success-darker">
                  + {p.action.entry ?? p.action.command ?? (p.action.lines ?? []).join(' ')}
                </div>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button size="sm" className="h-7 text-xs" disabled={write.isPending} onClick={() => write.mutate({ path: '/api/autotune', body: { action: 'approve', fixId: p.fixId } })}>
                  {p.action.label}
                </Button>
                <Button variant="outline" size="sm" className="h-7 text-xs" disabled={write.isPending} onClick={() => write.mutate({ path: '/api/autotune', body: { action: 'dismiss', fixId: p.fixId } })}>
                  Not now
                </Button>
                <Button variant="link" size="sm" className="h-7 px-1 text-xs" disabled={write.isPending} onClick={() => write.mutate({ path: '/api/autotune', body: { action: 'never', fixId: p.fixId } })}>
                  Never suggest this
                </Button>
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <p className="ds-text-body-xs-medium text-ds-text-subtle">Nothing waiting.</p>
      )}
    </Panel>
  );
}

const fmtMetric = (e: AppliedEffect, v: number | null) => {
  if (v == null) return '–';
  const name = e.metric?.name;
  if (name === 'verified_share' || name === 'fail_rate') return `${Math.round(v * 100)}%`;
  if (name === 'out_bytes') return `${Math.round(v / 4000)}k tokens/run`;
  return v.toFixed(2);
};

function Log({ s }: { s: AutotuneStatus }) {
  const write = useWrite();
  const active = new Map(s.active.map((e) => [e.id, e]));
  return (
    <Panel title="Change History" question="Each change is measured on its own metric. A change with no effect after 15 sessions is removed.">
      <WriteError write={write} />
      {s.log.length ? (
        <ol className="flex flex-col">
          {s.log.map((l, i) => {
            const e = l.entry ? active.get(l.entry) : undefined;
            return (
              <li key={`${l.at}:${i}`} className="flex gap-3 border-b border-ds-border-subtlest py-2 last:border-0">
                <span
                  aria-hidden
                  className={cn(
                    'mt-1.5 size-2.5 shrink-0 rounded-full',
                    l.kind === 'retire' || l.kind === 'error' ? 'bg-ds-gray-subtler' : l.kind === 'queue' ? 'bg-ds-warning-default' : 'bg-ds-brand-subtler'
                  )}
                />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap justify-between gap-2">
                    <span className="ds-text-body-xs-semibold text-ds-text-darker">
                      {{ add: 'Added', approve: 'You approved', queue: 'Pending approval', retire: 'Retired', error: 'Could not write' }[l.kind] ?? l.kind}: {l.title}
                    </span>
                    <span className="ds-text-body-xs-medium text-ds-text-subtle">{l.at ? new Date(l.at).toLocaleDateString() : ''}</span>
                  </div>
                  <div className="ds-text-body-xs-medium text-ds-text-subtle">
                    {[l.file, l.repo].filter(Boolean).join(' · ')}
                    {l.detail ? ` · ${l.detail}` : ''}
                  </div>
                  {e ? (
                    <div className="ds-text-body-xs-medium text-ds-text-default">
                      {e.sessionsSince
                        ? `${fmtMetric(e, e.baseline)} before → ${fmtMetric(e, e.current)} over ${e.sessionsSince} sessions since`
                        : 'Measuring: no sessions since yet'}
                    </div>
                  ) : null}
                </div>
                {e ? (
                  <Button variant="link" size="sm" className="h-7 shrink-0 px-1 text-xs" onClick={() => write.mutate({ path: '/api/fix', body: { action: 'undo', entry: e.id } })}>
                    Undo
                  </Button>
                ) : null}
              </li>
            );
          })}
        </ol>
      ) : (
        <p className="ds-text-body-xs-medium text-ds-text-subtle">No changes yet.</p>
      )}
    </Panel>
  );
}
