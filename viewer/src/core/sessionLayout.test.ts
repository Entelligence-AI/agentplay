import { describe, expect, it, vi } from 'vitest';
import {
  SESSION_MAP_SCHEMA_V1,
  sessionMapSchema,
  type SessionExplorerSession,
  type SessionMap,
  type SessionMapEvent,
  type SessionMapPr,
} from './schema';
import {
  buildTree,
  deriveLensDoc,
  eventNodeRefs,
  outputTotals,
  srcSeqOf,
  subagentOrdinal,
  activeSet,
  type NodeRef,
} from './sessionMapModel';
import { deriveScopeDoc, sessionTasks, stampTasks, withoutSrcSeq } from './sessionScope';
import { axisTickLabels, optionalLanesHeight } from './timelineDraw';
import {
  SIDEBAR_COLLAPSED_KEY,
  adjacentSubagent,
  adjacentTask,
  agentWindow,
  costLabel,
  lensSourceIndex,
  nearestLensIndex,
  orderedSubagents,
  outputsLine,
  readSidebarCollapsed,
  resolveSidebarTab,
  errorClickAction,
  sessionErrors,
  taskErrors,
  wholeSessionErrors,
  sidebarTabs,
  stripState,
  taskAt,
  taskIndexWindow,
  taskSummaries,
  touchedGroups,
  windowProgress,
  writeSidebarCollapsed,
  getSidebarCollapsed,
  setSidebarCollapsed,
  spanBarGeometry,
  subscribeSidebarCollapsed,
  agentOptions,
  agentOptionsInScope,
  emptyScopeDescription,
  detailsTitle,
  groupSessionsByDay,
  hasScopeFilter,
  outcomeParts,
  prCountLabel,
  prMenuItems,
  safeLinkUrl,
  promptLine,
  sidebarRailLabel,
  taskFilterLabel,
  CATEGORIES,
  DEFAULT_CATEGORIES,
  activeRefs,
  DOCK_HEIGHT_KEY,
  DOCK_MIN_HEIGHT,
  clampDockHeight,
  dockOverflow,
  categoryIndices,
  categoryMatches,
  categoryOf,
  displayCategory,
  isDefaultCategories,
  lastPicked,
  mapChipSelection,
  magnitude,
  matchesAnyCategory,
  mapFocus,
  pathFocus,
  narrowsView,
  parseCategories,
  pickHistory,
  serializeCategories,
  sizeMetric,
  toggleCategory,
  type Category,
  getDockHeight,
  readDockHeight,
  setDockHeight,
  stepEvent,
  stepPosition,
  subagentLanes,
  subscribeDockHeight,
  writeDockHeight,
} from './sessionLayout';

type RawEvent = Record<string, unknown> & { kind: string };

function makeMap(events: RawEvent[]): SessionMap {
  return sessionMapSchema.parse({
    schema: SESSION_MAP_SCHEMA_V1,
    session: {
      id: 'local:claude-code:s1',
      agentTool: 'claude-code',
      startedAt: '2026-09-23T15:44:00Z',
      durationMs: 60_000,
      models: ['opus-5.5'],
    },
    totals: { calls: 0, requests: 0 },
    repos: [{ id: 'r0', name: 'dashboard' }],
    files: [
      { id: 0, repo: 'r0', path: 'src/proxy.ts' },
      { id: 1, repo: 'r0', path: 'src/app.ts' },
    ],
    agents: [
      { id: 'main', parent: null, kind: 'main' },
      { id: 'subA', parent: 'main', kind: 'subagent', subagentType: 'Explore' },
      { id: 'subB', parent: 'main', kind: 'subagent', subagentType: 'fork' },
      { id: 'ghost', parent: 'main', kind: 'subagent' },
    ],
    prs: [],
    events: events.map((e, i) => ({
      seq: i,
      t: i * 1000,
      agent: 'main',
      ...e,
    })),
  });
}

/** Two tasks: subA is spawned and runs in task 1, subB in task 2; the
 * `ghost` agent is listed but never runs. */
const EVENTS: RawEvent[] = [
  { kind: 'user', chars: 10 }, // 0  task 1
  { kind: 'llm', model: 'opus-5.5', cost: 0.5 }, // 1
  { kind: 'subagent', child: 'subA', subagentType: 'Explore' }, // 2
  { kind: 'llm', agent: 'subA', model: 'haiku-4.5', cost: 0 }, // 3
  { kind: 'read', agent: 'subA', targets: [{ file: 0, touch: 'read' }] }, // 4
  { kind: 'read', agent: 'subA', targets: [{ file: 0, touch: 'read' }] }, // 5
  { kind: 'edit', agent: 'subA', targets: [{ file: 1, touch: 'edit' }] }, // 6
  { kind: 'user', chars: 20 }, // 7  task 2
  { kind: 'subagent', child: 'subB', subagentType: 'fork' }, // 8
  { kind: 'exec', agent: 'subB' }, // 9
  { kind: 'llm', model: 'opus-5.5', cost: 1.25 }, // 10
  { kind: 'commit', repo: 'dashboard', sha: 'abc', files: 1 }, // 11
];

const map = stampTasks(makeMap(EVENTS));

describe('windows', () => {
  it('agentWindow spans first..last event of the agent', () => {
    expect(agentWindow(map.events, 'subA')).toEqual({ start: 3, end: 6 });
    expect(agentWindow(map.events, 'subB')).toEqual({ start: 9, end: 9 });
    expect(agentWindow(map.events, 'ghost')).toBeNull();
  });

  it('taskIndexWindow uses the task stamps', () => {
    expect(taskIndexWindow(map.events, 1)).toEqual({ start: 0, end: 6 });
    expect(taskIndexWindow(map.events, 2)).toEqual({ start: 7, end: 11 });
    expect(taskIndexWindow(map.events, 9)).toBeNull();
  });

  it('lensSourceIndex maps a lensed replay back onto the session', () => {
    const src = lensSourceIndex(map.events, 'subA');
    expect(src).toEqual([3, 4, 5, 6]);
    // the lens doc has exactly those events, in that order
    expect(deriveLensDoc(map, 'subA').events).toHaveLength(src.length);
  });

  it('nearestLensIndex snaps a session index to the closest lens event', () => {
    const src = [3, 4, 5, 9];
    expect(nearestLensIndex(src, 0)).toBe(0);
    expect(nearestLensIndex(src, 4)).toBe(1);
    expect(nearestLensIndex(src, 7)).toBe(2); // 5 and 9 tie -> earlier
    expect(nearestLensIndex(src, 8)).toBe(3);
    expect(nearestLensIndex(src, 100)).toBe(3);
    expect(nearestLensIndex([], 4)).toBe(0);
  });

  it('windowProgress clamps to the window', () => {
    expect(windowProgress({ start: 10, end: 20 }, 5)).toBe(0);
    expect(windowProgress({ start: 10, end: 20 }, 15)).toBe(0.5);
    expect(windowProgress({ start: 10, end: 20 }, 25)).toBe(1);
    expect(windowProgress({ start: 4, end: 4 }, 4)).toBe(1);
    expect(windowProgress(null, 4)).toBe(0);
  });
});

