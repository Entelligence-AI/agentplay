import { useMemo, type ReactNode } from 'react';
import { parseAsStringEnum, useQueryState } from 'nuqs';
import type { SessionMap, SessionMapEvent } from '@/core/schema';
import { X } from '@phosphor-icons/react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import {
  CAT_LABEL,
  ERROR_SOURCES,
  ERROR_SOURCE_LABEL,
  agentLabel,
  catLabel,
  clockAt,
  countersAt,
  describeEvent,
  errClassLabel,
  errClassOf,
  errSource,
  fileCoverage,
  fmtDur,
  fmtTok,
  fmtUsd,
  isOutputEvent,
  type ErrorSource,
  type ReviewMetrics,
  type ToolCategory,
} from '@/core/sessionMapModel';
import {
  detailRows,
  signalsAt,
  type DetailRow,
  type SessionSignals,
} from '@/core/sessionSignals';
import {
  CATEGORY_COLOR,
  ERROR_COLOR,
  ERROR_SOURCE_COLOR,
  PANEL_BAR_COLOR,
  catColor,
  kindMeta,
  kindStyle,
} from '@/core/eventColors';
import {
  summarizeEvents,
  taskNoun,
  type ModelUsage,
  type TaskSummary,
  type TaskWindow,
} from '@/core/sessionScope';
import { promptImageTurns, type PromptImageTurn } from '@/core/promptImages';
import { HookDetail } from './HookDetail';
import { PromptImages } from './PromptImages';
import { ReviewLoopBlock } from './ReviewLoopBlock';
import {
  CATEGORIES,
  CATEGORY_LABEL,
  DEFAULT_CATEGORIES,
  categoryMatches,
  costLabel,
  detailsTitle,
  lastPicked,
  matchesAnyCategory,
  narrowsView,
  outcomeParts,
  promptLine,
  type Category,
} from '@/core/sessionLayout';

// Signals live in the sidebar's Insights tab (`InsightsPanel`);
// the right column keeps the inspector (Details), Event and Activity.
const PANEL_TABS = ['task', 'event', 'activity', 'review'] as const;
type PanelTab = (typeof PANEL_TABS)[number];
const PANEL_TAB_LABEL: Record<PanelTab, string> = {
  task: 'Details',
  event: 'Event',
  activity: 'Activity',
  review: 'Review',
};
/** Review has a tab of its own because the block in Details sits under the
 * Produced list, and a session with a long list pushes it past the fold. */
const ACCENT_TAB: PanelTab = 'review';

const FEED_LEN = 14;
/** `cat:<ToolCategory>`, one filter value per category:
 * clicking a row in the CategoriesCard sets it. */
const CAT_KEYS = Object.keys(CAT_LABEL) as ToolCategory[];
const CAT_FILTER_VALUES = CAT_KEYS.map((c) => `cat:${c}` as const);
const FEED_FILTER_VALUES = ['all', ...CAT_FILTER_VALUES] as const;
type FeedFilter = (typeof FEED_FILTER_VALUES)[number];
const isCatFilter = (f: FeedFilter): f is `cat:${ToolCategory}` =>
  f.startsWith('cat:');
/** What Recent activity lists: the dock chips' categories when they narrow
 * the view (the default Prompts + Errors, or every chip, lists everything),
 * further narrowed to a picked tool category. */
const feedWant =
  (filter: FeedFilter, selected: ReadonlySet<Category>) =>
  (e: SessionMapEvent): boolean =>
    (!narrowsView(selected) || matchesAnyCategory(e, selected)) &&
    (!isCatFilter(filter) || e.cat === filter.slice(4));

interface Props {
  map: SessionMap;
  /** the unfiltered map: pasted-image turns are numbered over the whole
   * session, which a scoped `map` no longer has */
  fullMap?: SessionMap;
  p: number;
  ordinals: Map<string, number>;
  cumCost: Float64Array;
  review: ReviewMetrics;
  signals: SessionSignals;
  onSeek: (index: number) => void;
  onJumpSource: (source: ErrorSource) => void;
  /** Task windows of the session, and the selected one. Their
   * offsets are from `sessionStart` (the full session), not from the scoped
   * map's rebased start. */
  tasks?: TaskWindow[];
  sessionStart?: string;
  task?: number | null;
  onSelectTask?: (n: number) => void;
  /** Per-model usage of the current scope, and the selected model */
  models?: ModelUsage[];
  model?: string | null;
  onSelectModel?: (model: string) => void;
  /** replaces the task/session detail at the top of Details (the open
   * subagent's card) */
  inspector?: ReactNode;
  /** a model filter is narrowing the map (no task picked) */
  scoped?: boolean;
  /** the dock's category chips (Recent activity follows them) */
  selected?: ReadonlySet<Category>;
  /** the chips' click order (`useEventCategories`), for "the last chip" */
  picks?: readonly Category[];
  /** the review comment whose causal path the map is lighting */
  reviewPath?: readonly number[] | null;
  onReviewPath?: (seqs: readonly number[] | null) => void;
}

/** No chip selection passed: Recent activity lists everything. */
const DEFAULT_SELECTION: ReadonlySet<Category> = new Set(DEFAULT_CATEGORIES);
const NO_PICKS: readonly Category[] = [];

/**
 * The right-hand column next to the map: Details (the inspector for the
 * selected task / open subagent / whole session, and what the scope
 * produced), Event (the event at the playhead) and Activity (recent events).
 * The Signals cards live in the sidebar's Insights tab (`InsightsPanel`).
 */
