import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  type KeyboardEvent,
} from 'react';
import type { SessionMap, SessionMapEvent } from '@/core/schema';
import {
  initialPlayback,
  playbackClock,
  playbackReducer,
  replayKeyAction,
} from '@/core/playback';
import { MAP_RADIUS } from '@/core/mapGeometry';
import {
  buildTree,
  cumulativeCost,
  errSource,
  eventNodeRefs,
  fileCoverage,
  findNext,
  findNextWhere,
  initialPlayhead,
  layoutTree,
  reviewMetrics,
  subagentOrdinal,
  type ErrorSource,
  type JumpKind,
} from '@/core/sessionMapModel';
import { sessionSignals } from '@/core/sessionSignals';

/** Everything the map, timeline and panels derive from the replayed map. */
export function useMapModel(map: SessionMap) {
  return useMemo(() => {
    const tree = buildTree(map);
    const nodes = layoutTree(tree, MAP_RADIUS);
    return {
      tree,
      nodes,
      refs: eventNodeRefs(map, tree),
      cumCost: cumulativeCost(map.events),
      ordinals: subagentOrdinal(map),
      review: reviewMetrics(map),
      coverage: fileCoverage(map),
      signals: sessionSignals(map),
    };
  }, [map]);
}

export type MapModel = ReturnType<typeof useMapModel>;

/**
 * The replay's playback window: the reducer (lazily loaded once per mount
 * against `map`'s event count), the rAF loop while playing, seeking/jumping,
 * and the replay region's keyboard shortcuts.
 */
export function usePlaybackWindow(map: SessionMap) {
  const [pb, dispatch] = useReducer(playbackReducer, map, (m) =>
    playbackReducer(initialPlayback, {
      type: 'load',
      n: m.events.length,
      start: initialPlayhead(m.events),
    })
  );

  // Progress survives a speed change but not a pause, seek or step (all of
  // which stop playback), so this resets only when playing flips.
  const carry = useRef({ last: 0, acc: 0 });
  useEffect(() => {
    carry.current = { last: 0, acc: 0 };
  }, [pb.playing]);
  useEffect(() => {
    if (!pb.playing) return;
    const tick = playbackClock(pb.speedIdx, dispatch, carry.current);
    let raf = 0;
    const frame = (t: number) => {
      tick(t);
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [pb.playing, pb.speedIdx]);

  const seek = useCallback(
    (index: number) => dispatch({ type: 'seek', index }),
    []
  );
  const jumpWhere = useCallback(
    (test: (e: SessionMapEvent) => boolean) => {
      const i = findNextWhere(map.events, pb.p, test);
      if (i != null) dispatch({ type: 'seek', index: i });
    },
    [map.events, pb.p]
  );
  const jump = useCallback(
    (kind: JumpKind) => {
      const i = findNext(map.events, pb.p, kind);
      if (i != null) dispatch({ type: 'seek', index: i });
    },
    [map.events, pb.p]
  );
  const jumpSource = useCallback(
    (src: ErrorSource) => jumpWhere((e) => e.err && errSource(e) === src),
    [jumpWhere]
  );

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const t = e.target as HTMLElement;
    if (t.closest('input, textarea, [contenteditable="true"]')) return;
    const action = replayKeyAction(e);
    if (!action) return;
    if (action.type === 'jump') {
      jump(action.kind);
      return;
    }
    e.preventDefault();
    dispatch(action);
  };

  return { pb, dispatch, seek, jump, jumpSource, onKeyDown };
}

export type PlaybackWindow = ReturnType<typeof usePlaybackWindow>;
