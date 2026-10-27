/**
 * The one left sidebar: the sessions list, or (once a session is
 * open) that session's card with Tasks / Subagents / Insights tabs, or (once
 * a subagent is open) the helper's card and what it touched. Collapses to a
 * 40px rail. Also the one-line task/subagent strip above the timeline.
 */
import type { ReactNode } from 'react';
import {
  ArrowLeft,
  CaretDoubleLeft,
  CaretDoubleRight,
  CaretLeft,
  CaretRight,
  ImageSquare,
  X,
} from '@phosphor-icons/react';
import type { SessionExplorerSession } from '@/core/schema';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';
import { agentToolLabel } from './SessionList';
import { clockAt, dayLabel, fmtDur, fmtTok } from '@/core/sessionMapModel';
import {
  taskNoun,
  type TaskSummary,
  type TaskWindow,
} from '@/core/sessionScope';
import {
  costLabel,
  outputsLine,
  spanBarGeometry,
  spanLabel,
  type Category,
  type ErrorGroup,
  type SessionError,
  type SidebarTab,
  type SubagentInfo,
  type TaskErrors,
  type TouchedGroup,
} from '@/core/sessionLayout';
import { TOUCH_COLOR } from '@/core/eventColors';

const SIDEBAR_TAB_LABEL: Record<SidebarTab, string> = {
  errors: 'Errors',
  tasks: 'Tasks',
  subagents: 'Subagents',
  insights: 'Insights',
};

const iconBtn =
  'inline-flex size-7 shrink-0 items-center justify-center rounded-md text-ds-text-subtle transition-colors hover:bg-ds-gray-subtlest hover:text-ds-text-default';

/** The frame every sidebar mode renders in: the explorer's full height, with
 * one region inside it scrolling (never the page). */
export function SidebarFrame({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col gap-3">{children}</div>
  );
}

export function CollapseButton({ onCollapse }: { onCollapse: () => void }) {
  return (
    <button
      type="button"
      onClick={onCollapse}
      aria-label="Collapse sidebar"
      title="Collapse sidebar"
      className={iconBtn}
    >
      <CaretDoubleLeft size={14} />
    </button>
  );
}

/** The collapsed sidebar: a 40px rail with vertical text; click expands. */
export function SidebarRail({
  label,
  onExpand,
}: {
  label: string;
  onExpand: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onExpand}
      aria-label={`Show sidebar (${label})`}
      title="Show sidebar"
      className="flex h-full min-h-[240px] w-10 flex-col items-center gap-3 rounded-xl border border-ds-border-subtlest bg-ds-surface-white py-3 text-ds-text-subtle transition-colors hover:border-ds-border-default hover:text-ds-text-default"
    >
      <CaretDoubleRight size={14} className="shrink-0" />
      <span className="max-h-full overflow-hidden whitespace-nowrap font-mono ds-text-body-xs-medium [writing-mode:vertical-rl]">
        {label}
      </span>
    </button>
  );
}

/** Header row of the sessions list (list mode). */
export function SessionListHeader({
  count,
  onCollapse,
}: {
  count: number;
  onCollapse: () => void;
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="ds-text-body-sm-semibold text-ds-text-darker">
        Sessions
      </span>
      <span className="ds-text-body-xs-medium text-ds-text-subtle tabular-nums">
        {count}
      </span>
      <span className="flex-1" />
      <CollapseButton onCollapse={onCollapse} />
    </div>
  );
}

function BackRow({
  label,
  onBack,
  onCollapse,
}: {
  label: string;
  onBack: () => void;
  onCollapse: () => void;
}) {
  return (
    <div className="flex items-center gap-2">
      <button
        type="button"
        onClick={onBack}
        className="inline-flex items-center gap-1.5 rounded-md py-1 pr-2 ds-text-body-xs-semibold text-ds-text-default hover:text-ds-text-darker"
      >
        <ArrowLeft size={14} />
        {label}
      </button>
      <span className="flex-1" />
      <CollapseButton onCollapse={onCollapse} />
    </div>
  );
}

/** A chip that narrows the timeline when clicked. Falls back to a plain chip
 * when no handler is given, so the chips still render outside the explorer. */