export function SidePanels({
  map,
  p,
  ordinals,
  cumCost,
  onSeek,
  fullMap,
  tasks = [],
  sessionStart,
  task = null,
  onSelectTask,
  inspector,
  scoped,
  selected = DEFAULT_SELECTION,
  picks = NO_PICKS,
  reviewPath = null,
  onReviewPath,
}: Omit<
  Props,
  'review' | 'signals' | 'onJumpSource' | 'models' | 'model' | 'onSelectModel'
>) {
  const [tab, setTab] = useQueryState(
    'panel',
    parseAsStringEnum<PanelTab>([...PANEL_TABS]).withDefault('task')
  );
  return (
    <aside className="flex h-full min-h-0 min-w-0 flex-col gap-3">
      <div
        role="tablist"
        aria-label="Session details"
        className="flex shrink-0 gap-0.5 rounded-lg border border-ds-border-subtlest bg-ds-surface-white p-[3px]"
      >
        {PANEL_TABS.map((t) => (
          <button
            key={t}
            type="button"
            role="tab"
            aria-selected={tab === t}
            onClick={() => void setTab(t)}
            className={cn(
              'flex-1 rounded-md px-2 py-1 ds-text-body-xs-medium transition-colors',
              tab === t
                ? 'bg-ds-brand-lightest text-ds-brand-default'
                : t === ACCENT_TAB
                  ? // Unselected but not quiet: the one tab here that is an
                    // offer rather than a view of the session. `motion-safe`
                    // because a loop that never ends is exactly what someone
                    // who asked for less motion was asking to be spared.
                    'text-ds-brand-default ring-1 ring-inset ring-ds-brand-subtler hover:bg-ds-brand-lightest motion-safe:animate-pulse-glow'
                  : 'text-ds-text-subtle hover:text-ds-text-default'
            )}
          >
            {PANEL_TAB_LABEL[t]}
          </button>
        ))}
      </div>
      {tab === 'task' ? (
        <div className="flex min-h-0 flex-1 flex-col gap-3">
          <TaskCard
            map={map}
            fullMap={fullMap}
            sessionStart={sessionStart ?? map.session.startedAt}
            tasks={tasks}
            task={task}
            onSelectTask={onSelectTask}
            onSeek={onSeek}
            inspector={inspector}
            scoped={scoped}
            reviewPath={reviewPath}
            onReviewPath={onReviewPath}
          />
          <UniqueEntitiesCard map={map} selected={selected} picks={picks} />
        </div>
      ) : tab === 'review' ? (
        <div className="min-h-0 flex-1 overflow-y-auto">
          <ReviewLoopBlock
            map={map}
            src="review-tab"
            path={reviewPath}
            onPath={onReviewPath}
            onSeek={onSeek}
          />
        </div>
      ) : (
        // Event / Activity scroll inside the column, never the page.
        <div className="min-h-0 flex-1 overflow-y-auto">
          {tab === 'event' ? (
            <EventCard
              map={map}
              p={p}
              ordinals={ordinals}
              cumCost={cumCost}
              sessionId={(fullMap ?? map).session.id}
              onSeek={onSeek}
            />
          ) : (
            <FeedCard
              map={map}
              p={p}
              ordinals={ordinals}
              onSeek={onSeek}
              selected={selected}
            />
          )}
        </div>
      )}
    </aside>
  );
}

/**
 * The sidebar's Insights tab: counters at the playhead, signals, tool
 * categories, agents, models and review (the old Signals tab, moved).
 */
export function InsightsPanel({
  map,
  p,
  ordinals,
  review,
  signals,
  onJumpSource,
  models = [],
  model = null,
  onSelectModel,
}: Pick<
  Props,
  | 'map'
  | 'p'
  | 'ordinals'
  | 'review'
  | 'signals'
  | 'onJumpSource'
  | 'models'
  | 'model'
  | 'onSelectModel'
>) {
  const at = useMemo(() => signalsAt(map.events, p), [map.events, p]);
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <CountersCard map={map} p={p} ctxNow={at.ctxNow} />
      <SignalsCard at={at} session={signals} onJumpSource={onJumpSource} />
      <CategoriesCard map={map} />
      <AgentsCard map={map} ordinals={ordinals} />
      <ModelsCard models={models} model={model} onSelect={onSelectModel} />
      <ReviewCard review={review} signals={signals} />
    </div>
  );
}

function SideCard({
  title,
  aside,
  children,
  collapsible = false,
  defaultOpen = true,
  className,
}: {
  title: string;
  aside?: ReactNode;
  children: ReactNode;
  /** render as a <details> section that can be folded away */
  collapsible?: boolean;
  defaultOpen?: boolean;
  className?: string;
}) {
  if (collapsible) {
    return (
      <details
        open={defaultOpen}
        className="group rounded-xl border border-ds-border-subtlest bg-ds-surface-white p-3"
      >
        <summary className="flex cursor-pointer list-none items-center justify-between gap-2 ds-text-body-xs-semibold text-ds-text-subtle group-open:mb-2 [&::-webkit-details-marker]:hidden">
          <span>
            <span
              aria-hidden
              className="mr-1 inline-block group-open:rotate-90"
            >
              ▸
            </span>
            {title}
          </span>
          {aside}
        </summary>
        {children}
      </details>
    );
  }
  return (
    <section
      className={cn(
        'rounded-xl border border-ds-border-subtlest bg-ds-surface-white p-3',
        className
      )}
    >
      <div className="mb-2 flex shrink-0 items-center justify-between gap-2 ds-text-body-xs-semibold text-ds-text-subtle">
        <span>{title}</span>
        {aside}
      </div>
      {children}
    </section>
  );
}