describe('tasks', () => {
  const { tasks } = sessionTasks(map);

  it('taskAt reads the task under the playhead', () => {
    expect(taskAt(map.events, 0)).toBe(1);
    expect(taskAt(map.events, 9)).toBe(2);
    expect(taskAt(map.events, 99)).toBeNull();
  });

  it('adjacentTask steps and clamps', () => {
    expect(adjacentTask(tasks, 1, 1)).toBe(2);
    expect(adjacentTask(tasks, 2, 1)).toBe(2);
    expect(adjacentTask(tasks, 1, -1)).toBe(1);
    expect(adjacentTask(tasks, null, 1)).toBe(1);
    expect(adjacentTask(tasks, null, -1)).toBe(2);
    expect(adjacentTask([], 1, 1)).toBeNull();
  });

  it('taskSummaries sums each task once', () => {
    const s = taskSummaries(map);
    expect(s.get(1)?.cost).toBeCloseTo(0.5);
    expect(s.get(2)?.cost).toBeCloseTo(1.25);
    expect(s.get(2)?.outputs.commits).toBe(1);
    expect(s.get(1)?.requests).toBe(2);
  });

  it('costLabel says n/a instead of $0.00', () => {
    expect(costLabel(0)).toBe('cost n/a');
    expect(costLabel(1.5)).toBe('$1.50');
  });

  it('outputsLine lists only non-zero outputs', () => {
    expect(outputsLine(outputTotals(map.events))).toBe('1 commit');
    expect(outputsLine(outputTotals([]))).toBe('');
    expect(outputsLine(outputTotals([]), 2)).toBe('2 PRs created');
  });
});

describe('subagents', () => {
  const ordinals = subagentOrdinal(map);

  it('orders by first event, skips agents with no events, finds the parent task', () => {
    const subs = orderedSubagents(map, ordinals);
    expect(subs.map((s) => s.id)).toEqual(['subA', 'subB']);
    expect(subs.map((s) => s.index)).toEqual([1, 2]);
    expect(subs[0]).toMatchObject({
      label: 'Sub 1: Explore',
      type: 'Explore',
      window: { start: 3, end: 6 },
      startT: 3000,
      endT: 6000,
      parentTask: 1,
      requests: 1,
      cost: 0,
    });
    expect(subs[1].parentTask).toBe(2);
  });

  it('orders by start even when the agents list says otherwise', () => {
    const late = stampTasks(
      makeMap([
        { kind: 'user' },
        { kind: 'exec', agent: 'subB' },
        { kind: 'exec', agent: 'subA' },
      ])
    );
    expect(
      orderedSubagents(late, subagentOrdinal(late)).map((s) => s.id)
    ).toEqual(['subB', 'subA']);
  });

  it('falls back to the first event task without a spawn event', () => {
    const noSpawn = stampTasks(
      makeMap([{ kind: 'user' }, { kind: 'exec', agent: 'subA' }])
    );
    expect(
      orderedSubagents(noSpawn, subagentOrdinal(noSpawn))[0].parentTask
    ).toBe(1);
  });

  it('adjacentSubagent steps without wrapping', () => {
    const subs = orderedSubagents(map, ordinals);
    expect(adjacentSubagent(subs, 'subA', 1)).toBe('subB');
    expect(adjacentSubagent(subs, 'subB', 1)).toBeNull();
    expect(adjacentSubagent(subs, 'subA', -1)).toBeNull();
    expect(adjacentSubagent(subs, 'nope', 1)).toBeNull();
  });

  it('touchedGroups groups the helper leaves by top-level group', () => {
    const lens = deriveLensDoc(map, 'subA');
    const tree = buildTree(lens);
    const groups = touchedGroups(tree, eventNodeRefs(lens, tree));
    const repo = groups.find((g) => g.name === 'dashboard');
    expect(repo).toBeDefined();
    const names = repo!.items.map((i) => i.name);
    expect(names.some((n) => n.includes('proxy.ts'))).toBe(true);
    // most-touched first: proxy.ts was read twice, app.ts edited once
    expect(repo!.items[0].count).toBeGreaterThanOrEqual(
      repo!.items[repo!.items.length - 1].count
    );
    expect(groups.every((g) => g.items.every((i) => i.type !== 'model'))).toBe(
      true
    );
  });
});

describe('stripState', () => {
  const base = {
    task: null,
    sub: null,
    collapsed: false,
    playing: false,
    playheadTask: 1,
    taskCount: 3,
  };

  it('hides with nothing active', () => {
    expect(stripState(base)).toBeNull();
    expect(stripState({ ...base, collapsed: true })).toBeNull();
  });

  it('shows a selected task only when collapsed or playing', () => {
    expect(stripState({ ...base, task: 2 })).toBeNull();
    expect(stripState({ ...base, task: 2, collapsed: true })).toEqual({
      kind: 'task',
      n: 2,
      playing: false,
      followsPlayhead: false,
    });
    expect(stripState({ ...base, task: 2, playing: true })).toMatchObject({
      n: 2,
      playing: true,
      followsPlayhead: false,
    });
  });

  it('follows the playhead task while playing with no task', () => {
    expect(stripState({ ...base, playing: true, playheadTask: 3 })).toEqual({
      kind: 'task',
      n: 3,
      playing: true,
      followsPlayhead: true,
    });
    expect(stripState({ ...base, playing: true, taskCount: 1 })).toBeNull();
    expect(
      stripState({ ...base, playing: true, playheadTask: null })
    ).toBeNull();
  });

  it('an open subagent wins over the task', () => {
    expect(
      stripState({ ...base, task: 2, sub: 'subA', playing: true })
    ).toEqual({ kind: 'sub', id: 'subA', playing: true });
    expect(stripState({ ...base, sub: 'subA' })).toBeNull();
  });
});

describe('wholeSessionErrors', () => {
  const err = (seq: number, task: number, verb: string): RawEvent => ({
    kind: 'exec', seq, t: seq * 10, tool: 'Bash', verb, err: true, errClass: 'exit_code', task,
  });
  const full = () =>
    makeMap([err(0, 1, 'ls'), err(1, 1, 'pytest'), err(2, 2, 'curl'), err(3, 3, 'ls')]);

  it('keeps the whole-session count and rows under any scope', () => {
    const m = full();
    const whole = wholeSessionErrors(m);
    expect(whole.total).toBe(4);
    // The scoped map is what the old tab read: it narrows.
    expect(sessionErrors(deriveScopeDoc(m, { task: 2, model: null })).total).toBe(1);
    // The whole-session list is built from the unscoped map, so it does not.
    expect(wholeSessionErrors(m).total).toBe(whole.total);
  });

  it('carries the whole-session seq and the task on each row', () => {
    const rows = wholeSessionErrors(full()).groups.flatMap((g) => g.errors);
    expect(rows.map((r) => [r.seq, r.task]).sort()).toEqual([
      [0, 1],
      [1, 1],
      [2, 2],
      [3, 3],
    ]);
  });
});

describe('errorClickAction', () => {
  const ev = (seq: number, task: number): RawEvent => ({
    kind: 'exec', seq, t: seq * 10, tool: 'Bash', verb: 'ls', err: true, errClass: 'exit_code', task,
  });
  const m = makeMap([ev(0, 1), ev(1, 1), ev(2, 2), ev(3, 3)]);
  const rowFor = (seq: number) =>
    wholeSessionErrors(m).groups.flatMap((g) => g.errors).find((r) => r.seq === seq)!;

  it('seeks in place by whole-session seq when nothing is filtered', () => {
    expect(errorClickAction(rowFor(2), m)).toEqual({ kind: 'seek', index: 2 });
  });

  it('seeks within a task scope by seq, not by the row index', () => {
    const scoped = deriveScopeDoc(m, { task: 2, model: null });
    expect(errorClickAction(rowFor(2), scoped)).toEqual({ kind: 'seek', index: 0 });
  });

  it('jumps to the error\'s task when the scope hides it', () => {
    const scoped = deriveScopeDoc(m, { task: 2, model: null });
    expect(errorClickAction(rowFor(3), scoped)).toEqual({ kind: 'jump', task: 3, seq: 3 });
  });

  it('clears the task filter for a hidden error that has none', () => {
    const scoped = deriveScopeDoc(m, { task: 2, model: null });
    expect(
      errorClickAction({ seq: 3, task: undefined }, scoped)
    ).toEqual({ kind: 'jump', task: null, seq: 3 });
  });
});