function ActionChip({
  onClick,
  title,
  variant,
  children,
}: {
  onClick?: () => void;
  title: string;
  variant: 'outline' | 'error';
  children: ReactNode;
}) {
  const chip = <Badge variant={variant}>{children}</Badge>;
  if (!onClick) return chip;
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      aria-label={title}
      className="rounded-full outline-none focus-visible:ring-2 focus-visible:ring-ds-brand-subtle"
    >
      {chip}
    </button>
  );
}

/** Chips of a session row: tool, tasks, PRs, subagents, errors.
 *
 * The counts are the quickest read in the sidebar, so the ones that correspond
 * to something on the timeline act as filters for it rather than sitting inert. */
function SessionChips({
  row,
  taskCount,
  noun,
  prCount,
  subCount,
  errors,
  onFocusCategory,
  onShowSubagents,
}: {
  row: SessionExplorerSession | null;
  taskCount: number;
  /** what the task windows are called (`taskNoun`) */
  noun: { many: string };
  prCount: number;
  subCount: number;
  errors: number;
  onFocusCategory?: (c: Category) => void;
  onShowSubagents?: () => void;
}) {
  return (
    <span className="flex flex-wrap gap-1">
      {row ? (
        <Badge variant="secondary">{agentToolLabel(row.agentTool)}</Badge>
      ) : null}
      {taskCount > 1 ? (
        <Badge variant="outline">
          {taskCount} {noun.many.toLowerCase()}
        </Badge>
      ) : null}
      {prCount ? (
        <ActionChip
          variant="outline"
          title="Show outputs on the timeline"
          onClick={onFocusCategory && (() => onFocusCategory('out'))}
        >
          {prCount} PR{prCount > 1 ? 's' : ''}
        </ActionChip>
      ) : null}
      {subCount ? (
        <ActionChip
          variant="outline"
          title="Show the subagents"
          onClick={onShowSubagents}
        >
          {subCount} subagent{subCount > 1 ? 's' : ''}
        </ActionChip>
      ) : null}
      {errors ? (
        <ActionChip
          variant="error"
          title="Show errors on the timeline"
          onClick={onFocusCategory && (() => onFocusCategory('error'))}
        >
          {errors} error{errors > 1 ? 's' : ''}
        </ActionChip>
      ) : null}
    </span>
  );
}

const card =
  'flex flex-col gap-1 rounded-xl border border-ds-border-subtlest bg-ds-surface-white p-3';

export interface SessionSidebarProps {
  row: SessionExplorerSession | null;
  title: string;
  startedAt: string;
  durationMs: number;
  costUsd: number;
  prCount: number;
  errors: number;
  tasks: TaskWindow[];
  taskSums: Map<number, TaskSummary>;
  task: number | null;
  onSelectTask: (n: number | null) => void;
  subs: SubagentInfo[];
  onOpenSub: (id: string) => void;
  tabs: SidebarTab[];
  tab: SidebarTab;
  onTab: (t: SidebarTab) => void;
  insights: ReactNode;
  onShowList: () => void;
  onCollapse: () => void;
  /** Narrow the timeline to one category, for the clickable count chips. */
  onFocusCategory?: (c: Category) => void;
  /** Every failure in the whole session, worst group first. */
  errorGroups?: ErrorGroup[];
  /** Whole-session `seq` under the playhead, to mark the error being replayed. */
  playheadSeq?: number | null;
  /** A row was clicked: the caller seeks, or re-scopes to reach it. */
  onSelectError?: (err: SessionError) => void;
  /** Errors per task, for the tag on each task row. */
  taskErrors?: Map<number, TaskErrors>;
  /** A task's error tag: scope to it, open Errors, land on its first error. */
  onJumpToTaskErrors?: (task: number, seq: number) => void;
}