/** Details tab: the inspector (the selected task's detail, an open
 * subagent's card, or the whole session) and what the current scope
 * produced. The task list itself lives in the sidebar's Tasks tab. Fills
 * the right column; the Produced list scrolls inside the card. */
function TaskCard({
  map,
  fullMap,
  sessionStart,
  tasks,
  task,
  onSelectTask,
  onSeek,
  inspector,
  scoped,
  reviewPath,
  onReviewPath,
}: {
  map: SessionMap;
  fullMap?: SessionMap;
  sessionStart: string;
  tasks: TaskWindow[];
  task: number | null;
  onSelectTask?: (n: number) => void;
  onSeek: (i: number) => void;
  inspector?: ReactNode;
  scoped?: boolean;
  reviewPath?: readonly number[] | null;
  onReviewPath?: (seqs: readonly number[] | null) => void;
}) {
  const summary = useMemo(
    () =>
      summarizeEvents(
        map.events,
        map.events.map((e) => e.model ?? '')
      ),
    [map.events]
  );
  const selected = task != null ? tasks.find((t) => t.n === task) : undefined;
  const noun = taskNoun(tasks);
  const imageTurns = useMemo(
    () => (selected ? promptImageTurns(fullMap ?? map, selected.n) : []),
    [fullMap, map, selected]
  );
  let detail: ReactNode;
  if (inspector) detail = inspector;
  else if (selected)
    detail = (
      <TaskDetail
        task={selected}
        summary={summary}
        sessionStart={sessionStart}
        onSelectTask={onSelectTask}
        sessionId={(fullMap ?? map).session.id}
        imageTurns={imageTurns}
        noun={noun}
      />
    );
  else
    detail = (
      <SessionSummary
        map={map}
        summary={summary}
        sessionStart={sessionStart}
        taskCount={tasks.length}
        noun={noun}
      />
    );
  return (
    <SideCard
      title={detailsTitle({
        inspector: !!inspector,
        selected,
        taskCount: tasks.length,
        noun,
        scoped: !!scoped,
      })}
      aside={selected && !inspector ? <span>{selected.source}</span> : null}
      className="flex min-h-0 flex-1 flex-col"
    >
      {/* One scroll region, not several sections competing for a fixed
       * height, which on a short window would clip every one of them. */}
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
        {detail}
        <OutcomeLine map={map} />
        <ProducedList
          map={map}
          onSeek={onSeek}
          reviewPath={reviewPath}
          onReviewPath={onReviewPath}
        />
      </div>
    </SideCard>
  );
}

/** Categories with a natural per-name identity worth deduplicating here.
 * Edits/Exploration/Tools are left out on purpose: they're one row per
 * file or per raw call, so a unique list would run to hundreds of rows
 * instead of the small, scannable set this card is for. */
const UNIQUE_CATEGORIES: readonly Category[] = [
  'error',
  'mcp',
  'skill',
  'model',
];

/** The one thing an event in `cat` is named after, for grouping -- null
 * when the event doesn't actually carry that identity (e.g. an MCP-category
 * event with no tool name). */
function entityOf(
  e: SessionMapEvent,
  cat: Category
): { key: string; label: string } | null {
  switch (cat) {
    case 'error': {
      const cls = errClassOf(e);
      return { key: `${errSource(e)}/${cls}`, label: errClassLabel(cls) };
    }
    case 'mcp':
      return e.tool
        ? { key: `${e.server ?? ''}/${e.tool}`, label: e.tool }
        : null;
    case 'skill':
      return e.skill ? { key: e.skill, label: e.skill } : null;
    case 'model':
      return e.model ? { key: e.model, label: e.model } : null;
    default:
      return null;
  }
}

/** Details tab, below the task/session card: the most recently clicked
 * chip's unique names, each with how many times it occurred -- the same
 * identities the round map's leaves dedupe to, as a scannable list instead
 * of a graph. Only one chip, not every chip on at once: the most recently
 * clicked of the eligible ones (`picks` is the click order), so turning on
 * e.g. Tools after MCP keeps showing MCP. */
function UniqueEntitiesCard({
  map,
  selected,
  picks,
}: {
  map: SessionMap;
  selected: ReadonlySet<Category>;
  picks: readonly Category[];
}) {
  const cat = lastPicked(picks, selected, UNIQUE_CATEGORIES);
  const rows = useMemo(() => {
    if (!cat) return [];
    const counts = new Map<string, { label: string; n: number }>();
    for (const e of map.events) {
      if (!categoryMatches(e, cat)) continue;
      const entity = entityOf(e, cat);
      if (!entity) continue;
      const cur = counts.get(entity.key);
      if (cur) cur.n++;
      else counts.set(entity.key, { label: entity.label, n: 1 });
    }
    return [...counts.values()].sort((a, b) => b.n - a.n);
  }, [map.events, cat]);
  if (!cat || !rows.length) return null;
  return (
    <SideCard
      title="Unique activity"
      className="shrink-0"
      aside={
        <span className="flex items-center gap-1.5 normal-case text-ds-text-subtle">
          <span
            aria-hidden
            className="size-2 shrink-0 rounded-full"
            style={{ background: CATEGORY_COLOR[cat] }}
          />
          {CATEGORY_LABEL[cat]} · {rows.length} unique
        </span>
      }
    >
      <ul className="flex flex-col gap-0.5">
        {rows.map((r) => (
          <li
            key={r.label}
            className="flex items-center justify-between gap-2 ds-text-body-xs-medium text-ds-text-default"
          >
            <span className="min-w-0 truncate font-mono">{r.label}</span>
            {r.n > 1 ? (
              <span
                className="shrink-0 font-mono text-[10.5px] tabular-nums"
                style={{ color: CATEGORY_COLOR[cat] }}
              >
                ×{r.n}
              </span>
            ) : null}
          </li>
        ))}
      </ul>
    </SideCard>
  );
}

