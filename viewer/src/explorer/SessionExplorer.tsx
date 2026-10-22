import { srcSeqOf } from '@/core/sessionMapModel';
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from 'react';
import { CalendarBlankIcon, MapTrifold } from '@phosphor-icons/react';
import {
  parseAsInteger,
  parseAsString,
  parseAsStringEnum,
  useQueryState,
  useQueryStates,
} from 'nuqs';
import type {
  SessionExplorerSession,
  SessionMap,
} from '@/core/schema';
import { SessionExplorerError } from '@/data/errors';
import { dedupeSessions } from '@/data/parse';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { EmptyState } from '@/components/EmptyState';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { cn } from '@/lib/utils';
import { useDebounce } from '@/hooks/useDebounce';
import {
  useSessionExplorerMap,
  useSessionExplorerSessions,
  useSessionExplorerStatus,
} from '@/data/hooks';
import { WINDOW_OPTIONS, type WindowKey } from '@/core/windowOptions';
import { ExplorerToolbar } from './ExplorerToolbar';
import { ExplorerSidebar, MapPane, type SidebarCtl } from './MapPlayerParts';
import { SessionHeader } from './SessionHeader';
import { SessionList } from './SessionList';
import {
  InlineError,
  MapSkeleton,
  SessionExplorerSkeleton,
  SidePanelsSkeleton,
} from './SessionExplorerStates';
import {
  SessionListHeader,
  SidebarRail,
  SubagentInspector,
} from './SessionSidebar';
import { InsightsPanel, SidePanels } from './SidePanels';
import { TimelineDock } from './TimelineDock';
import {
  SIDEBAR_TABS,
  agentOptions,
  agentOptionsInScope,
  emptyScopeDescription,
  getSidebarCollapsed,
  getSidebarCollapsedServer,
  hasScopeFilter,
  setSidebarCollapsed,
  subscribeSidebarCollapsed,
  type SidebarTab,
} from '@/core/sessionLayout';
import type { TreeNode } from '@/core/sessionMapModel';
import {
  CLEAR_SCOPE_PARAMS,
  SCOPE_PARAMS,
  deriveScopeDoc,
  hasReplayEvents,
  modelUsage,
  scopedModelUsage,
  taskNoun,
} from '@/core/sessionScope';
import { sessionWindowRange } from '@/core/sessionWindow';
import { useEventCategories } from './useEventCategories';
import { useExplorerScope, type ExplorerScope } from './useExplorerScope';
import { useMapModel, usePlaybackWindow } from './usePlaybackWindow';

interface Props {
  /** the window selector's value; the list follows it */
  windowKey: WindowKey;
  /** the window select lives in the sessions sidebar */
  onWindowChange?: (key: WindowKey) => void;
  /** optional control under the "No session maps yet" empty state (e.g. an
   * "Open a map file" button) */
  emptyAction?: ReactNode;
}

/** A cache-busting key that changes at EITHER the viewer's local midnight
 * or UTC midnight, whichever comes first (not a real ISO date). Two
 * different fields of `sessionWindowRange`'s result roll over on two
 * different clocks: `daysThisMonth` derives MTD's `from` from local
 * `getDate()`, but `to` (every window) is `toISOString().slice(0, 10)`,
 * a UTC date. A key on only one clock stays stale across the other
 * boundary for up to the viewer's UTC offset. */
const dateBoundaryKey = (d: Date) =>
  `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}-${d.toISOString().slice(0, 10)}`;

/** The list's date range for `windowKey`, kept fresh across midnight. */
function useWindowRange(windowKey: WindowKey) {
  // `sessionWindowRange` is keyed off `new Date()` at call time, but a memo
  // keyed only on `windowKey` never re-runs that call once the tab has been
  // open a while - the range (mtd's start, and every window's `to`) goes
  // stale after a real calendar day rolls over while the page stays mounted
  //. A cheap poll refreshes it within a minute of that,
  // keyed by both clocks it can roll over on (see `dateBoundaryKey`).
  const [today, setToday] = useState(() => dateBoundaryKey(new Date()));
  useEffect(() => {
    const id = setInterval(() => {
      const next = dateBoundaryKey(new Date());
      setToday((prev) => (prev === next ? prev : next));
    }, 60_000);
    return () => clearInterval(id);
  }, []);
  return useMemo(
    // `today` is not read in the body; it exists purely to invalidate this
    // memo once the poll above notices the calendar date has moved on.
    () => sessionWindowRange(windowKey, new Date()),
    [windowKey, today]
  );
}