describe('taskErrors', () => {
  const err = (seq: number, task: number, verb: string, errClass = 'exit_code'): RawEvent => ({
    kind: 'exec', seq, t: seq * 10, tool: 'Bash', verb, err: true, errClass, task,
  });

  it('counts each task\'s errors and skips tasks with none', () => {
    const m = makeMap([
      err(0, 1, 'pytest'),
      { kind: 'exec', seq: 1, t: 10, tool: 'Bash', verb: 'ls', err: false, task: 1 },
      err(2, 1, 'pytest'),
      { kind: 'exec', seq: 3, t: 30, tool: 'Bash', verb: 'ls', err: false, task: 2 },
      err(4, 3, 'curl'),
    ]);
    const byTask = taskErrors(m);
    expect(byTask.get(1)?.count).toBe(2);
    expect(byTask.has(2)).toBe(false);
    expect(byTask.get(3)).toEqual({ count: 1, firstSeq: 4 });
  });

  it('lands on the row the scoped Errors tab lists first, not the earliest error', () => {
    // A provider-side failure comes first in time, but the Errors tab ranks it
    // last; the tag must land on what the user then sees at the top. Task 2
    // starts later, so scoping to it renumbers `seq` and the tag must still
    // resolve to the same event.
    const m = makeMap([
      err(0, 1, 'ls'),
      { kind: 'user', seq: 1, t: 10, task: 2 },
      err(2, 2, 'claude', 'rate_limit'),
      err(3, 2, 'pytest'),
      err(4, 2, 'pytest'),
    ]);
    const first = taskErrors(m).get(2);
    expect(first?.firstSeq).toBe(3);
    const scopedMap = deriveScopeDoc(m, { task: 2, model: null });
    const top = sessionErrors(scopedMap).groups[0].errors[0];
    expect(top.seq).not.toBe(3); // renumbered by the scope
    expect(srcSeqOf(scopedMap.events[top.index])).toBe(first?.firstSeq);
  });

  it('keeps the whole-session position for an event without a seq', () => {
    const m = makeMap([
      { kind: 'exec', seq: 0, t: 0, tool: 'Bash', verb: 'ls', err: false, task: 1 },
      { kind: 'exec', t: 10, tool: 'Bash', verb: 'pytest', err: true, errClass: 'exit_code', task: 1 },
    ]);
    // makeMap fills `seq` in; take it back out so the fallback is what runs.
    const noSeq = {
      ...m,
      events: m.events.map((e, i) => (i === 1 ? { ...e, seq: undefined } : e)),
    };
    expect(noSeq.events[1].seq).toBeUndefined();
    expect(taskErrors(noSeq).get(1)?.firstSeq).toBe(1);
  });

  it('never trusts a srcSeq the document arrived with', () => {
    const m = makeMap([err(0, 1, 'ls'), err(1, 1, 'pytest')]);
    // A document claiming its pytest failure is event 0: trusted, the tag
    // would land on the `ls` failure instead.
    const raw = {
      ...m,
      events: m.events.map((e, i) => (i === 1 ? { ...e, srcSeq: 0 } : e)),
    };
    const full = stampTasks(withoutSrcSeq(raw));
    expect(full.events.some((e) => e.srcSeq !== undefined)).toBe(false);
    expect(taskErrors(full).get(1)?.count).toBe(2);
  });

  it('a derivation stamps the document\'s own seq, not its position', () => {
    // seq !== index: a document whose numbering starts at 100.
    const m = makeMap([err(0, 1, 'ls'), { kind: 'user', seq: 1, t: 10, task: 2 }, err(2, 2, 'pytest')]);
    const shifted = { ...m, events: m.events.map((e, i) => ({ ...e, seq: 100 + i })) };
    const first = taskErrors(shifted).get(2);
    expect(first?.firstSeq).toBe(102);
    const scoped = deriveScopeDoc(shifted, { task: 2, model: null });
    const i = scoped.events.findIndex((e) => srcSeqOf(e) === first?.firstSeq);
    expect(scoped.events[i]?.verb).toBe('pytest');
  });
});

describe('sessionErrors', () => {
  const errMap = (events: RawEvent[]) => makeMap(events);

  it('groups failures by class and carries the detail a row needs', () => {
    const { groups, total } = sessionErrors(
      errMap([
        { kind: 'exec', seq: 0, t: 0, tool: 'Bash', verb: 'pytest', err: true, errClass: 'exit_code', exitCode: 1,
          targets: [{ label: 'src/app.py' }] },
        { kind: 'mcp', seq: 1, t: 10, server: 'linear', tool: 'search', err: true, errClass: 'timeout' },
        { kind: 'exec', seq: 2, t: 20, tool: 'Bash', verb: 'curl', err: false },
      ])
    );
    expect(total).toBe(2);
    expect(groups.map((g) => g.errClass).sort()).toEqual(['exit_code', 'timeout']);
    const exit = groups.find((g) => g.errClass === 'exit_code');
    expect(exit?.label).toBe('non-zero exit');
    expect(exit?.errors[0]).toMatchObject({
      index: 0,
      label: 'pytest',
      exitCode: 1,
      target: 'src/app.py',
    });
    expect(groups.find((g) => g.errClass === 'timeout')?.errors[0].label).toBe(
      'linear.search'
    );
  });

  // One failure is noise; the same command failing four times running is the
  // signal worth acting on, so it has to outrank a bigger pile of one-offs.
  it('ranks a retry run above a larger pile of unrelated failures', () => {
    const run = [0, 1, 2, 3].map((i) => ({
      kind: 'exec', seq: i, t: i, tool: 'Bash', verb: 'tsc',
      err: true, errClass: 'exit_code',
    }));
    const spread = [4, 5, 6, 7, 8].map((i) => ({
      kind: 'exec', seq: i, t: i, tool: 'Bash', verb: `cmd${i}`,
      err: true, errClass: 'timeout',
    }));
    const { groups } = sessionErrors(errMap([...run, ...spread]));
    expect(groups[0].errClass).toBe('exit_code');
    expect(groups[0].worstRun).toBe(4);
    expect(groups[0].worstRunLabel).toBe('tsc');
    expect(groups[1].worstRun).toBe(1);
  });

  it('is empty for a clean session', () => {
    expect(sessionErrors(errMap([{ kind: 'exec', seq: 0, t: 0, verb: 'ls' }]))).toEqual({
      groups: [],
      total: 0,
    });
  });

  // "other error" was a fifth of real errors and says nothing. Those events
  // still carry a tool category, so use it.
  it('names an unclassified failure after its tool category', () => {
    const { groups } = sessionErrors(
      errMap([
        { kind: 'exec', seq: 0, t: 0, verb: 'aws s3', err: true, errClass: 'other', cat: 'cloud' },
        { kind: 'mcp', seq: 1, t: 1, server: 'linear', tool: 'save', err: true, cat: 'mcp' },
      ])
    );
    expect(groups.map((g) => g.label).sort()).toEqual([
      'Cloud / infra failed',
      'MCP (other) failed',
    ]);
  });

  // Provider failures can outnumber tool failures, so left unranked they
  // bury everything the harness can actually fix.
  it('ranks provider-side failures last however many there are', () => {
    const many = Array.from({ length: 9 }, (_, i) => ({
      kind: 'llm', seq: i, t: i, err: true, errClass: 'api_error',
    }));
    const { groups } = sessionErrors(
      errMap([
        ...many,
        { kind: 'exec', seq: 9, t: 9, verb: 'pytest', err: true, errClass: 'exit_code' },
      ])
    );
    expect(groups.map((g) => [g.errClass, g.provider])).toEqual([
      ['exit_code', false],
      ['api_error', true],
    ]);
  });

  // Every target on a real error event was a bucket ("outside-repo"), so showing
  // it put the same word on every row.
  it('keeps a target that names something and drops a privacy bucket', () => {
    const { groups } = sessionErrors(
      errMap([
        { kind: 'edit', seq: 0, t: 0, tool: 'Edit', err: true, errClass: 'stale_file',
          targets: [{ label: 'src/app.ts', scope: 'repo' }] },
        { kind: 'exec', seq: 1, t: 1, verb: 'cat', err: true, errClass: 'stale_file', dur: 1200,
          targets: [{ label: 'outside-repo', scope: 'outside' }] },
      ])
    );
    const errs = groups[0].errors;
    expect(errs[0].target).toBe('src/app.ts');
    expect(errs[1].target).toBeUndefined();
    expect(errs[1].durMs).toBe(1200);
  });
});

