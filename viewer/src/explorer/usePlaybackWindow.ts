import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  type KeyboardEvent,
} from 'react';
import type { SessionMap, SessionMapEvent } from '@/core/schema';
import { initialPlayback, playbackReducer, replayKeyAction } from '@/core/playback';
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

  // rAF loop while playing; the reducer turns elapsed ms into events.
  useEffect(() => {
    if (!pb.playing) return;
    let raf = 0;
    let last = 0;
    const frame = (t: number) => {
      const dt = last ? t - last : 0;
      last = t;
      if (dt) dispatch({ type: 'tick', dtMs: dt });
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [pb.playing]);

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