/** The sessions in the window (search-filtered), the effective selection,
 * and its map. */
function useExplorerSessions(windowKey: WindowKey) {
  const [search, setSearch] = useQueryState(
    'sq',
    parseAsString.withDefault('')
  );
  const [selected, setSelected] = useQueryState('session', parseAsString);
  const debouncedSearch = useDebounce(search.trim(), 300);
  const range = useWindowRange(windowKey);
  const list = useSessionExplorerSessions(debouncedSearch, range);
  const sessions = useMemo<SessionExplorerSession[]>(
    () =>
      dedupeSessions(list.data?.pages.flatMap((pg) => pg.sessions) ?? []).filter(
        (s) => s.calls > 0
      ),
    [list.data]
  );
  const listIds = useMemo(() => sessions.map((s) => s.sessionId), [sessions]);
  const selectedId = sessions.some((s) => s.sessionId === selected)
    ? selected
    : (sessions[0]?.sessionId ?? null);
  const selectedRow = sessions.find((s) => s.sessionId === selectedId) ?? null;
  const mapQuery = useSessionExplorerMap(selectedId, listIds);
  return {
    search,
    setSearch,
    selected,
    setSelected,
    debouncedSearch,
    list,
    sessions,
    selectedId,
    selectedRow,
    mapQuery,
  };
}

type MapQuery = ReturnType<typeof useSessionExplorerMap>;