describe('sidebar', () => {
  it('shows Tasks only with >1 task and Subagents only with any', () => {
    expect(sidebarTabs(1, 0)).toEqual(['insights']);
    expect(sidebarTabs(2, 0)).toEqual(['tasks', 'insights']);
    expect(sidebarTabs(1, 3)).toEqual(['subagents', 'insights']);
    expect(sidebarTabs(4, 3)).toEqual(['tasks', 'subagents', 'insights']);
  });

  it('shows Errors only when the session has any', () => {
    expect(sidebarTabs(1, 0, 0)).toEqual(['insights']);
    expect(sidebarTabs(1, 0, 2)).toEqual(['errors', 'insights']);
    expect(sidebarTabs(4, 3, 2)).toEqual([
      'tasks',
      'errors',
      'subagents',
      'insights',
    ]);
  });

  it('resolves an unavailable tab to the first available', () => {
    expect(resolveSidebarTab('tasks', ['subagents', 'insights'])).toBe(
      'subagents'
    );
    expect(resolveSidebarTab(null, ['tasks', 'insights'])).toBe('tasks');
    expect(resolveSidebarTab('insights', ['tasks', 'insights'])).toBe(
      'insights'
    );
  });

  it('persists the collapsed state and survives broken storage', () => {
    const mem = new Map<string, string>();
    const storage = {
      getItem: (k: string) => mem.get(k) ?? null,
      setItem: (k: string, v: string) => void mem.set(k, v),
    };
    expect(readSidebarCollapsed(storage)).toBe(false);
    writeSidebarCollapsed(true, storage);
    expect(mem.get(SIDEBAR_COLLAPSED_KEY)).toBe('1');
    expect(readSidebarCollapsed(storage)).toBe(true);
    const broken = {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('blocked');
      },
    };
    expect(readSidebarCollapsed(broken)).toBe(false);
    expect(() => writeSidebarCollapsed(true, broken)).not.toThrow();
    expect(readSidebarCollapsed(null)).toBe(false);
  });
});

describe('spanBarGeometry', () => {
  it('keeps a one-event helper at the session end inside the track', () => {
    const { left, width } = spanBarGeometry(10_000, 10_000, 10_000);
    expect(width).toBeCloseTo(0.015);
    expect(left + width).toBeLessThanOrEqual(1);
    expect(left).toBeCloseTo(0.985);
  });

  it('places a normal span proportionally', () => {
    expect(spanBarGeometry(2_000, 5_000, 10_000)).toEqual({
      left: 0.2,
      width: 0.3,
    });
  });

  it('survives a zero-length session', () => {
    const g = spanBarGeometry(0, 0, 0);
    expect(g.left).toBe(0);
    expect(g.width).toBeCloseTo(0.015);
    expect(g.left + g.width).toBeLessThanOrEqual(1);
  });
});

describe('sidebar collapsed store', () => {
  it('notifies subscribers and reads back the value', () => {
    let calls = 0;
    const unsub = subscribeSidebarCollapsed(() => {
      calls += 1;
    });
    setSidebarCollapsed(true);
    expect(getSidebarCollapsed()).toBe(true);
    setSidebarCollapsed(false);
    expect(getSidebarCollapsed()).toBe(false);
    expect(calls).toBe(2);
    unsub();
    setSidebarCollapsed(true);
    expect(calls).toBe(2);
    setSidebarCollapsed(false);
  });
});

describe('fit-viewport header', () => {
  const pr = (number: number, repo?: string, state?: string): SessionMapPr => ({
    provider: 'github',
    repo,
    number,
    url: `https://github.com/o/${repo ?? 'r'}/pull/${number}`,
    seq: number,
    state,
  });

  it('prMenuItems lists each PR once, in map order', () => {
    const items = prMenuItems([
      pr(12, 'dashboard', 'merged'),
      pr(7),
      pr(12, 'dashboard', 'open'),
      pr(12, 'backend'),
    ]);
    expect(items.map((i) => i.label)).toEqual([
      'dashboard #12',
      '#7',
      'backend #12',
    ]);
    expect(items[0]).toMatchObject({
      provider: 'github',
      state: 'merged',
      url: 'https://github.com/o/dashboard/pull/12',
    });
    expect(items[1].state).toBeNull();
    expect(prMenuItems([])).toEqual([]);
  });

  it('prMenuItems drops a PR url that is not http(s)', () => {
    const bad = (url: string) => ({ ...pr(1), url });
    expect(prMenuItems([bad('javascript:alert(1)')])[0].url).toBeNull();
    expect(prMenuItems([bad('data:text/html,<script>x</script>')])[0].url).toBeNull();
    expect(prMenuItems([bad('not a url')])[0].url).toBeNull();
    expect(safeLinkUrl('http://git.example/pr/1')).toBe('http://git.example/pr/1');
    expect(safeLinkUrl(' JavaScript:alert(1)')).toBeNull();
  });

  it('prCountLabel pluralises', () => {
    expect(prCountLabel(1)).toBe('1 PR');
    expect(prCountLabel(9)).toBe('9 PRs');
  });

  it('outcomeParts: the non-zero outputs, never the task count', () => {
    expect(outcomeParts(map)).toEqual(['1 commit']);
    // A session that produced nothing gets no callout, rather than one
    // restating the `Tasks` row above it.
    expect(outcomeParts(makeMap([{ kind: 'user' }]))).toEqual([]);
  });
});

describe('fit-viewport toolbar labels', () => {
  const tasks = sessionTasks(map).tasks;

  it('taskFilterLabel names the picked prompt', () => {
    expect(taskFilterLabel(tasks, null)).toBe('All prompts');
    expect(taskFilterLabel(tasks, 2)).toBe(`2 · ${tasks[1].name}`);
    expect(taskFilterLabel(tasks, 99)).toBe('All prompts');
    // A real segmenter's windows are tasks.
    expect(
      taskFilterLabel(
        tasks.map((t) => ({ ...t, source: 'segments' as const })),
        null
      )
    ).toBe('All tasks');
  });

  it('hasScopeFilter: any filter or an open subagent', () => {
    const none = { task: null, model: null };
    expect(hasScopeFilter(none, null)).toBe(false);
    expect(hasScopeFilter({ ...none, task: 1 }, null)).toBe(true);
    expect(hasScopeFilter({ ...none, model: 'opus' }, null)).toBe(true);
    expect(hasScopeFilter(none, 'subA')).toBe(true);
  });

  it('agentOptions: the whole session, then each subagent', () => {
    const opts = agentOptions(map, subagentOrdinal(map));
    expect(opts.map((o) => o.id)).toEqual(['main', 'subA', 'subB', 'ghost']);
    expect(opts[0].label).toBe('All agents');
    expect(opts[1].label).toMatch(/Explore/);
    const solo = makeMap([{ kind: 'user' }]);
    expect(
      agentOptions({ ...solo, agents: solo.agents.slice(0, 1) }, new Map())
    ).toEqual([]);
  });

  it('agentOptionsInScope: counts inside the prompt/model scope, empty rows flagged', () => {
    const all = agentOptions(map, subagentOrdinal(map));
    const inTask = (task: number | null, model: string | null = null) =>
      agentOptionsInScope(all, deriveScopeDoc(map, { task, model }));
    const emptyIds = (o: ReturnType<typeof inTask>) =>
      o.filter((a) => a.empty).map((a) => a.id);
    expect(emptyIds(inTask(null))).toEqual(['ghost']);
    expect(emptyIds(inTask(1))).toEqual(['subB', 'ghost']);
    expect(emptyIds(inTask(2))).toEqual(['subA', 'ghost']);
    expect(inTask(1)[1].calls).toBeGreaterThan(0);
    expect(inTask(2)[1].calls).toBe(0);
    expect(inTask(2)[1].cost).toBe(0);
    // a prompt x model pair with nothing in it empties every row
    expect(emptyIds(inTask(1, 'nope'))).toEqual(['main', 'subA', 'subB', 'ghost']);
  });

  it('emptyScopeDescription: names only the active filters', () => {
    const noun = { one: 'Prompt' };
    const d = (task: number | null, model: string | null, sub: string | null) =>
      emptyScopeDescription({ task, model }, sub, noun);
    expect(d(1, null, null)).toBe(
      'No events match the selected prompt. Clear a filter above.'
    );
    expect(d(null, 'm', 'a')).toBe(
      'No events match the selected model and agent. Clear a filter above.'
    );
    expect(d(1, 'm', 'a')).toBe(
      'No events match the selected prompt, model and agent. Clear a filter above.'
    );
    expect(emptyScopeDescription({ task: 1, model: null }, null, { one: 'Task' })).toBe(
      'No events match the selected task. Clear a filter above.'
    );
  });
});