/** Session mode: "← All sessions", the session card, then the tabs. */
export function SessionSidebar(props: SessionSidebarProps) {
  const {
    row,
    title,
    startedAt,
    durationMs,
    costUsd,
    tasks,
    task,
    onSelectTask,
    subs,
    tabs,
    tab,
    onTab,
  } = props;
  const noun = taskNoun(tasks);
  return (
    <SidebarFrame>
      <BackRow
        label="All sessions"
        onBack={props.onShowList}
        onCollapse={props.onCollapse}
      />
      <div className={card}>
        <span className="line-clamp-3 ds-text-body-sm-semibold text-ds-text-darker">
          {title}
        </span>
        <span className="ds-text-body-xs-medium text-ds-text-subtle">
          {dayLabel(startedAt)} · {clockAt(startedAt, 0).slice(0, 5)} UTC ·{' '}
          {fmtDur(durationMs)} · {costLabel(costUsd)}
        </span>
        <SessionChips
          row={row}
          taskCount={tasks.length}
          noun={noun}
          prCount={props.prCount}
          subCount={subs.length}
          errors={props.errors}
          onFocusCategory={props.onFocusCategory}
          onShowSubagents={
            tabs.includes('subagents') ? () => onTab('subagents') : undefined
          }
        />
      </div>
      <div
        role="tablist"
        aria-label="Session sidebar"
        className="flex min-w-0 gap-0.5 rounded-lg border border-ds-border-subtlest bg-ds-surface-white p-[3px]"
      >
        {tabs.map((t) => (
          <button
            key={t}
            id={`se-sidebar-tab-${t}`}
            type="button"
            role="tab"
            aria-selected={tab === t}
            aria-controls="se-sidebar-panel"
            onClick={() => onTab(t)}
            className={cn(
              // Sized to its label (then sharing the slack), not a fixed quarter:
              // "Subagents 13" is wider than "Insights". `min-w-0` lets a label
              // truncate instead of widening the bar past the sidebar.
              'flex min-w-0 flex-auto items-center justify-center gap-1 whitespace-nowrap rounded-md px-1 py-1 ds-text-body-xs-medium transition-colors',
              tab === t
                ? 'bg-ds-brand-lightest text-ds-brand-default'
                : 'text-ds-text-subtle hover:text-ds-text-default'
            )}
          >
            <span className="truncate">
              {t === 'tasks' ? noun.many : SIDEBAR_TAB_LABEL[t]}
            </span>
            {t === 'tasks' || t === 'subagents' || t === 'errors' ? (
              <span className="shrink-0 font-mono tabular-nums text-ds-text-subtler">
                {t === 'tasks'
                  ? tasks.length
                  : t === 'errors'
                    ? props.errors
                    : subs.length}
              </span>
            ) : null}
          </button>
        ))}
      </div>
      <div
        id="se-sidebar-panel"
        role="tabpanel"
        aria-labelledby={`se-sidebar-tab-${tab}`}
        className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto"
      >
        {tab === 'tasks' ? (
          <TaskList
            tasks={tasks}
            sums={props.taskSums}
            task={task}
            startedAt={startedAt}
            durationMs={durationMs}
            onSelect={onSelectTask}
            errors={props.taskErrors}
            onJumpToErrors={props.onJumpToTaskErrors}
          />
        ) : null}
        {tab === 'errors' ? (
          <ErrorList
            groups={props.errorGroups ?? []}
            noun={noun}
            startedAt={startedAt}
            playheadSeq={props.playheadSeq ?? null}
            onSelectError={props.onSelectError}
          />
        ) : null}
        {tab === 'subagents' ? (
          <SubagentList
            subs={subs}
            tasks={tasks}
            startedAt={startedAt}
            durationMs={durationMs}
            onOpen={props.onOpenSub}
          />
        ) : null}
        {tab === 'insights' ? props.insights : null}
      </div>
    </SidebarFrame>
  );
}

const rowBtn = (active: boolean) =>
  cn(
    'flex w-full items-start gap-2 rounded-lg border p-2 text-left transition-colors',
    active
      ? 'border-ds-brand-subtler bg-ds-brand-lightest'
      : 'border-transparent hover:bg-ds-gray-subtlest'
  );

/** Every failure in the session, grouped by class, worst group first.
 *
 * The dock could already step between errors and the Activity feed listed the
 * ones behind the playhead, but nothing showed them all, and a row that reads
 * "outside-repo failed" is not enough to decide anything. These rows carry the
 * command, the class and the exit code the event already recorded, and a run
 * marker where the same command failed repeatedly — one failure is noise, four
 * in a row is a harness problem. */