function WindowSelect({
  windowKey,
  onChange,
}: {
  windowKey: WindowKey;
  onChange: (key: WindowKey) => void;
}) {
  return (
    <Select
      value={windowKey}
      // label lookup for SelectValue (otherwise it renders the raw key)
      items={WINDOW_OPTIONS.map((o) => ({ value: o.key, label: o.label }))}
      onValueChange={(v: string | null) => {
        if (v) onChange(v as WindowKey);
      }}
    >
      <SelectTrigger
        aria-label="Select time window"
        className="h-8 w-full gap-2"
      >
        <CalendarBlankIcon
          size={14}
          className="shrink-0 text-ds-text-subtle"
          weight="duotone"
        />
        <SelectValue />
      </SelectTrigger>
      <SelectContent align="start">
        {WINDOW_OPTIONS.map((opt) => (
          <SelectItem key={opt.key} value={opt.key}>
            {opt.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

/**
 * The Session Explorer: sessions sidebar + the selected session's replay
 * (header, filter toolbar, map + details, timeline dock). It fills the
 * height it is given (a flex parent): the map row takes what the docked
 * timeline leaves, and only the inner regions scroll.
 */
export function SessionExplorer({
  windowKey,
  onWindowChange,
  emptyAction,
}: Props) {
  const data = useExplorerSessions(windowKey);
  const { list, sessions, selectedId } = data;
  const scanning = !!useSessionExplorerStatus().data?.scanning;
  const [, setScopeParams] = useQueryStates(SCOPE_PARAMS);
  // One sidebar: the list until a session is picked (or one is linked), then
  // only that session; "All sessions" flips back without touching the URL.
  const [sidebarMode, setSidebarMode] = useState<'list' | 'session'>(() =>
    data.selected ? 'session' : 'list'
  );
  // An external store (localStorage-backed, every access guarded) so the
  // first client render already has the persisted value; SSR renders
  // expanded via the server snapshot.
  const collapsed = useSyncExternalStore(
    subscribeSidebarCollapsed,
    getSidebarCollapsed,
    getSidebarCollapsedServer
  );
  const setCollapsed = setSidebarCollapsed;

  if (list.isLoading && !list.data)
    return (
      <div className="flex flex-col gap-3">
        <SessionExplorerSkeleton />
      </div>
    );
  if (list.isError && !list.data) {
    console.error('session explorer: session list failed', list.error);
    return (
      <div className="flex flex-col gap-3">
        <InlineError
          title="Could not load sessions"
          description="The list of recorded sessions did not load. Try again in a moment."
          onRetry={() => void list.refetch()}
        />
      </div>
    );
  }
  const windowSelect = onWindowChange ? (
    <WindowSelect windowKey={windowKey} onChange={onWindowChange} />
  ) : null;

  if (!sessions.length && !data.debouncedSearch && !list.hasNextPage) {
    // The window select lives in the sidebar, which this state replaces, so
    // it is rendered here too -- otherwise "try a longer window" would have
    // no control to act on.
    return (
      <div className="flex flex-col gap-3">
        {windowSelect ? <div className="w-48">{windowSelect}</div> : null}
        {scanning ? (
          // The first scan is still running: "none yet" would be a false
          // answer. The list fills in by itself as maps land.
          <EmptyState
            icon={<MapTrifold size={24} weight="duotone" />}
            title="Scanning your sessions…"
            description="agentplay is building maps from your coding-agent sessions. They appear here as they are ready."
          />
        ) : (
          <EmptyState
            icon={<MapTrifold size={24} weight="duotone" />}
            title="No session maps yet"
            description="Session maps appear here once a coding-agent session has been indexed. Try a longer window if you expected one."
            action={emptyAction}
          />
        )}
      </div>
    );
  }

  const listNode = (
    <SessionList
      sessions={sessions}
      selectedId={selectedId}
      // Picking a session shows its map and Details; the list stays, so
      // the next one is one click away rather than one back-out away.
      onSelect={(id) => {
        if (id !== selectedId) void setScopeParams(CLEAR_SCOPE_PARAMS);
        void data.setSelected(id);
      }}
      // Open descends into it, on every task: any task/model narrowing from
      // the previous session would otherwise follow you in.
      onOpen={(id) => {
        void setScopeParams(CLEAR_SCOPE_PARAMS);
        void data.setSelected(id);
        setSidebarMode('session');
      }}
      search={data.search}
      onSearch={(v) => void data.setSearch(v || null)}
      hasMore={!!list.hasNextPage}
      loadingMore={list.isFetchingNextPage}
      onLoadMore={() => void list.fetchNextPage()}
      unavailable={data.mapQuery.unavailable}
      header={
        <SessionListHeader
          count={sessions.length}
          onCollapse={() => setCollapsed(true)}
        />
      }
      toolbar={windowSelect}
    />
  );
  const sidebar: SidebarCtl = {
    mode: sidebarMode,
    collapsed,
    setCollapsed,
    showList: () => setSidebarMode('list'),
    showSession: () => setSidebarMode('session'),
    list: listNode,
  };

  return (
    <div
      className={cn(
        'grid min-h-0 flex-1 grid-rows-[minmax(0,1fr)] gap-4',
        collapsed
          ? 'grid-cols-[40px_minmax(0,1fr)]'
          : 'grid-cols-[300px_minmax(0,1fr)]'
      )}
    >
      <ExplorerBody
        selectedId={selectedId}
        mapQuery={data.mapQuery}
        row={data.selectedRow}
        sidebar={sidebar}
      />
    </div>
  );
}

/** The sidebar column's slot: the explorer's full height, scrolling inside. */
function SidebarSlot({ children }: { children: ReactNode }) {
  return (
    <aside
      aria-label="Session explorer sidebar"
      className="h-full min-h-0 min-w-0"
    >
      {children}
    </aside>
  );
}

/** Everything right of (and including) the sidebar, by map state. */
function ExplorerBody({
  selectedId,
  mapQuery,
  row,
  sidebar,
}: {
  selectedId: string | null;
  mapQuery: MapQuery;
  row: SessionExplorerSession | null;
  sidebar: SidebarCtl;
}) {
  // Without a loaded map there is no session to show: the list (or rail).
  const listSidebar = (
    <SidebarSlot>
      {sidebar.collapsed ? (
        <SidebarRail
          label="Sessions"
          onExpand={() => sidebar.setCollapsed(false)}
        />
      ) : (
        sidebar.list
      )}
    </SidebarSlot>
  );
  // Until a map loads (and on errors) the right column is one stacked cell.
  const column = (content: ReactNode) => (
    <div className="flex min-h-0 min-w-0 flex-col gap-3">{content}</div>
  );
  if (!selectedId) {
    return (
      <>
        {listSidebar}
        {column(
          <EmptyState
            title="Pick a session"
            description="Choose a session on the left to replay its map."
          />
        )}
      </>
    );
  }
  if (mapQuery.isError) {
    console.error('session explorer: map failed', mapQuery.error);
    const copy = mapErrorCopy(mapQuery.error);
    return (
      <>
        {listSidebar}
        {column(
          <InlineError
            title={copy.title}
            description={copy.description}
            onRetry={copy.retry ? () => void mapQuery.retry() : undefined}
          />
        )}
      </>
    );
  }
  if (mapQuery.isLoading || !mapQuery.data) {
    return (
      <>
        {listSidebar}
        {column(
          <div className="grid min-h-0 min-w-0 grid-cols-[minmax(0,1fr)_300px] items-start gap-4 overflow-hidden">
            <MapSkeleton />
            <SidePanelsSkeleton />
          </div>
        )}
      </>
    );
  }
  return (
    <ErrorBoundary
      key={selectedId}
      fallback={
        <>
          {listSidebar}
          {column(
            <InlineError
              title="This session could not be displayed."
              description="Pick another session from the list."
            />
          )}
        </>
      }
    >
      <MapPlayer
        sessionId={selectedId}
        map={mapQuery.data}
        row={row}
        sidebar={sidebar}
      />
    </ErrorBoundary>
  );
}

function mapErrorCopy(error: unknown): {
  title: string;
  description: string;
  retry: boolean;
} {
  const code = error instanceof SessionExplorerError ? error.code : '';
  if (code === 'not_found') {
    return {
      title: 'Map not available',
      description:
        'This session has no stored map. Pick another session from the list.',
      retry: false,
    };
  }
  if (code === 'invalid_map') {
    return {
      title: 'This session map has a field this page cannot read',
      description: `${(error as Error).message}.`,
      retry: false,
    };
  }
  if (code === 'unsupported_schema') {
    return {
      title: 'This session map needs a newer page',
      description:
        'It was recorded in a format this page cannot draw yet. Refresh the page to pick up the latest version.',
      retry: false,
    };
  }
  return {
    title: 'Could not load this session map',
    description: 'Something went wrong fetching the map. Try again.',
    retry: true,
  };
}

/**
 * Binds the URL scope + agent lens to this session (`useExplorerScope`) and
 * remounts `MapPlayerBody` on any change (`key`), since `useReducer`'s lazy
 * playback init only runs once per mount and must re-run against the new
 * event count.
 */
function MapPlayer({
  sessionId,
  map,
  row,
  sidebar,
}: {
  sessionId: string;
  map: SessionMap;
  row: SessionExplorerSession | null;
  sidebar: SidebarCtl;
}) {
  const x = useExplorerScope(sessionId, map);
  return (
    <MapPlayerBody
      key={x.key}
      x={x}
      row={row}
      sidebar={sidebar}
    />
  );
}

/** `?at=<seq>`: a deep link to one event (e.g. from an error row or a badge).
 * Seeks the replay to the event carrying that `seq` (or that index when no
 * event carries it), then drops the param so a later seek is not undone. */
function useSeekParam(map: SessionMap, seek: (index: number) => void) {
  const [at, setAt] = useQueryState('at', parseAsInteger);
  useEffect(() => {
    if (at == null) return;
    const n = map.events.length;
    if (n) {
      // `srcSeqOf`, not `e.seq`: a task or model scope renumbers `seq`, and a
      // link (or a task's error tag) names the whole-session event.
      const bySeq = map.events.findIndex((e) => srcSeqOf(e) === at);
      seek(bySeq >= 0 ? bySeq : Math.max(0, Math.min(n - 1, at)));
    }
    void setAt(null);
  }, [at, map.events, seek, setAt]);
}

/** URL-backed sidebar tab. */
function useSideTab() {
  const [sideTab, setSideTab] = useQueryState(
    'stab',
    parseAsStringEnum<SidebarTab>([...SIDEBAR_TABS])
  );
  return { sideTab, setSideTab };
}

/** Task / model toggles, and what a click on a map node does. */
function useScopeActions(x: ExplorerScope) {
  const { setScope, lens, fullMap, setLens } = x;
  const selectTask = useCallback(
    (n: number) =>
      setScope((s0) => ({ ...s0, task: s0.task === n ? null : n })),
    [setScope]
  );
  const selectModel = useCallback(
    (m: string) =>
      setScope((s0) => ({ ...s0, model: s0.model === m ? null : m })),
    [setScope]
  );
  const onNodeAction = useCallback(
    (node: TreeNode): boolean => {
      if (node.type === 'model') {
        selectModel(node.name);
        return true;
      }
      if (node.type !== 'agent' || lens !== 'main') return false;
      const id = node.id.slice('agent:'.length);
      if (!fullMap.agents.some((a) => a.id === id)) return false;
      setLens(id);
      return true;
    },
    [selectModel, lens, fullMap.agents, setLens]
  );
  return { selectTask, selectModel, onNodeAction };
}

const REPLAY_LABEL =
  'Session replay. Space plays or pauses, arrow keys step, Shift steps 10, E jumps to the next edit, X to the next error, D to the next delay, O to the next output, R to the next retry.';

/**
 * The fit-viewport replay: sidebar | (header with the filters on its line,
 * map + details row that takes the remaining height, timeline pinned at the
 * bottom).
 */
function MapPlayerBody({
  x,
  row,
  sidebar,
}: {
  x: ExplorerScope;
  row: SessionExplorerSession | null;
  sidebar: SidebarCtl;
}) {
  const { map, scope, sub } = x;
  const url = useSideTab();
  const cats = useEventCategories();
  const { selectTask, selectModel, onNodeAction } = useScopeActions(x);
  const scopedModels = useMemo(() => modelUsage(map), [map]);
  // Both pickers count inside the other filters: the Agent rows inside the
  // prompt/model scope, the Model rows inside the prompt.
  const agents = useMemo(
    () =>
      agentOptionsInScope(agentOptions(x.fullMap, x.ordinals), x.scopedMap),
    [x.fullMap, x.ordinals, x.scopedMap]
  );
  const pickerModels = useMemo(
    () =>
      scopedModelUsage(
        x.models,
        deriveScopeDoc(x.fullMap, { task: scope.task, model: null })
      ),
    [x.models, x.fullMap, scope.task]
  );
  const model = useMapModel(map);
  const playback = usePlaybackWindow(map);
  const { pb } = playback;
  useSeekParam(map, playback.seek);
  // A task row's error tag, or an Errors row the scope hides (`task` null when
  // that error has none: the task filter is cleared). Scope, tab and `at` change in one URL update, so the
  // replay re-scoped to the task is the one that seeks: `at` is a `seq`, which
  // survives the re-scope where an index would not. The model filter and the
  // agent lens are cleared so they can't hide the error being jumped to (the
  // Errors tab lists every agent's errors).
  const [, setAt] = useQueryState('at', parseAsInteger);
  const jumpToTaskErrors = useCallback(
    (task: number | null, seq: number) => {
      x.setScope((s0) => ({ ...s0, task, model: null }), { clearLens: true });
      void url.setSideTab('errors');
      void setAt(seq);
    },
    [x, url, setAt]
  );
  const subInfo = sub ? (x.subs.find((s) => s.id === sub) ?? null) : null;
  const hasEvents = hasReplayEvents(map);
  // The review comment whose causal path the map is lighting, as the event
  // `seq`s on that path. Owned here because the comment is clicked in the
  // right-hand column and drawn on the map beside it.
  const [reviewPath, setReviewPath] = useState<readonly number[] | null>(null);
  // How far the dock's optional lanes (Subagents / Context) grow it past
  // its budget (TimelineDock's onOverflow). The column grows by that much
  // below the fold, so the map's flex row keeps its size and the page
  // scrolls instead of shrinking it. Null until the dock first measures.
  const [dockExtra, setDockExtra] = useState<number | null>(null);
  const columnRef = useRef<HTMLDivElement>(null);
  const prevExtra = useRef<number | null>(null);
  useEffect(() => {
    if (dockExtra == null) return;
    const prev = prevExtra.current;
    prevExtra.current = dockExtra;
    // A lane was just turned on or expanded: scroll it into view. Not on
    // the first measurement, so opening a session never jumps the page.
    if (prev != null && dockExtra > prev)
      columnRef.current?.scrollIntoView({ block: 'end', behavior: 'smooth' });
  }, [dockExtra]);

  const insights = hasEvents ? (
    <InsightsPanel
      map={map}
      p={pb.p}
      ordinals={model.ordinals}
      review={model.review}
      signals={model.signals}
      onJumpSource={playback.jumpSource}
      models={scopedModels}
      model={scope.model}
      onSelectModel={selectModel}
    />
  ) : (
    <p className="ds-text-body-xs-medium text-ds-text-subtle">
      Nothing in this scope.
    </p>
  );

  return (
    <>
      <SidebarSlot>
        <ExplorerSidebar
          x={x}
          model={model}
          row={row}
          sidebar={sidebar}
          subInfo={subInfo}
          tabParam={url.sideTab}
          onTab={(t) => void url.setSideTab(t)}
          insights={insights}
          onFocusCategory={(c) => cats.setSelected(new Set([c]))}
          p={pb.p}
          onSeek={playback.seek}
          onJumpToTaskErrors={jumpToTaskErrors}
        />
      </SidebarSlot>
      <div
        ref={columnRef}
        className="flex min-h-0 min-w-0 flex-col gap-3 outline-none"
        style={
          dockExtra ? { height: `calc(100% + ${dockExtra}px)` } : undefined
        }
        onKeyDown={playback.onKeyDown}
        role="region"
        tabIndex={-1}
        aria-label={REPLAY_LABEL}
      >
        <SessionHeader
          map={map}
          row={row}
          lensLabel={x.lensLabel}
          onExitLens={() => x.setLens('main')}
          filters={
            <ExplorerToolbar
              tasks={x.tasks}
              task={scope.task}
              onTask={(task) => x.setScope((s0) => ({ ...s0, task }))}
              agents={agents}
              lens={x.lens}
              lensLabel={x.lensLabel}
              onLens={x.setLens}
              models={pickerModels}
              model={scope.model}
              onModel={(m) => x.setScope((s0) => ({ ...s0, model: m }))}
              filtered={hasScopeFilter(scope, sub)}
              onClear={x.clearAll}
            />
          }
        />
        {/* The middle row takes whatever height the docked timeline
            leaves; the map scales into it and the details column scrolls
            inside itself. */}
        <div className="grid min-h-0 min-w-0 flex-1 grid-cols-[minmax(0,1fr)_300px] grid-rows-[minmax(0,1fr)] gap-4">
          <MapPane
            map={map}
            model={model}
            p={pb.p}
            onSeek={playback.seek}
            onNodeAction={onNodeAction}
            filtered={hasScopeFilter(scope, sub)}
            filteredCopy={emptyScopeDescription(scope, sub, taskNoun(x.tasks))}
            playing={pb.playing}
            selected={cats.selected}
            path={reviewPath}
          />
          {hasEvents ? (
            <SidePanels
              map={map}
              fullMap={x.fullMap}
              p={pb.p}
              ordinals={model.ordinals}
              cumCost={model.cumCost}
              onSeek={playback.seek}
              tasks={x.tasks}
              sessionStart={x.fullMap.session.startedAt}
              task={scope.task}
              onSelectTask={selectTask}
              scoped={scope.model != null}
              selected={cats.selected}
              picks={cats.picks}
              reviewPath={reviewPath}
              onReviewPath={setReviewPath}
              inspector={
                subInfo ? (
                  <SubagentInspector
                    sub={subInfo}
                    tasks={x.tasks}
                    startedAt={x.fullMap.session.startedAt}
                  />
                ) : undefined
              }
            />
          ) : (
            <div />
          )}
        </div>
        {hasEvents ? (
          <TimelineDock
            x={x}
            playback={playback}
            model={model}
            cats={cats}
            onSelectTask={selectTask}
            subInfo={subInfo}
            collapsed={sidebar.collapsed}
            onOverflow={setDockExtra}
            columnExtra={dockExtra}
          />
        ) : null}
      </div>
    </>
  );
}