describe('fit-viewport side column', () => {
  const tasks = sessionTasks(map).tasks;

  it('detailsTitle', () => {
    const base = {
      inspector: false,
      selected: undefined,
      taskCount: 2,
      noun: { one: 'Prompt' },
    };
    expect(detailsTitle({ ...base, scoped: false })).toBe('Whole session');
    expect(detailsTitle({ ...base, scoped: true })).toBe('Filtered session');
    expect(detailsTitle({ ...base, selected: tasks[1], scoped: true })).toBe(
      'Prompt 2 of 2'
    );
    expect(
      detailsTitle({
        ...base,
        noun: { one: 'Task' },
        selected: tasks[1],
        scoped: true,
      })
    ).toBe('Task 2 of 2');
    expect(
      detailsTitle({
        ...base,
        inspector: true,
        selected: tasks[1],
        scoped: true,
      })
    ).toBe('Subagent');
  });

  it('promptLine', () => {
    const z = { promptChars: 30, promptImages: 0, promptUrls: 0 };
    expect(promptLine(z)).toBe('30 chars');
    expect(promptLine({ ...z, promptImages: 1, promptUrls: 2 })).toBe(
      '30 chars · 1 image · 2 URLs'
    );
  });

  it('sidebarRailLabel', () => {
    const base = {
      subLabel: null,
      mode: 'session' as const,
      sessionId: 'local:claude-code:abcd1234-5678',
      taskCount: 1,
      noun: { one: 'Prompt', many: 'Prompts' },
    };
    expect(sidebarRailLabel(base)).toBe('abcd1234 · 1 prompt');
    expect(sidebarRailLabel({ ...base, taskCount: 17 })).toBe(
      'abcd1234 · 17 prompts'
    );
    expect(
      sidebarRailLabel({
        ...base,
        taskCount: 17,
        noun: { one: 'Task', many: 'Tasks' },
      })
    ).toBe('abcd1234 · 17 tasks');
    expect(sidebarRailLabel({ ...base, mode: 'list' })).toBe('Sessions');
    expect(sidebarRailLabel({ ...base, subLabel: 'Sub 2: Explore' })).toBe(
      '↳ Sub 2: Explore'
    );
  });

  it('groupSessionsByDay keeps list order and runs', () => {
    const s = (id: string, startedAt: string, sessionDate?: string) =>
      ({ sessionId: id, startedAt, sessionDate }) as SessionExplorerSession;
    const groups = groupSessionsByDay([
      s('a', '2026-09-23T10:00:00Z'),
      s('b', '2026-09-23T08:00:00Z'),
      s('c', '2026-09-22T23:00:00Z', '2026-09-23'),
      s('d', '2026-09-21T09:00:00Z'),
    ]);
    expect(groups.map((g) => [g.day, g.items.map((i) => i.sessionId)])).toEqual(
      [
        ['2026-09-23', ['a', 'b', 'c']],
        ['2026-09-21', ['d']],
      ]
    );
  });
});