function ErrorList({
  groups,
  noun,
  startedAt,
  playheadSeq,
  onSelectError,
}: {
  groups: ErrorGroup[];
  noun: { one: string };
  startedAt: string;
  playheadSeq: number | null;
  onSelectError?: (err: SessionError) => void;
}) {
  if (!groups.length)
    return (
      <p className="p-2 ds-text-body-xs-medium text-ds-text-subtle">
        No errors in this session.
      </p>
    );
  return (
    <>
      {groups.map((g) => (
        <section key={g.errClass} className="flex flex-col gap-1">
          <header className="flex items-baseline gap-1.5 px-2 pt-1.5">
            <span className="ds-text-body-xs-semibold text-ds-text-darker">
              {g.label}
            </span>
            <span className="font-mono tabular-nums ds-text-body-xs-medium text-ds-text-subtler">
              {g.errors.length}
            </span>
            {g.provider ? (
              <span className="ds-text-body-xs-medium text-ds-text-subtler">
                provider-side
              </span>
            ) : null}
            {g.worstRun > 2 && g.worstRunLabel ? (
              <Badge variant="error">
                {g.worstRun}× {g.worstRunLabel} in a row
              </Badge>
            ) : null}
          </header>
          {g.errors.map((e) => (
            <button
              key={`${e.index}`}
              type="button"
              aria-label={`${g.label}: ${e.label} at event ${e.seq}`}
              title={`${e.label} · ${g.label}${e.exitCode != null ? ` · exit ${e.exitCode}` : ''}`}
              onClick={() => onSelectError?.(e)}
              disabled={!onSelectError}
              className={rowBtn(playheadSeq === e.seq)}
            >
              <span className="w-10 shrink-0 text-right font-mono ds-text-body-xs-medium text-ds-text-subtler">
                {clockAt(startedAt, e.t).slice(0, 5)}
              </span>
              <span className="flex min-w-0 flex-col gap-0.5">
                <span className="truncate font-mono ds-text-body-xs-semibold text-ds-text-darker">
                  {e.label}
                  {e.exitCode != null ? (
                    <span className="text-ds-text-subtler"> · exit {e.exitCode}</span>
                  ) : null}
                </span>
                <span className="truncate ds-text-body-xs-medium text-ds-text-subtler">
                  {[
                    e.durMs ? `took ${fmtDur(e.durMs)}` : null,
                    e.task != null
                      ? `${noun.one.toLowerCase()} ${e.task}`
                      : null,
                    e.target,
                  ]
                    .filter(Boolean)
                    .join(' · ')}
                </span>
              </span>
            </button>
          ))}
        </section>
      ))}
    </>
  );
}