/** What the scope produced, on one line. Was on the header (moved into
 * Details so the header stays one line and the map gets the vertical
 * space back). */
function OutcomeLine({ map }: { map: SessionMap }) {
  const parts = outcomeParts(map);
  const truncated = map.session.truncated;
  if (!parts.length && !truncated) return null;
  const text = parts.join(' · ');
  // A brand-tinted callout, not a muted footnote: the outcome is the one
  // line a reader scans Details for. It wraps rather than truncating, so
  // every part (tasks, PRs, tickets...) stays readable.
  return (
    <div className="mt-1 shrink-0 rounded-lg border border-ds-brand-subtler bg-ds-brand-lightest px-2.5 py-2">
      {parts.length ? (
        <>
          <p className="font-mono text-[10.5px] font-semibold uppercase tracking-wide text-ds-brand-default">
            Outcome
          </p>
          <p className="mt-0.5 ds-text-body-sm-semibold text-ds-text-darker">
            {text}
          </p>
        </>
      ) : null}
      {truncated ? (
        <p className="mt-1 ds-text-body-xs-medium text-ds-warning-default">
          This session was long, so the map stops at its first 20,000 events.
        </p>
      ) : null}
    </div>
  );
}

function DetailRows({ rows }: { rows: [string, string][] }) {
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 ds-text-body-xs-medium">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-ds-text-subtle">{k}</dt>
          <dd className="text-right font-mono text-ds-text-darker">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

/** The selected task: its name, span and what it cost. */
function TaskDetail({
  task,
  summary,
  sessionStart,
  onSelectTask,
  sessionId,
  imageTurns,
  noun,
}: {
  task: TaskWindow;
  summary: TaskSummary;
  sessionStart: string;
  onSelectTask?: (n: number) => void;
  sessionId: string;
  imageTurns: PromptImageTurn[];
  noun: { many: string };
}) {
  return (
    <div className="flex flex-col gap-2">
      <div className="ds-text-body-xs-semibold text-ds-text-darker">
        {task.name}
      </div>
      <DetailRows
        rows={[
          [
            'Span',
            `${clockAt(sessionStart, task.startT).slice(0, 5)}–${clockAt(sessionStart, task.endT).slice(0, 5)} · ${fmtDur(task.endT - task.startT)}`,
          ],
          ['Prompt', promptLine(summary)],
          ['Requests', String(summary.requests)],
          ['Tool calls', String(summary.calls)],
          ['Cost', fmtUsd(summary.cost)],
          ['Tokens', fmtTok(summary.tokens)],
          ['Agents', String(summary.agents.length)],
          ['Models', summary.models.join(', ') || '-'],
        ]}
      />
      <PromptImages sessionId={sessionId} turns={imageTurns} />
      {onSelectTask ? (
        <button
          type="button"
          onClick={() => onSelectTask(task.n)}
          className="self-start ds-text-body-xs-medium text-ds-text-subtle underline-offset-2 hover:underline"
        >
          Show all {noun.many.toLowerCase()}
        </button>
      ) : null}
    </div>
  );
}

/** No task picked: the whole (possibly filtered) session. */
function SessionSummary({
  map,
  summary,
  sessionStart,
  taskCount,
  noun,
}: {
  map: SessionMap;
  summary: TaskSummary;
  sessionStart: string;
  taskCount: number;
  noun: { one: string; many: string };
}) {
  // Distinct files by their strongest touch (the old header coverage row).
  const files = useMemo(() => fileCoverage(map), [map]);
  return (
    <div className="flex flex-col gap-2">
      <DetailRows
        rows={[
          [
            'Span',
            `${clockAt(sessionStart, 0).slice(0, 5)}–${clockAt(sessionStart, map.session.durationMs).slice(0, 5)} · ${fmtDur(map.session.durationMs)}`,
          ],
          [noun.many, String(taskCount)],
          ['Requests', String(summary.requests)],
          ['Tool calls', String(summary.calls)],
          ['Cost', costLabel(summary.cost)],
          ['Tokens', fmtTok(summary.tokens)],
          ['Agents', String(summary.agents.length)],
          [
            'Files',
            `${files.edited} edited · ${files.read} read · ${files.seen} seen`,
          ],
        ]}
      />
      {taskCount > 1 ? (
        <p className="ds-text-body-xs-medium text-ds-text-subtle">
          Pick a {noun.one.toLowerCase()} in the sidebar or on the timeline to
          narrow the map and timeline. Hover any node or mark for details.
        </p>
      ) : null}
    </div>
  );
}

/** What the current scope produced, in the Details card's scroll region. */
function ProducedList({
  map,
  onSeek,
  reviewPath = null,
  onReviewPath,
}: {
  map: SessionMap;
  onSeek: (i: number) => void;
  reviewPath?: readonly number[] | null;
  onReviewPath?: (seqs: readonly number[] | null) => void;
}) {
  const produced = useMemo(
    () =>
      map.events.map((e, i) => ({ e, i })).filter(({ e }) => isOutputEvent(e)),
    [map.events]
  );
  return (
    <>
      <div className="mb-1 mt-3 flex shrink-0 items-center justify-between ds-text-body-xs-semibold text-ds-text-subtle">
        <span>Produced</span>
        <span>{produced.length}</span>
      </div>
      {produced.length ? (
        <ul className="flex shrink-0 flex-col gap-0.5">
          {produced.map(({ e, i }) => (
            <ProducedRow key={i} map={map} e={e} onSeek={() => onSeek(i)} />
          ))}
        </ul>
      ) : (
        <p className="ds-text-body-xs-medium text-ds-text-subtle">
          Nothing produced in this scope.
        </p>
      )}
      {/* Below the list, never over it: what the session produced is a local
       * answer and stays one, whether or not it found anything. */}
      <ReviewLoopBlock
        map={map}
        path={reviewPath}
        onPath={onReviewPath}
        onSeek={onSeek}
      />
    </>
  );
}

function ProducedRow({
  map,
  e,
  onSeek,
}: {
  map: SessionMap;
  e: SessionMapEvent;
  onSeek: () => void;
}) {
  const K = kindMeta(e.vcs?.action === 'pr_create' ? 'pr_create' : e.kind);
  const D = describeEvent(map, e);
  return (
    <li>
      <button
        type="button"
        onClick={onSeek}
        className="flex w-full items-center gap-2 rounded px-1 py-0.5 text-left ds-text-body-xs-medium hover:bg-ds-gray-subtlest"
      >
        <span
          aria-hidden
          className="size-2 shrink-0 rotate-45 rounded-[1px]"
          style={{ background: K.color }}
        />
        <span className="min-w-0 flex-1 truncate text-ds-text-default">
          {D.title}
        </span>
        <span className="shrink-0 font-mono text-ds-text-subtle">
          {clockAt(map.session.startedAt, e.t).slice(0, 5)}
        </span>
      </button>
    </li>
  );
}

/** Per-model cost share; clicking a model scopes the explorer to it. */
function ModelsCard({
  models,
  model,
  onSelect,
}: {
  models: ModelUsage[];
  model: string | null;
  onSelect?: (model: string) => void;
}) {
  if (!models.length) return null;
  const total = models.reduce((s, m) => s + m.cost, 0) || 1;
  return (
    <SideCard
      title="Models"
      aside={<span>cost share</span>}
      collapsible
      defaultOpen={false}
    >
      <div className="flex flex-col gap-2">
        {models.map((m) => {
          const share = Math.max(0, Math.min(1, m.cost / total));
          return (
            <button
              key={m.model}
              type="button"
              aria-pressed={model === m.model}
              onClick={() => onSelect?.(m.model)}
              className={cn(
                'flex flex-col gap-1 rounded px-1 py-0.5 text-left',
                model === m.model && 'bg-ds-brand-lightest'
              )}
            >
              <span className="flex w-full items-baseline justify-between gap-2 ds-text-body-xs-medium">
                <span className="min-w-0 truncate text-ds-text-darker">
                  {m.model}
                </span>
                <span className="shrink-0 font-mono text-ds-text-subtle tabular-nums">
                  {fmtUsd(m.cost)} · {m.requests} req
                </span>
              </span>
              <span className="block h-1.5 w-full overflow-hidden rounded-full bg-ds-gray-subtlest">
                <span
                  className="block h-full rounded-full"
                  style={{
                    width: `${share * 100}%`,
                    background: PANEL_BAR_COLOR,
                  }}
                />
              </span>
            </button>
          );
        })}
      </div>
    </SideCard>
  );
}

const toneClass = (tone?: 'bad' | 'warn') =>
  tone === 'bad'
    ? 'text-ds-danger-default'
    : tone === 'warn'
      ? 'text-ds-warning-default'
      : 'text-ds-text-darker';

function CountersCard({
  map,
  p,
  ctxNow,
}: {
  map: SessionMap;
  p: number;
  ctxNow: number;
}) {
  const c = useMemo(() => countersAt(map.events, p), [map.events, p]);
  const pct = map.events.length
    ? Math.round((100 * (p + 1)) / map.events.length)
    : 0;
  const items: { v: string | number; l: string; tone?: 'bad' }[] = [
    { v: c.calls, l: 'tool calls' },
    { v: c.requests, l: 'LLM requests' },
    { v: fmtTok(c.tokens), l: 'tokens' },
    { v: fmtUsd(c.cost), l: 'est. cost' },
    { v: c.filesRead, l: 'files read' },
    { v: c.filesEdited, l: 'files edited' },
    { v: c.errors, l: 'error signals', tone: c.errors ? 'bad' : undefined },
    { v: fmtTok(ctxNow), l: 'context now' },
  ];
  return (
    <SideCard
      title="At playhead"
      aside={<span className="font-mono tabular-nums">{pct}%</span>}
      collapsible
    >
      <div className="grid grid-cols-4 gap-x-2 gap-y-2.5">
        {items.map((it) => (
          <div key={it.l} className="flex min-w-0 flex-col">
            <b
              className={cn(
                'truncate font-mono ds-text-body-xs-semibold tabular-nums',
                toneClass(it.tone)
              )}
            >
              {it.v}
            </b>
            <span className="truncate ds-text-body-xs-medium text-ds-text-subtle">
              {it.l}
            </span>
          </div>
        ))}
      </div>
    </SideCard>
  );
}

function SignalsCard({
  at,
  session,
  onJumpSource,
}: {
  at: ReturnType<typeof signalsAt>;
  session: SessionSignals;
  onJumpSource: (source: ErrorSource) => void;
}) {
  const classes = Object.entries(at.byClass)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 6);
  const peak = Math.max(session.maxContext, 1);
  const ctxPct = Math.min(100, (100 * at.ctxNow) / peak);
  return (
    <SideCard
      title="Signals"
      aside={<span>at playhead / session</span>}
      collapsible
    >
      <div className="flex flex-col">
        {ERROR_SOURCES.map((src) => (
          <button
            key={src}
            type="button"
            onClick={() => onJumpSource(src)}
            disabled={!session.bySource[src]}
            aria-label={`Jump to the next ${ERROR_SOURCE_LABEL[src]} error`}
            className="flex items-center gap-2 rounded-md px-1.5 py-1 text-left transition-colors hover:bg-ds-gray-subtlest disabled:cursor-default disabled:hover:bg-transparent"
          >
            <span
              aria-hidden
              className="size-2 shrink-0 rounded-full"
              style={{ background: ERROR_SOURCE_COLOR[src] }}
            />
            <span className="min-w-0 flex-1 truncate ds-text-body-xs-medium text-ds-text-default">
              {ERROR_SOURCE_LABEL[src]}
            </span>
            <span className="shrink-0 font-mono ds-text-body-xs-medium tabular-nums">
              <b
                className={
                  at.bySource[src]
                    ? 'text-ds-danger-default'
                    : 'text-ds-text-darker'
                }
              >
                {at.bySource[src]}
              </b>
              <span className="text-ds-text-subtle">
                {' '}
                / {session.bySource[src]}
              </span>
            </span>
          </button>
        ))}
      </div>
      <div className="mt-2 flex flex-wrap gap-1">
        {classes.length ? (
          classes.map(([k, v]) => (
            <Badge key={k} variant="error">
              {errClassLabel(k)} ×{v}
            </Badge>
          ))
        ) : (
          <span className="ds-text-body-xs-medium text-ds-text-subtle">
            No errors yet at the playhead
          </span>
        )}
      </div>
      <dl className="mt-2 grid grid-cols-[1fr_auto] gap-x-3 gap-y-0.5 ds-text-body-xs-medium">
        <dt className="text-ds-text-subtle">Retries</dt>
        <dd className="text-right font-mono text-ds-text-darker">
          {at.retries}
        </dd>
        <dt className="text-ds-text-subtle">Delays</dt>
        <dd className="text-right font-mono text-ds-text-darker">
          {at.delays} · {fmtDur(at.delayMs)}
        </dd>
        <dt className="text-ds-text-subtle">Compactions</dt>
        <dd className="text-right font-mono text-ds-text-darker">
          {at.compactions}
        </dd>
      </dl>
      <div className="mt-2 flex items-center justify-between ds-text-body-xs-medium text-ds-text-subtle">
        <span>Context window</span>
        <span className="font-mono">
          {fmtTok(at.ctxNow)} / peak {fmtTok(session.maxContext)}
        </span>
      </div>
      <div
        className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-ds-gray-subtlest"
        role="meter"
        aria-label="Context now against the session peak"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(ctxPct)}
      >
        <div
          className="h-full rounded-full"
          style={{ width: `${ctxPct}%`, background: PANEL_BAR_COLOR }}
        />
      </div>
    </SideCard>
  );
}