describe('event categories', () => {
  const m = makeMap([
    { kind: 'user', chars: 40 }, // 0 prompt
    { kind: 'read', targets: [{ file: 0, touch: 'read' }] }, // 1 explore
    { kind: 'exec', err: true }, // 2 tool + error
    { kind: 'delay', ms: 90_000 }, // 3 delay
    { kind: 'commit', sha: 'abc1234' }, // 4 out
    { kind: 'compaction', preTokens: 9, postTokens: 1 }, // 5 delay
    { kind: 'error', source: 'llm', err: true }, // 6 error
    { kind: 'edit', vcs: { provider: 'github', action: 'pr_create' } }, // 7 out + edit
    { kind: 'user', chars: 5 }, // 8 prompt
    { kind: 'mcp', server: 'linear', tool: 'get_issue', err: true }, // 9 mcp + error
    { kind: 'skill', name: 'deploy' }, // 10 skill
    { kind: 'llm', model: 'opus-5.5', cost: 0.4, in: 900, out: 100 }, // 11 model, cost, tokens
    { kind: 'llm', model: 'opus-5.5', cost: 0 }, // 12 model only
    { kind: 'model_switch', from: 'opus-5.5', to: 'sonnet-5' }, // 13 model
    { kind: 'search' }, // 14 explore
    { kind: 'web' }, // 15 tool
    { kind: 'subagent', child: 'subA', subagentType: 'Explore' }, // 16 tool
    { kind: 'edit', targets: [{ file: 1, touch: 'edit' }] }, // 17 edit
    { kind: 'exec', cat: 'mcp' }, // 18 mcp by category
  ]);
  const ev = m.events;

  it('gives every event one primary category', () => {
    expect(ev.map(categoryOf)).toEqual([
      'prompt',
      'explore',
      'tool',
      'delay',
      'out',
      'delay',
      'error',
      'out',
      'prompt',
      'mcp',
      'skill',
      'model',
      'model',
      'model',
      'explore',
      'tool',
      'tool',
      'edit',
      'mcp',
    ]);
  });

  it('lets one event belong to several categories', () => {
    const cats = (i: number) =>
      CATEGORIES.filter((c) => categoryMatches(ev[i], c));
    expect(cats(2)).toEqual(['error', 'tool']);
    expect(cats(7)).toEqual(['out', 'edit']);
    expect(cats(9)).toEqual(['error', 'mcp']);
    expect(cats(11)).toEqual(['model', 'cost', 'tokens']);
    expect(cats(12)).toEqual(['model']);
    expect(cats(18)).toEqual(['mcp']);
    expect(cats(16)).toEqual(['tool']);
  });

  it('colours by the first selected category an event is in', () => {
    const errors = new Set<Category>(['error']);
    expect(displayCategory(ev[9], errors)).toBe('error');
    expect(displayCategory(ev[9], new Set(['mcp']))).toBe('mcp');
    expect(displayCategory(ev[9], new Set(['prompt']))).toBe('mcp');
    expect(displayCategory(ev[11], new Set(['cost']))).toBe('cost');
  });

  it('counts a PR-opening exec as an Output, not also as Tools', () => {
    const [prOpen, plainExec] = makeMap([
      { kind: 'exec', vcs: { provider: 'github', action: 'pr_create' } },
      { kind: 'exec' },
    ]).events;
    expect(categoryMatches(prOpen, 'out')).toBe(true);
    expect(categoryMatches(prOpen, 'tool')).toBe(false);
    expect(categoryMatches(plainExec, 'tool')).toBe(true);
  });

  it('starts on Prompts + Errors, and knows when a selection narrows', () => {
    expect([...DEFAULT_CATEGORIES]).toEqual(['prompt', 'error']);
    expect(isDefaultCategories(new Set(DEFAULT_CATEGORIES))).toBe(true);
    expect(narrowsView(new Set(DEFAULT_CATEGORIES))).toBe(false);
    expect(narrowsView(new Set(CATEGORIES))).toBe(false);
    expect(narrowsView(new Set(['edit']))).toBe(true);
    // An empty selection is "no filter active" (every chip deselected),
    // the same as picking every chip -- not "filter to nothing". ev=none
    // round-trips it through the URL as exactly that.
    expect(narrowsView(new Set())).toBe(false);
  });

  it('lists indices by category in time order, optionally lens-limited', () => {
    expect(categoryIndices(ev, 'prompt')).toEqual([0, 8]);
    expect(categoryIndices(ev, 'delay')).toEqual([3, 5]);
    expect(categoryIndices(ev, 'out')).toEqual([4, 7]);
    expect(categoryIndices(ev, 'error')).toEqual([2, 6, 9]);
    expect(categoryIndices(ev, 'edit', (i) => i > 10)).toEqual([17]);
  });

  it('toggles a category without mutating the set, allowing an empty set', () => {
    const on = new Set<Category>(['prompt']);
    const off = toggleCategory(on, 'prompt');
    expect([...off]).toEqual([]);
    expect([...on]).toEqual(['prompt']);
    expect([...toggleCategory(off, 'delay')]).toEqual(['delay']);
  });

  it('knows the last chip clicked, not the last in CATEGORIES order', () => {
    const base = new Set<Category>(['prompt', 'error']);
    // click MCP, then Errors off and on again: Errors is the latest pick,
    // though the URL rebuilds the Set as prompt, error, mcp
    let h = pickHistory(
      [],
      base,
      new Set<Category>(['prompt', 'error', 'mcp'])
    );
    h = pickHistory(
      h,
      new Set<Category>(['prompt', 'error', 'mcp']),
      new Set<Category>(['prompt', 'mcp'])
    );
    const now = new Set<Category>(['prompt', 'error', 'mcp']);
    h = pickHistory(h, new Set<Category>(['prompt', 'mcp']), now);
    expect(h).toEqual(['mcp', 'error']);
    expect(lastPicked(h, now)).toBe('error');
    // Tools clicked last: an eligible-only lookup keeps the previous pick
    const withTools = new Set<Category>([...now, 'tool']);
    const h2 = pickHistory(h, now, withTools);
    expect(lastPicked(h2, withTools)).toBe('tool');
    expect(lastPicked(h2, withTools, ['error', 'mcp', 'skill', 'model'])).toBe(
      'error'
    );
    // nothing clicked yet (URL / default): CATEGORIES order stands in
    expect(lastPicked([], base, ['error', 'mcp'])).toBe('error');
    expect(lastPicked([], new Set(), ['error'])).toBeNull();
  });

  it('sizes marks by Cost, else Tokens (Cost wins when both are on)', () => {
    expect(sizeMetric(new Set(['cost', 'tokens']))).toBe('cost');
    expect(sizeMetric(new Set(['tokens', 'model']))).toBe('tokens');
    expect(sizeMetric(new Set(['model']))).toBeNull();
    // square-root scaled, floored for anything above zero, capped at 1
    expect(magnitude(1, 1)).toBe(1);
    expect(magnitude(0.25, 1)).toBeCloseTo(0.5);
    expect(magnitude(0.0001, 1)).toBe(0.12);
    expect(magnitude(0, 1)).toBe(0);
    expect(magnitude(5, 1)).toBe(1);
    expect(magnitude(1, 0)).toBe(0);
    expect(magnitude(Number.NaN, 1)).toBe(0);
  });
});

describe('event categories: URL', () => {
  it('parses ev=, drops junk, and round-trips', () => {
    expect([...parseCategories(null)]).toEqual(DEFAULT_CATEGORIES);
    expect([...parseCategories('edit,bogus,mcp')]).toEqual(['edit', 'mcp']);
    expect([...parseCategories('none')]).toEqual([]);
    expect([...parseCategories('toString')]).toEqual(DEFAULT_CATEGORIES);
    const s = new Set<Category>(['cost', 'edit']);
    expect(serializeCategories(s)).toBe('edit,cost');
    expect(parseCategories(serializeCategories(s))).toEqual(s);
    expect(serializeCategories(new Set(DEFAULT_CATEGORIES))).toBeNull();
    expect(serializeCategories(new Set())).toBe('none');
  });
});

describe('event categories: map focus', () => {
  const m = makeMap([
    { kind: 'user' }, // 0
    { kind: 'read', targets: [{ file: 0, touch: 'read' }] }, // 1
    { kind: 'edit', targets: [{ file: 1, touch: 'edit' }] }, // 2
    { kind: 'delay', ms: 5000 }, // 3
  ]);
  const tree = buildTree(m);
  const refs = eventNodeRefs(m, tree);
  const fileNode = (f: number) =>
    [...tree.byId.values()].find((n) => n.id === `file:${f}`);

  it('is off for the default and the full selection', () => {
    expect(
      mapFocus(tree, refs, m.events, new Set(DEFAULT_CATEGORIES))
    ).toBeNull();
    expect(mapFocus(tree, refs, m.events, new Set(CATEGORIES))).toBeNull();
  });

  it('lights the touched nodes and their branch only', () => {
    const f = mapFocus(tree, refs, m.events, new Set(['edit']));
    expect(f).not.toBeNull();
    expect(f?.has(fileNode(1)?.id ?? '')).toBe(true);
    expect(f?.has(fileNode(0)?.id ?? '')).toBe(false);
    expect(f?.has('root')).toBe(true);
    const x = mapFocus(tree, refs, m.events, new Set(['explore']));
    expect(x?.has(fileNode(0)?.id ?? '')).toBe(true);
    expect(x?.has(fileNode(1)?.id ?? '')).toBe(false);
  });

  it('stays off when nothing selected touches the map', () => {
    expect(mapFocus(tree, refs, m.events, new Set(['delay']))).toBeNull();
  });

  it('pathFocus lights only the cited steps and their branch', () => {
    const f = pathFocus(tree, refs, m.events, [2]);
    expect(f?.has(fileNode(1)?.id ?? '')).toBe(true);
    expect(f?.has(fileNode(0)?.id ?? '')).toBe(false);
    expect(f?.has('root')).toBe(true);
  });

  it('pathFocus reads seq, not the index into events', () => {
    // Events dropped by a scope shift every index; `seq` survives that, and
    // a comment cites `seq`.
    const scoped = { ...m, events: m.events.slice(2) };
    const scopedRefs = eventNodeRefs(scoped, tree);
    const f = pathFocus(tree, scopedRefs, scoped.events, [2]);
    expect(f?.has(fileNode(1)?.id ?? '')).toBe(true);
  });

  it('pathFocus is null when no cited step is in this scope', () => {
    expect(pathFocus(tree, refs, m.events, [99])).toBeNull();
    expect(pathFocus(tree, refs, m.events, [])).toBeNull();
    // A step with no map node of its own (a delay) dims nothing.
    expect(pathFocus(tree, refs, m.events, [3])).toBeNull();
  });

  // The chips narrow the map twice. `pathFocus` winning the
  // focus set is not enough, because `activeRefs` blanks the refs of every
  // event outside the selection, and that is what the active dots and the
  // labels are built from. A path step in an unchecked category went dark.
  it('a lit path survives a chip selection that excludes its steps', () => {
    const chips = new Set<Category>(['edit']); // Exploration unchecked
    const step = 1; // ...and step #1 is a Read
    expect(categoryOf(m.events[step])).toBe('explore');

    // The focus set lights it either way.
    expect(
      pathFocus(tree, refs, m.events, [step])?.has(fileNode(0)?.id ?? '')
    ).toBe(true);

    // But the refs the map draws from do not, while the chips still narrow.
    expect(activeRefs(refs, m.events, chips)[step]).toEqual([]);

    // With a path lit, the chips are suspended and the whole path is drawn.
    const lit = activeRefs(refs, m.events, mapChipSelection(chips, true));
    expect(lit[step]).toEqual(refs[step]);
    expect(lit[2]).toEqual(refs[2]); // the Edit the chips did select, still on

    // With no path lit, the chips narrow exactly as before.
    expect(activeRefs(refs, m.events, mapChipSelection(chips, false))).toEqual(
      activeRefs(refs, m.events, chips)
    );
  });
});

