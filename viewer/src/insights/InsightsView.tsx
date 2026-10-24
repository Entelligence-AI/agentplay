import { useMemo, useState } from 'react';
import { parseAsStringEnum, useQueryState } from 'nuqs';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import { CalendarBlankIcon } from '@phosphor-icons/react';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { useInsightsStats } from '@/data/hooks';
import { useSessionMapSource } from '@/data/SourceContext';
import { useWindowRange } from '@/data/windowRange';
import { WINDOW_OPTIONS, type WindowKey } from '@/core/windowOptions';
import { filterOptions, filterSessions, type InsightsFilter } from '@/core/insights';
import {
  CompactionPanel,
  FailingCommands,
  KpiTiles,
  OutcomeMix,
  SpendPanel,
  WastePanel,
  WhatFails,
} from './panels';
import { SearchHeatmap } from './SearchHeatmap';
import { RunHealthView } from './RunHealthView';
import { AutotuneView } from './AutotuneView';

const PAGES = ['overview', 'health', 'autotune'] as const;
type Page = (typeof PAGES)[number];

const ALL = '__all__';

function FilterSelect({
  label,
  value,
  options,
  allLabel,
  onChange,
  format = (v) => v,
}: {
  label: string;
  value: string | null;
  options: string[];
  allLabel: string;
  onChange: (v: string | null) => void;
  format?: (v: string) => string;
}) {
  const items = [{ value: ALL, label: allLabel }, ...options.map((o) => ({ value: o, label: format(o) }))];
  return (
    <label className="flex min-w-40 flex-col gap-1 ds-text-body-xs-semibold uppercase tracking-wide text-ds-text-subtle">
      {label}
      <Select
        value={value ?? ALL}
        items={items}
        onValueChange={(v: string | null) => onChange(!v || v === ALL ? null : v)}
      >
        <SelectTrigger aria-label={label} className="h-8 normal-case tracking-normal">
          <SelectValue />
        </SelectTrigger>
        <SelectContent align="start">
          {items.map((i) => (
            <SelectItem key={i.value} value={i.value}>
              {i.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </label>
  );
}

const AGENT_LABEL: Record<string, string> = {
  'claude-code': 'Claude Code',
  codex: 'Codex',
  cursor: 'Cursor',
  kiro: 'Kiro',
  opencode: 'OpenCode',
  copilot: 'Copilot',
};

/**
 * Insights: patterns across every stored session. Shares the time window
 * with the session list; agent, repo and model filters are local to the page.
 */
export function InsightsView({
  windowKey,
  onWindowChange,
}: {
  windowKey: WindowKey;
  onWindowChange: (key: WindowKey) => void;
}) {
  const source = useSessionMapSource();
  // Stable per window and calendar day: a range off "now" that moved every render
  // would refetch forever, one fixed at mount would drop today after midnight.
  const range = useWindowRange(windowKey);
  const stats = useInsightsStats(range);
  const [filter, setFilter] = useState<InsightsFilter>({});
  const [page, setPage] = useQueryState('ipage', parseAsStringEnum([...PAGES]).withDefault('overview'));
  const all = stats.data?.sessions ?? [];
  const options = useMemo(() => filterOptions(all), [all]);
  const rows = useMemo(() => filterSessions(all, filter), [all, filter]);

  if (!source.stats && page === 'overview') {
    return (
      <Notice>
        Insights read the facts the CLI stores for each session, so they are not available for opened map files. Go back
        to your local sessions, or run <code className="font-mono">agentplay</code> to see them.
      </Notice>
    );
  }

  return (
    <main className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto pb-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="ds-text-title-md-semibold text-ds-text-darker">Insights</h1>
          <p className="ds-text-body-xs-medium text-ds-text-subtle">
            Patterns across your sessions. Root sessions only; computed on this machine.
          </p>
        </div>
        <ToggleGroup
          value={[page]}
          onValueChange={(v: unknown[]) => {
            const next = v.find((x): x is Page => (PAGES as readonly unknown[]).includes(x));
            if (next) void setPage(next === 'overview' ? null : next);
          }}
          aria-label="Insights pages"
          className="mr-auto"
        >
          <ToggleGroupItem value="overview">Overview</ToggleGroupItem>
          <ToggleGroupItem value="health">Run health</ToggleGroupItem>
          <ToggleGroupItem value="autotune">Autotune</ToggleGroupItem>
        </ToggleGroup>
        <div className="flex flex-wrap items-end gap-2" role="group" aria-label="Filters">
          <label className="flex min-w-36 flex-col gap-1 ds-text-body-xs-semibold uppercase tracking-wide text-ds-text-subtle">
            Window
            <Select
              value={windowKey}
              items={WINDOW_OPTIONS.map((o) => ({ value: o.key, label: o.label }))}
              onValueChange={(v: string | null) => {
                if (v) onWindowChange(v as WindowKey);
              }}
            >
              <SelectTrigger aria-label="Time window" className="h-8 gap-2 normal-case tracking-normal">
                <CalendarBlankIcon size={14} weight="duotone" className="shrink-0 text-ds-text-subtle" />
                <SelectValue />
              </SelectTrigger>
              <SelectContent align="start">
                {WINDOW_OPTIONS.map((o) => (
                  <SelectItem key={o.key} value={o.key}>
                    {o.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>
          {page === 'overview' ? (<>
          <FilterSelect
            label="Agent"
            value={filter.agent ?? null}
            options={options.agents}
            allLabel="All agents"
            format={(a) => AGENT_LABEL[a] ?? a}
            onChange={(agent) => setFilter((f) => ({ ...f, agent }))}
          />
          <FilterSelect
            label="Repo"
            value={filter.repo ?? null}
            options={options.repos}
            allLabel="All repos"
            onChange={(repo) => setFilter((f) => ({ ...f, repo }))}
          />
          <FilterSelect
            label="Model"
            value={filter.model ?? null}
            options={options.models}
            allLabel="All models"
            onChange={(model) => setFilter((f) => ({ ...f, model }))}
          />
          </>) : null}
        </div>
      </div>

      {page === 'health' ? <RunHealthView range={range} /> : null}
      {page === 'autotune' ? <AutotuneView /> : null}
      {page !== 'overview' ? null : (<>

      {stats.isPending ? (
        <div className="grid grid-cols-[repeat(auto-fit,minmax(200px,1fr))] gap-3">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-28 rounded-xl" />
          ))}
          <Skeleton className="col-span-full h-72 rounded-xl" />
        </div>
      ) : stats.isError ? (
        <Notice>
          Insights could not be loaded. If the CLI is older than this viewer, update it with{' '}
          <code className="font-mono">uvx agentplay@latest</code>.
        </Notice>
      ) : !rows.length ? (
        <Notice>No sessions match this window and these filters.</Notice>
      ) : (
        <>
          <KpiTiles rows={rows} />
          <div className="flex flex-wrap gap-3">
            <OutcomeMix rows={rows} />
            <WastePanel rows={rows} />
          </div>
          <div className="flex flex-wrap gap-3">
            <SpendPanel rows={rows} />
            <CompactionPanel rows={rows} />
          </div>
          <div className="flex flex-wrap gap-3">
            <FailingCommands rows={rows} />
            <WhatFails rows={rows} />
          </div>
          <SearchHeatmap rows={rows} />
        </>
      )}
      </>)}
    </main>
  );
}

function Notice({ children }: { children: React.ReactNode }) {
  return (
    <div
      role="note"
      className="rounded-xl border border-ds-border-subtlest bg-ds-surface-white p-6 text-center ds-text-body-sm-medium text-ds-text-default"
    >
      {children}
    </div>
  );
}
