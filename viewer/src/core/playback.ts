/**
 * Playback state machine for the Session Explorer. The reducer is pure:
 * `playbackClock` feeds it `advance` from the rAF loop once a whole event is
 * due, everything else is a user action. Unit-tested in `playback.test.ts`.
 */

export const SPEEDS = [1, 4, 16] as const;
/** ms of wall clock per event at 1x (the default pacing). */
export const MS_PER_EVENT = 220;
/** A longer frame counts as this long: rAF stalls while the tab is hidden. */
const MAX_FRAME_MS = 250;

export interface PlaybackState {
  /** playhead: index of the current event */
  p: number;
  /** number of events in the loaded session */
  n: number;
  playing: boolean;
  speedIdx: number;
}

export type PlaybackAction =
  | { type: 'load'; n: number; start: number }
  | { type: 'seek'; index: number }
  | { type: 'step'; delta: number }
  | { type: 'play' }
  | { type: 'pause' }
  | { type: 'toggle' }
  | { type: 'cycleSpeed' }
  | { type: 'advance'; by: number };

export const initialPlayback: PlaybackState = {
  p: 0,
  n: 0,
  playing: false,
  speedIdx: 1,
};

const clamp = (i: number, n: number) => Math.max(0, Math.min(n - 1, i));

export function playbackReducer(
  s: PlaybackState,
  a: PlaybackAction
): PlaybackState {
  switch (a.type) {
    case 'load':
      return {
        ...s,
        n: a.n,
        p: a.n ? clamp(a.start, a.n) : 0,
        playing: false,
      };
    case 'seek':
      if (!s.n) return s;
      return { ...s, p: clamp(a.index, s.n), playing: false };
    case 'step':
      if (!s.n) return s;
      return { ...s, p: clamp(s.p + a.delta, s.n), playing: false };
    case 'play':
      if (s.n < 2) return s;
      // At the end, play restarts from the top.
      return { ...s, playing: true, p: s.p >= s.n - 1 ? 0 : s.p };
    case 'pause':
      return s.playing ? { ...s, playing: false } : s;
    case 'toggle':
      return playbackReducer(s, { type: s.playing ? 'pause' : 'play' });
    case 'cycleSpeed':
      return { ...s, speedIdx: (s.speedIdx + 1) % SPEEDS.length };
    case 'advance': {
      if (!s.playing) return s;
      const p = clamp(s.p + a.by, s.n);
      return { ...s, p, playing: p < s.n - 1 };
    }
    default:
      return s;
  }
}

/** The clock's progress; pass the same one to a new clock to keep it across a speed change. */
export interface ClockCarry {
  last: number;
  acc: number;
}

export function playbackClock(
  speedIdx: number,
  dispatch: (a: PlaybackAction) => void,
  carry: ClockCarry = { last: 0, acc: 0 }
) {
  return (t: number) => {
    const dt = carry.last ? Math.min(t - carry.last, MAX_FRAME_MS) : 0;
    carry.last = t;
    carry.acc += (dt * SPEEDS[speedIdx]) / MS_PER_EVENT;
    const by = Math.floor(carry.acc);
    if (!by) return;
    carry.acc -= by;
    dispatch({ type: 'advance', by });
  };
}

/** What a key press in the replay region does (see its aria-label). */
export type ReplayKeyAction =
  | { type: 'toggle' }
  | { type: 'step'; delta: number }
  | { type: 'jump'; kind: JumpKey };

type JumpKey = 'edit' | 'err' | 'delay' | 'out' | 'retry';

const JUMP_KEYS: Record<string, JumpKey> = {
  e: 'edit',
  x: 'err',
  d: 'delay',
  o: 'out',
  // Retries have no category chip; R keeps the jump the old Next… menu had.
  r: 'retry',
};

/**
 * Space plays or pauses, the arrow keys step (Shift steps 10), and E / X / D /
 * O / R jump to the next edit / error / delay / output / retry. Any modifier
 * other than Shift means the key belongs to the browser, so it maps to nothing.
 */
export function replayKeyAction(e: {
  key: string;
  shiftKey: boolean;
  metaKey: boolean;
  ctrlKey: boolean;
  altKey: boolean;
}): ReplayKeyAction | null {
  if (e.metaKey || e.ctrlKey || e.altKey) return null;
  if (e.key === ' ') return { type: 'toggle' };
  if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
    const d = (e.shiftKey ? 10 : 1) * (e.key === 'ArrowRight' ? 1 : -1);
    return { type: 'step', delta: d };
  }
  const k = e.key.toLowerCase();
  return Object.hasOwn(JUMP_KEYS, k)
    ? { type: 'jump', kind: JUMP_KEYS[k] }
    : null;
}