/**
 * A scope reindexes `seq` over the events it kept, so a
 * causal path cited against the full session either found nothing or -- worse
 * -- found whatever event had landed on that number and lit unrelated nodes.
 */
describe('map focus: a causal path under a scope', () => {
  // file 0 is src/proxy.ts, file 1 is src/app.ts.
  const full = makeMap([
    { kind: 'user' }, // 0, task 1
    { kind: 'edit', targets: [{ file: 1, touch: 'edit' }] }, // 1, task 1
    { kind: 'user' }, // 2, task 2
    { kind: 'edit', targets: [{ file: 0, touch: 'edit' }] }, // 3, task 2
    { kind: 'edit', targets: [{ file: 1, touch: 'edit' }] }, // 4, task 2
  ]);
  const scoped = deriveScopeDoc(full, { task: 2, model: null });
  const tree = buildTree(scoped);
  const refs = eventNodeRefs(scoped, tree);
  const node = (f: number) =>
    [...tree.byId.values()].find((n) => n.id === `file:${f}`)?.id ?? '';

  it('renumbers seq but keeps the full session\'s number on srcSeq', () => {
    expect(scoped.events.map((e) => e.seq)).toEqual([0, 1, 2]);
    expect(scoped.events.map(srcSeqOf)).toEqual([2, 3, 4]);
  });

  it('lights the cited step, not the one now sitting on that number', () => {
    // #4 edits src/app.ts. It is the scoped map's event 2, whose renumbered
    // `seq` is 2 -- and the event that now holds seq 1 edits src/proxy.ts.
    const f = pathFocus(tree, refs, scoped.events, [4]);
    expect(f?.has(node(1))).toBe(true);
    expect(f?.has(node(0))).toBe(false);
  });

  it('lights nothing for a step the scope dropped', () => {
    // #1 edits src/app.ts but belongs to task 1. Read against the renumbered
    // `seq` it would have lit src/proxy.ts, which it never touched.
    expect(pathFocus(tree, refs, scoped.events, [1])).toBeNull();
  });

  it('is unchanged on the unscoped map', () => {
    const fullTree = buildTree(full);
    const fullRefs = eventNodeRefs(full, fullTree);
    const f = pathFocus(fullTree, fullRefs, full.events, [4]);
    expect(f?.has(node(1))).toBe(true);
    expect(f?.has(node(0))).toBe(false);
  });
});

describe('event categories: activeRefs', () => {
  it('is a no-op (same reference) when the selection does not narrow', () => {
    const refs: NodeRef[][] = [[{ id: 'a' }], [{ id: 'b' }]];
    const events = [{ kind: 'edit' }, { kind: 'mcp' }] as SessionMapEvent[];
    expect(activeRefs(refs, events, new Set(DEFAULT_CATEGORIES))).toBe(refs);
    expect(activeRefs(refs, events, new Set(CATEGORIES))).toBe(refs);
  });

  it('blanks refs for events outside the selection', () => {
    const refs: NodeRef[][] = [[{ id: 'a' }], [{ id: 'b' }], [{ id: 'c' }]];
    const events = [
      { kind: 'edit' },
      { kind: 'mcp' },
      { kind: 'read' },
    ] as SessionMapEvent[];
    const scoped = activeRefs(refs, events, new Set(['edit', 'explore']));
    expect(scoped).toEqual([[{ id: 'a' }], [], [{ id: 'c' }]]);
  });

  it("makes a kept event's passive refs active (the model under Model / Cost / Tokens)", () => {
    const model = { id: 'model:opus', passive: true };
    const refs: NodeRef[][] = [[model], [{ id: 'file:0' }]];
    const events = [
      { kind: 'llm', model: 'opus', in: 10, out: 5, cost: 0.2 },
      { kind: 'edit' },
    ] as SessionMapEvent[];
    const scoped = activeRefs(refs, events, new Set(['tokens']));
    expect(scoped).toEqual([[{ id: 'model:opus', passive: false }], []]);
    // the caller's refs are not mutated
    expect(refs[0][0].passive).toBe(true);
    expect(activeSet(scoped, 1, 20).has('model:opus')).toBe(true);
  });

  it("keeps a matched event's passive model ref passive outside Model / Cost / Tokens", () => {
    // An errored LLM, selected only through Errors: it wasn't the model the
    // user asked to see, just incidental to the error that was.
    const model = { id: 'model:opus', passive: true };
    const errClass = { id: 'errc:llm/timeout' };
    const refs: NodeRef[][] = [[model, errClass]];
    const events = [
      { kind: 'llm', model: 'opus', err: true, cost: 0.2 },
    ] as SessionMapEvent[];
    // a fresh literal, not `model` itself: an `activeRefs` regressed to
    // mutating its input would make `model` and the expected value the
    // same already-mutated object, and this assertion would pass either way
    expect(activeRefs(refs, events, new Set(['error']))).toEqual([
      [{ id: 'model:opus', passive: true }, errClass],
    ]);
    expect(model.passive).toBe(true);
    // the same event, with Model also selected: now it activates
    expect(activeRefs(refs, events, new Set(['error', 'model']))).toEqual([
      [{ id: 'model:opus', passive: false }, errClass],
    ]);
  });

  it("a burst of unselected activity doesn't crowd a selected touch out of the recent-N window", () => {
    // The bug: with Edits selected, 25 unrelated
    // MCP calls right before the playhead pushed the one edited file out
    // of activeSet's recent-20 budget, so its label never showed and
    // stopped tracking the playhead. Scoping refs by the selection first
    // means only matching touches consume that budget.
    const events: SessionMapEvent[] = [
      {
        kind: 'edit',
        targets: [{ file: 0, touch: 'edit' }],
      } as SessionMapEvent,
      // 25 DISTINCT mcp targets -- identical calls collapse to one node in
      // activeSet's distinct-id budget, so only distinct ones can crowd it.
      ...Array.from(
        { length: 25 },
        (_, i) =>
          ({ kind: 'mcp', server: 's', tool: `t${i}` }) as SessionMapEvent
      ),
    ];
    const m = makeMap(events);
    const tree = buildTree(m);
    const refs = eventNodeRefs(m, tree);
    const p = refs.length - 1;
    const selected = new Set<Category>(['edit']);

    const editedId = [...tree.byId.values()].find((n) => n.id === 'file:0')?.id;
    expect(editedId).toBeDefined();

    expect(activeSet(refs, p, 20).has(editedId!)).toBe(false); // the bug
    const scoped = activeRefs(refs, m.events, selected);
    expect(activeSet(scoped, p, 20).has(editedId!)).toBe(true); // the fix
  });
});