function Rows({
  rows,
  onSeek,
}: {
  rows: DetailRow[];
  onSeek?: (index: number) => void;
}) {
  return (
    <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 ds-text-body-xs-medium">
      {rows.map((r) => (
        <div key={r.k} className="contents">
          <dt className="text-ds-text-subtle">{r.k}</dt>
          <dd
            className={cn('truncate text-right font-mono', toneClass(r.tone))}
          >
            {r.seek != null && onSeek ? (
              <button
                type="button"
                onClick={() => onSeek(r.seek!)}
                className="max-w-full cursor-pointer truncate rounded text-right underline-offset-2 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ds-brand-subtle"
              >
                {r.v}
              </button>
            ) : (
              r.v
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function EventCard({
  map,
  p,
  ordinals,
  cumCost,
  sessionId,
  onSeek,
}: {
  map: SessionMap;
  p: number;
  ordinals: Map<string, number>;
  cumCost: Float64Array;
  sessionId: string;
  onSeek: (index: number) => void;
}) {
  const e = map.events[p];
  if (!e) return null;
  const K = kindStyle(e);
  const D = describeEvent(map, e, ordinals);
  const failedCall = e.err && e.kind !== 'error';
  return (
    <SideCard
      title={`Event #${p}`}
      aside={
        <span className="font-mono">
          {clockAt(map.session.startedAt, e.t)} UTC
        </span>
      }
      // One height for every event, sized to fit an LLM request (the longest
      // common kind), so stepping through events never resizes the card.
      // Anything longer scrolls inside it.
      className="flex h-[22rem] flex-col"
    >
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div
          className={cn(
            'flex items-center gap-1.5 ds-text-body-xs-semibold',
            failedCall ? 'text-ds-danger-default' : 'text-ds-text-default'
          )}
        >
          <span
            aria-hidden
            className="size-2 shrink-0 rounded-full"
            style={{ background: K.color }}
          />
          {K.label}
          {failedCall ? ' · failed' : ''}
        </div>
        <div className="mt-1 break-all ds-text-body-xs-semibold text-ds-text-darker">
          {D.title}
        </div>
        {D.detail ? (
          <div className="mt-0.5 break-all ds-text-body-xs-medium text-ds-text-subtle">
            {D.detail}
          </div>
        ) : null}
        <Rows
          rows={detailRows(map, p, cumCost, ordinals, false)}
          onSeek={onSeek}
        />
        {e.source === 'hook' && e.srcUuid ? (
          <HookDetail sessionId={sessionId} uuid={e.srcUuid} />
        ) : null}
      </div>
    </SideCard>
  );
}

function FeedCard({
  map,
  p,
  ordinals,
  onSeek,
  selected,
}: {
  map: SessionMap;
  p: number;
  ordinals: Map<string, number>;
  onSeek: (i: number) => void;
  selected: ReadonlySet<Category>;
}) {
  // `feed` now only carries a tool category picked in the Insights tab's
  // Categories card; older `feed=tools|llm|…` values read as no filter.
  const [filter, setFilter] = useQueryState(
    'feed',
    parseAsStringEnum<FeedFilter>([...FEED_FILTER_VALUES]).withDefault('all')
  );
  const rows = useMemo(() => {
    const want = feedWant(filter, selected);
    const out: number[] = [];
    for (let i = p; i >= 0 && out.length < FEED_LEN; i--) {
      if (want(map.events[i])) out.push(i);
    }
    return out;
  }, [map.events, p, filter, selected]);
  return (
    <SideCard
      title="Recent activity"
      aside={
        isCatFilter(filter) ? (
          <Button
            variant="outline"
            size="sm"
            onClick={() => void setFilter('all')}
            title="Clear the tool-category filter"
            className="h-6 gap-1 px-2"
          >
            {catLabel(filter.slice(4))}
            <X size={10} aria-hidden />
          </Button>
        ) : null
      }
    >
      <p className="mb-2 ds-text-body-xs-medium text-ds-text-subtle">
        {narrowsView(selected)
          ? `${CATEGORIES.filter((c) => selected.has(c))
              .map((c) => CATEGORY_LABEL[c])
              .join(', ')} · from the timeline chips`
          : 'Everything · pick timeline chips to narrow'}
      </p>
      {rows.length ? (
        <ul className="flex flex-col">
          {rows.map((i) => {
            const e = map.events[i];
            const K = kindStyle(e);
            const D = describeEvent(map, e, ordinals);
            return (
              <li key={i}>
                <button
                  type="button"
                  onClick={() => onSeek(i)}
                  className={cn(
                    'flex w-full items-center gap-2 rounded-md px-1.5 py-1 text-left transition-colors hover:bg-ds-gray-subtlest',
                    i === p && 'bg-ds-brand-lightest'
                  )}
                >
                  <span
                    aria-hidden
                    className="size-2 shrink-0 rounded-full"
                    style={{ background: e.err ? ERROR_COLOR : K.color }}
                  />
                  <span className="min-w-0 flex-1 truncate ds-text-body-xs-medium text-ds-text-default">
                    {D.title}
                    {e.err && e.kind !== 'error' ? (
                      <span className="ml-1 text-ds-danger-default">
                        failed
                      </span>
                    ) : null}
                    {e.retryOf != null ? (
                      <span className="ml-1 text-ds-warning-default">
                        retry
                      </span>
                    ) : null}
                  </span>
                  <span className="shrink-0 font-mono ds-text-body-xs-medium text-ds-text-subtler">
                    {clockAt(map.session.startedAt, e.t).slice(0, 5)}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      ) : (
        <div className="px-1 ds-text-body-xs-medium text-ds-text-subtle">
          Nothing of this kind yet
        </div>
      )}
    </SideCard>
  );
}

/** Tool-category breakdown: counts per
 * `events[].cat`, click a row to filter Recent activity to that category
 * (the `feed` query state FeedCard narrows by). Renders
 * nothing for a map with no categorized events (older maps, pre-contract). */
function CategoriesCard({ map }: { map: SessionMap }) {
  const [filter, setFilter] = useQueryState(
    'feed',
    parseAsStringEnum<FeedFilter>([...FEED_FILTER_VALUES]).withDefault('all')
  );
  const entries = useMemo(() => {
    const counts = new Map<string, number>();
    for (const e of map.events) {
      if (!e.cat) continue;
      counts.set(e.cat, (counts.get(e.cat) ?? 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [map.events]);
  if (!entries.length) return null;
  const max = entries[0][1];
  return (
    <SideCard title="Tool categories" collapsible defaultOpen={false}>
      <ul className="flex flex-col gap-0.5">
        {entries.map(([cat, n]) => {
          const value = `cat:${cat}` as FeedFilter;
          const active = filter === value;
          const color = catColor(cat);
          return (
            <li key={cat}>
              <button
                type="button"
                aria-pressed={active}
                onClick={() => void setFilter(active ? 'all' : value)}
                className={cn(
                  'grid w-full grid-cols-[104px_1fr_auto] items-center gap-2 rounded-md px-1.5 py-1 text-left transition-colors hover:bg-ds-gray-subtlest',
                  active && 'bg-ds-brand-lightest'
                )}
              >
                <span className="flex min-w-0 items-center gap-1.5 truncate ds-text-body-xs-medium text-ds-text-default">
                  <span
                    aria-hidden
                    className="size-2 shrink-0 rounded-full"
                    style={{ background: color }}
                  />
                  {catLabel(cat)}
                </span>
                <span className="h-1.5 overflow-hidden rounded-full bg-ds-gray-subtlest">
                  <span
                    className="block h-full rounded-full"
                    style={{ width: `${(100 * n) / max}%`, background: color }}
                  />
                </span>
                <span className="ds-text-body-xs-medium tabular-nums text-ds-text-subtle">
                  {n}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </SideCard>
  );
}

function AgentsCard({
  map,
  ordinals,
}: {
  map: SessionMap;
  ordinals: Map<string, number>;
}) {
  const total = map.agents.reduce((s, a) => s + a.cost, 0) || 1;
  if (!map.agents.length) return null;
  return (
    <SideCard
      title="Agents"
      aside={<span>cost share</span>}
      collapsible
      defaultOpen={false}
    >
      <div className="flex flex-col gap-2">
        {map.agents.map((a) => {
          const share = Math.max(0, Math.min(1, a.cost / total));
          const label = agentLabel(map, a.id, ordinals);
          return (
            <div key={a.id} className="flex flex-col gap-1">
              <div className="flex items-baseline justify-between gap-2 ds-text-body-xs-medium">
                <span
                  className={cn(
                    'min-w-0 truncate',
                    a.id === 'main'
                      ? 'text-ds-text-darker'
                      : 'text-ds-text-default'
                  )}
                  title={label}
                >
                  {label}
                </span>
                <span className="shrink-0 font-mono text-ds-text-subtle tabular-nums">
                  {fmtUsd(a.cost)} · {a.calls} calls
                </span>
              </div>
              <div
                className="h-1.5 w-full overflow-hidden rounded-full bg-ds-gray-subtlest"
                role="meter"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.round(share * 100)}
                aria-label={`${label} cost share`}
              >
                <div
                  className="h-full rounded-full"
                  style={{
                    width: `${share * 100}%`,
                    background: PANEL_BAR_COLOR,
                  }}
                />
              </div>
            </div>
          );
        })}
      </div>
    </SideCard>
  );
}

const VERIFICATION_COPY: Record<ReviewMetrics['verification'], string> = {
  never: 'never verified',
  after_final_edit: 'after final edit',
  edits_after_verify: 'edits after last verify',
};

function ReviewCard({
  review,
  signals,
}: {
  review: ReviewMetrics;
  signals: SessionSignals;
}) {
  const bySource =
    ERROR_SOURCES.filter((s) => signals.bySource[s])
      .map((s) => `${ERROR_SOURCE_LABEL[s]} ${signals.bySource[s]}`)
      .join(' · ') || 'none';
  const latency =
    signals.latencyP50 != null && signals.latencyP95 != null
      ? `${fmtDur(signals.latencyP50)} / ${fmtDur(signals.latencyP95)}`
      : 'n/a';
  const rows: {
    label: string;
    value: string;
    tone?: 'bad' | 'warn';
    sub?: string;
  }[] = [
    {
      label: 'Error signals',
      value: String(signals.errors),
      tone: signals.errors ? 'bad' : undefined,
      sub: bySource,
    },
    {
      label: 'Retries after a failure',
      value: String(signals.retries),
      tone: signals.retries ? 'warn' : undefined,
    },
    {
      label: 'Delays (total)',
      value: `${signals.delays} · ${fmtDur(signals.delayMs)}`,
      tone: signals.delays ? 'warn' : undefined,
    },
    { label: 'LLM latency p50 / p95', value: latency },
    {
      label: 'Compactions · peak context',
      value: `${signals.compactions} · ${fmtTok(signals.maxContext)}`,
    },
    {
      label: 'Edits to files never read first',
      value: String(review.blindEdits),
      tone: review.blindEdits ? 'warn' : undefined,
    },
    {
      label: 'Files edited 3+ times',
      value: String(review.churnFiles),
      tone: review.churnFiles ? 'warn' : undefined,
    },
    { label: 'Re-reads of unchanged files', value: `${review.reReadPct}%` },
    {
      label: 'Verification',
      value: VERIFICATION_COPY[review.verification],
      tone: review.verification === 'after_final_edit' ? undefined : 'warn',
    },
  ];
  return (
    <SideCard
      title="Review"
      aside={<span>whole session</span>}
      collapsible
      defaultOpen={false}
    >
      <div className="flex flex-col divide-y divide-ds-border-subtlest">
        {rows.map((r) => (
          <div key={r.label} className="flex flex-col gap-0.5 py-1.5">
            <div className="flex items-center justify-between gap-3 ds-text-body-xs-medium">
              <span className="text-ds-text-subtle">{r.label}</span>
              <b
                className={cn(
                  'shrink-0 font-mono ds-text-body-xs-semibold',
                  toneClass(r.tone)
                )}
              >
                {r.value}
              </b>
            </div>
            {r.sub ? (
              <span className="ds-text-body-xs-medium text-ds-text-subtler">
                {r.sub}
              </span>
            ) : null}
          </div>
        ))}
      </div>
    </SideCard>
  );
}