function TaskList({
  tasks,
  sums,
  task,
  startedAt,
  durationMs,
  onSelect,
  errors,
  onJumpToErrors,
}: {
  tasks: TaskWindow[];
  sums: Map<number, TaskSummary>;
  task: number | null;
  startedAt: string;
  durationMs: number;
  onSelect: (n: number | null) => void;
  errors?: Map<number, TaskErrors>;
  onJumpToErrors?: (task: number, seq: number) => void;
}) {
  const noun = taskNoun(tasks);
  return (
    <ul
      className="flex flex-col gap-0.5"
      aria-label={`${noun.many} in this session`}
    >
      <li>
        <button
          type="button"
          aria-pressed={task == null}
          onClick={() => onSelect(null)}
          className={rowBtn(task == null)}
        >
          <span className="w-5 shrink-0 text-right font-mono ds-text-body-xs-medium text-ds-text-subtler">
            ∗
          </span>
          <span className="flex min-w-0 flex-col">
            <span className="ds-text-body-xs-semibold text-ds-text-darker">
              All {noun.many.toLowerCase()}
            </span>
            <span className="ds-text-body-xs-medium text-ds-text-subtle">
              {tasks.length} {noun.many.toLowerCase()} · {fmtDur(durationMs)}
            </span>
          </span>
        </button>
      </li>
      {tasks.map((t) => {
        const s = sums.get(t.n);
        const outs = s ? outputsLine(s.outputs, s.prsCreated) : '';
        const errs = errors?.get(t.n);
        const imgs = s?.promptImages ?? 0;
        const imgsText = `${imgs} ${imgs === 1 ? 'image' : 'images'}`;
        return (
          <li key={t.n} className="relative">
            <button
              type="button"
              aria-pressed={task === t.n}
              aria-label={`${noun.one} ${t.n}: ${t.name}${imgs ? `, ${imgsText}` : ''}`}
              title={`${t.n} · ${t.name}`}
              onClick={() => onSelect(task === t.n ? null : t.n)}
              className={rowBtn(task === t.n)}
            >
              <span className="w-5 shrink-0 text-right font-mono ds-text-body-xs-medium text-ds-text-subtler">
                {t.n}
              </span>
              <span className="flex min-w-0 flex-col gap-0.5">
                <span className="line-clamp-2 ds-text-body-xs-semibold text-ds-text-darker">
                  {t.name}
                </span>
                <span className="ds-text-body-xs-medium text-ds-text-subtle">
                  {spanLabel(startedAt, t.startT, t.endT)} ·{' '}
                  {fmtDur(t.endT - t.startT)} · {costLabel(s?.cost ?? 0)} ·{' '}
                  {s?.calls ?? 0} calls
                </span>
                {outs ? (
                  <span className="ds-text-body-xs-medium text-ds-text-subtler">
                    {outs}
                  </span>
                ) : null}
                {/* Room for the tags, which sit over this corner: a button
                    can't hold another button. */}
                {errs || imgs ? <span aria-hidden className="h-5" /> : null}
              </span>
            </button>
            {errs || imgs ? (
              // Clicks fall through to the row, except on the error tag.
              <div className="pointer-events-none absolute bottom-2 left-9 flex items-center gap-1.5">
                {errs ? (
                  <button
                    type="button"
                    onClick={() => onJumpToErrors?.(t.n, errs.firstSeq)}
                    disabled={!onJumpToErrors}
                    aria-label={`${noun.one} ${t.n}: ${errs.count} ${errs.count === 1 ? 'error' : 'errors'}. Show them`}
                    title={`Show this ${noun.one.toLowerCase()}'s errors`}
                    className="pointer-events-auto rounded-full outline-none focus-visible:ring-2 focus-visible:ring-ds-brand-subtle"
                  >
                    <Badge variant="error">
                      {errs.count} {errs.count === 1 ? 'error' : 'errors'}
                    </Badge>
                  </button>
                ) : null}
                {imgs ? (
                  <Badge variant="secondary" aria-hidden>
                    <ImageSquare size={12} aria-hidden />
                    {imgsText}
                  </Badge>
                ) : null}
              </div>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

/** Thin bar: where in the session [startT, endT] ran. */
function SpanBar({
  startT,
  endT,
  durationMs,
}: {
  startT: number;
  endT: number;
  durationMs: number;
}) {
  const { left, width } = spanBarGeometry(startT, endT, durationMs);
  return (
    <span
      aria-hidden
      className="relative mt-1 block h-1 w-full overflow-hidden rounded-full bg-ds-gray-subtlest"
    >
      <span
        className="absolute inset-y-0 rounded-full bg-ds-brand-default"
        style={{ left: `${left * 100}%`, width: `${width * 100}%` }}
      />
    </span>
  );
}

const taskName = (tasks: TaskWindow[], n: number | null) =>
  n == null ? null : (tasks.find((t) => t.n === n)?.name ?? null);

function SubagentList({
  subs,
  tasks,
  startedAt,
  durationMs,
  onOpen,
}: {
  subs: SubagentInfo[];
  tasks: TaskWindow[];
  startedAt: string;
  durationMs: number;
  onOpen: (id: string) => void;
}) {
  const noun = taskNoun(tasks);
  return (
    <ul
      className="flex flex-col gap-0.5"
      aria-label="Subagents in this session"
    >
      {subs.map((s) => {
        const parent = taskName(tasks, s.parentTask);
        return (
          <li key={s.id}>
            <button
              type="button"
              onClick={() => onOpen(s.id)}
              aria-label={`Open ${s.label}`}
              className={rowBtn(false)}
            >
              <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                <span className="truncate ds-text-body-xs-semibold text-ds-text-darker">
                  {s.label}
                </span>
                <span className="ds-text-body-xs-medium text-ds-text-subtle">
                  {spanLabel(startedAt, s.startT, s.endT)} · {s.calls} calls ·{' '}
                  {costLabel(s.cost)}
                </span>
                {s.parentTask != null && tasks.length > 1 ? (
                  <span className="truncate ds-text-body-xs-medium text-ds-text-subtler">
                    in {noun.one.toLowerCase()} {s.parentTask}
                    {parent ? ` · ${parent}` : ''}
                  </span>
                ) : null}
                <SpanBar
                  startT={s.startT}
                  endT={s.endT}
                  durationMs={durationMs}
                />
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

/** The open subagent's facts, shared by the sidebar card and the inspector. */
export function SubagentFacts({
  sub,
  tasks,
  startedAt,
}: {
  sub: SubagentInfo;
  tasks: TaskWindow[];
  startedAt: string;
}) {
  const parent = taskName(tasks, sub.parentTask);
  const rows: [string, string][] = [
    ['When', spanLabel(startedAt, sub.startT, sub.endT)],
    ['Steps', `${sub.window.start + 1}–${sub.window.end + 1}`],
    ['Calls', `${sub.calls} · ${sub.requests} requests`],
    ['Cost', costLabel(sub.cost)],
    ['Tokens', fmtTok(sub.tokens)],
  ];
  if (sub.parentTask != null)
    rows.push([
      'Spawned in',
      `${taskNoun(tasks).one.toLowerCase()} ${sub.parentTask}${parent ? ` · ${parent}` : ''}`,
    ]);
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

/** Inspector (right column) body for an open subagent. */
export function SubagentInspector({
  sub,
  tasks,
  startedAt,
}: {
  sub: SubagentInfo;
  tasks: TaskWindow[];
  startedAt: string;
}) {
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-col">
        <span className="font-mono ds-text-body-xs-medium uppercase tracking-wide text-ds-text-subtler">
          {sub.type}
        </span>
        <span className="ds-text-body-sm-semibold text-ds-text-darker">
          {sub.label}
        </span>
      </div>
      <SubagentFacts sub={sub} tasks={tasks} startedAt={startedAt} />
      <p className="ds-text-body-xs-medium text-ds-text-subtle">
        Map and timeline show only this helper&apos;s work. Play replays its
        window; ‹ › in the strip step through helpers.
      </p>
    </div>
  );
}

/** Subagent mode: "← Main session", the helper card, what it touched. */
export function SubagentSidebar({
  sub,
  total,
  tasks,
  startedAt,
  groups,
  onBack,
  onStep,
  canPrev,
  canNext,
  onCollapse,
}: {
  sub: SubagentInfo;
  total: number;
  tasks: TaskWindow[];
  startedAt: string;
  groups: TouchedGroup[];
  onBack: () => void;
  onStep: (delta: number) => void;
  canPrev: boolean;
  canNext: boolean;
  onCollapse: () => void;
}) {
  const leaves = groups.reduce((n, g) => n + g.items.length, 0);
  return (
    <SidebarFrame>
      <BackRow label="Main session" onBack={onBack} onCollapse={onCollapse} />
      <div className={cn(card, 'border-ds-brand-subtler bg-ds-brand-lightest')}>
        <span className="font-mono ds-text-body-xs-medium text-ds-text-subtle">
          Subagent {sub.index} of {total} · {sub.type}
        </span>
        <span className="ds-text-body-sm-semibold text-ds-text-darker">
          {sub.label}
        </span>
        <SubagentFacts sub={sub} tasks={tasks} startedAt={startedAt} />
        <div className="mt-1 flex gap-1.5">
          <button
            type="button"
            onClick={() => onStep(-1)}
            disabled={!canPrev}
            aria-label="Previous subagent"
            className="inline-flex items-center gap-1 rounded-md border border-ds-border-subtlest bg-ds-surface-white px-3 py-1 ds-text-body-xs-medium text-ds-text-default hover:border-ds-border-default disabled:opacity-40"
          >
            <CaretLeft size={12} /> Prev
          </button>
          <button
            type="button"
            onClick={() => onStep(1)}
            disabled={!canNext}
            aria-label="Next subagent"
            className="inline-flex items-center gap-1 rounded-md border border-ds-border-subtlest bg-ds-surface-white px-3 py-1 ds-text-body-xs-medium text-ds-text-default hover:border-ds-border-default disabled:opacity-40"
          >
            Next <CaretRight size={12} />
          </button>
        </div>
      </div>
      <div className="font-mono ds-text-body-xs-medium uppercase tracking-wide text-ds-text-subtler">
        Touched by this helper · {leaves}
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto">
        {groups.length ? (
          groups.map((g) => (
            <section key={g.id} aria-label={g.name}>
              <div className="mb-1 ds-text-body-xs-semibold text-ds-text-default">
                {g.name}{' '}
                <span className="text-ds-text-subtler">{g.items.length}</span>
              </div>
              <ul className="flex flex-col gap-0.5">
                {g.items.map((it) => (
                  <li
                    key={it.id}
                    className="flex items-center gap-2 ds-text-body-xs-medium"
                  >
                    <span
                      aria-hidden
                      className="size-2 shrink-0 rounded-full"
                      style={{
                        background:
                          it.type === 'file' || it.type === 'dir'
                            ? TOUCH_COLOR[2]
                            : TOUCH_COLOR[1],
                      }}
                    />
                    <span
                      className="min-w-0 flex-1 truncate text-ds-text-default"
                      title={it.name}
                    >
                      {it.name}
                    </span>
                    <span className="shrink-0 font-mono tabular-nums text-ds-text-subtle">
                      {it.count}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          ))
        ) : (
          <p className="ds-text-body-xs-medium text-ds-text-subtle">
            This helper touched no files or tools in this scope.
          </p>
        )}
      </div>
    </SidebarFrame>
  );
}

/**
 * The one-line strip directly above the timeline tracks: the active task
 * (or the task under the playhead while playing) or the open subagent, with
 * ‹ › to step and × to clear.
 */
export function ScopeStrip({
  lead,
  name,
  meta,
  progress,
  onPrev,
  onNext,
  onClear,
  prevLabel,
  nextLabel,
  clearLabel,
  canPrev,
  canNext,
}: {
  lead: string;
  name: string;
  meta: string;
  /** 0..1 while playing, null otherwise */
  progress: number | null;
  onPrev: () => void;
  onNext: () => void;
  onClear: (() => void) | null;
  prevLabel: string;
  nextLabel: string;
  clearLabel: string;
  canPrev: boolean;
  canNext: boolean;
}) {
  return (
    <div
      role="status"
      aria-live="off"
      className="flex min-w-0 items-center gap-3 rounded-lg border border-ds-brand-subtler bg-ds-brand-lightest px-3 py-1.5"
    >
      <span className="shrink-0 font-mono ds-text-body-xs-medium text-ds-brand-default">
        {lead}
      </span>
      <span
        className="min-w-0 shrink truncate ds-text-body-xs-semibold text-ds-text-darker"
        title={name}
      >
        {name}
      </span>
      <span className="min-w-0 flex-1 truncate ds-text-body-xs-medium text-ds-text-subtle">
        {meta}
      </span>
      {progress != null ? (
        <span
          role="progressbar"
          aria-label="Progress through this window"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(progress * 100)}
          className="relative h-1 w-24 shrink-0 overflow-hidden rounded-full bg-ds-surface-white"
        >
          <span
            className="absolute inset-y-0 left-0 rounded-full bg-ds-brand-default"
            style={{ width: `${progress * 100}%` }}
          />
        </span>
      ) : null}
      <span className="flex shrink-0 items-center gap-0.5">
        <button
          type="button"
          onClick={onPrev}
          disabled={!canPrev}
          aria-label={prevLabel}
          title={prevLabel}
          className={cn(iconBtn, 'size-6 disabled:opacity-40')}
        >
          <CaretLeft size={12} />
        </button>
        <button
          type="button"
          onClick={onNext}
          disabled={!canNext}
          aria-label={nextLabel}
          title={nextLabel}
          className={cn(iconBtn, 'size-6 disabled:opacity-40')}
        >
          <CaretRight size={12} />
        </button>
        {onClear ? (
          <button
            type="button"
            onClick={onClear}
            aria-label={clearLabel}
            title={clearLabel}
            className={cn(iconBtn, 'size-6')}
          >
            <X size={12} />
          </button>
        ) : null}
      </span>
    </div>
  );
}