describe('timeline dock: stepping', () => {
  const list = [2, 5, 9];

  it('steps from the pinned event, clamped at both ends', () => {
    expect(stepEvent(list, 5, 0, 1)).toBe(9);
    expect(stepEvent(list, 5, 0, -1)).toBe(2);
    expect(stepEvent(list, 9, 0, 1)).toBe(9);
    expect(stepEvent(list, 2, 0, -1)).toBe(2);
  });

  it('with nothing pinned, steps from the playhead and wraps', () => {
    expect(stepEvent(list, null, 5, 1)).toBe(9);
    expect(stepEvent(list, null, 5, -1)).toBe(2);
    expect(stepEvent(list, null, 9, 1)).toBe(2); // past the last: wrap
    expect(stepEvent(list, null, 2, -1)).toBe(9); // before the first: wrap
    // a pin of another type counts as nothing pinned in this list
    expect(stepEvent(list, 4, 4, 1)).toBe(5);
  });

  it('returns null for an empty list', () => {
    expect(stepEvent([], null, 3, 1)).toBeNull();
    expect(stepEvent([], 3, 3, -1)).toBeNull();
  });

  it('reports k/N only while the pinned event is in the list', () => {
    expect(stepPosition(list, 5)).toBe('2/3');
    expect(stepPosition(list, 4)).toBeNull();
    expect(stepPosition(list, null)).toBeNull();
  });
});

describe('timeline dock: subagent rows', () => {
  it('keeps the start order and labels rows "n · type"', () => {
    const m = makeMap(EVENTS);
    const subs = orderedSubagents(m, subagentOrdinal(m));
    const rows = subagentLanes(subs, m.events);
    expect(rows.map((r) => r.id)).toEqual(subs.map((s) => s.id));
    expect(rows[0].label).toBe(`1 · ${subs[0].type}`);
    // a subagent with no events in the timeline's map gets no row
    const onlyMain = m.events.filter((e) => e.agent === 'main');
    expect(subagentLanes(subs, onlyMain)).toEqual([]);
  });
});

describe('timeline dock: height', () => {
  it('clamps a dragged height to [min, 72% of the explorer]', () => {
    expect(clampDockHeight(50, 900)).toBe(DOCK_MIN_HEIGHT);
    expect(clampDockHeight(300.4, 900)).toBe(300);
    expect(clampDockHeight(2000, 900)).toBe(648);
    // a tiny explorer never clamps below the minimum
    expect(clampDockHeight(300, 100)).toBe(DOCK_MIN_HEIGHT);
  });

  it("measures the optional lanes' height over the always-on ones", () => {
    const all = new Set(['tasks', 'events', 'ctx', 'main', 'subs'] as const);
    const subs = [
      { id: 'a', label: '1 · fork' },
      { id: 'b', label: '2 · fork' },
    ];
    // Context 32+4, Subagents 16+4, and each expanded row 12+2
    expect(optionalLanesHeight(all, subs, true)).toBe(84);
    expect(optionalLanesHeight(all, subs, false)).toBe(56);
    expect(
      optionalLanesHeight(
        new Set(['tasks', 'events', 'main'] as const),
        subs,
        true
      )
    ).toBe(0);
  });

  it('extends the page by what the optional lanes add past any dragged floor', () => {
    // no floor: all of it (so the map keeps its size)
    expect(dockOverflow(300, 84, null)).toBe(84);
    // a floor under the base height reserves nothing
    expect(dockOverflow(300, 84, 176)).toBe(84);
    // a floor that already covers part of the lanes
    expect(dockOverflow(300, 84, 280)).toBe(20);
    // a floor taller than every lane: the dock never grows
    expect(dockOverflow(320, 84, 320)).toBe(0);
    expect(dockOverflow(216, 0, null)).toBe(0);
  });

  it('persists through guarded storage; junk and resets read as null', () => {
    const mem = new Map<string, string>();
    const storage = {
      getItem: (k: string) => mem.get(k) ?? null,
      setItem: (k: string, v: string) => void mem.set(k, v),
    };
    expect(readDockHeight(storage)).toBeNull();
    writeDockHeight(260, storage);
    expect(mem.get(DOCK_HEIGHT_KEY)).toBe('260');
    expect(readDockHeight(storage)).toBe(260);
    writeDockHeight(null, storage);
    expect(readDockHeight(storage)).toBeNull();
    mem.set(DOCK_HEIGHT_KEY, 'tall');
    expect(readDockHeight(storage)).toBeNull();
    mem.set(DOCK_HEIGHT_KEY, '12');
    expect(readDockHeight(storage)).toBeNull(); // under the minimum
    const broken = {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('blocked');
      },
    };
    expect(readDockHeight(broken)).toBeNull();
    expect(() => writeDockHeight(300, broken)).not.toThrow();
    expect(readDockHeight(null)).toBeNull();
  });

  it('notifies subscribers; a mid-drag set is not persisted', () => {
    let calls = 0;
    const unsub = subscribeDockHeight(() => {
      calls += 1;
    });
    // An in-memory storage (this env's jsdom localStorage isn't usable).
    const store = new Map<string, string>([[DOCK_HEIGHT_KEY, '240']]);
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    });
    setDockHeight(300, false);
    expect(getDockHeight()).toBe(300);
    // mid-drag: memory moves, storage keeps the last persisted height
    expect(store.get(DOCK_HEIGHT_KEY)).toBe('240');
    vi.unstubAllGlobals();
    setDockHeight(null);
    expect(getDockHeight()).toBeNull();
    expect(calls).toBe(2);
    unsub();
    setDockHeight(320);
    expect(calls).toBe(2);
    setDockHeight(null);
  });
});

describe('sessionErrors for failed hooks', () => {
  const hook = (seq: number, name: string, errClass = 'hook_error'): RawEvent => ({
    kind: 'error', seq, t: seq, err: true, source: 'hook', errClass, hook: name,
  });

  it('names each hook in its group and in the burst label', () => {
    const { groups } = sessionErrors(
      makeMap([
        hook(0, 'PostToolUse:Bash'),
        hook(1, 'PostToolUse:Bash'),
        hook(2, 'PostToolUse:Bash'),
        hook(3, 'Stop', 'hook_blocking'),
      ])
    );
    expect(groups.map((g) => g.label).sort()).toEqual([
      'PostToolUse:Bash hook error',
      'Stop hook blocked',
    ]);
    const post = groups.find((g) => g.label === 'PostToolUse:Bash hook error');
    expect(post?.errors).toHaveLength(3);
    expect(post?.errors[0].label).toBe('PostToolUse:Bash hook error');
    expect(post?.worstRun).toBe(3);
    expect(post?.worstRunLabel).toBe('PostToolUse:Bash hook error');
  });
});

describe('axisTickLabels', () => {
  const start = '2026-10-21T07:04:00Z';
  const H = 3_600_000;

  it('is plain HH:MM within the start day', () => {
    expect(axisTickLabels(start, [0, H, 3 * H])).toEqual([
      '07:04',
      '08:04',
      '10:04',
    ]);
  });

  it('dates only the first tick on each new UTC day', () => {
    // A 26.9h session: the axis used to read 07:04 08:04 02:22 07:46 09:58.
    expect(
      axisTickLabels(start, [0, H, 19.3 * H, 24.7 * H, 26.9 * H])
    ).toEqual(['07:04', '08:04', 'Oct 22 02:22', '07:46', '09:58']);
  });

  it('dates every tick that starts another day, across many days', () => {
    expect(axisTickLabels(start, [0, 30 * H, 60 * H, 250 * H])).toEqual([
      '07:04',
      'Oct 22 13:04',
      'Oct 23 19:04',
      'Oct 31 17:04',
    ]);
  });

  it('flips at midnight, not after 24h', () => {
    const late = '2026-10-21T23:50:00Z';
    expect(axisTickLabels(late, [0, 9 * 60_000, 11 * 60_000])).toEqual([
      '23:50',
      '23:59',
      'Oct 22 00:01',
    ]);
  });

  it('falls back to bare clocks for an unparseable start', () => {
    expect(axisTickLabels('not a date', [0, 30 * H])).toEqual([
      '--:--',
      '--:--',
    ]);
  });
});
