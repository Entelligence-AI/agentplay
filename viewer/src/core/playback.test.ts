import { describe, expect, it } from 'vitest';
import {
  MS_PER_EVENT,
  SPEEDS,
  initialPlayback,
  playbackClock,
  playbackReducer,
  replayKeyAction,
  type PlaybackAction,
  type PlaybackState,
} from './playback';

const run = (s: PlaybackState, ...actions: PlaybackAction[]) =>
  actions.reduce(playbackReducer, s);

const loaded = (n = 100, start = 10) =>
  playbackReducer(initialPlayback, { type: 'load', n, start });

const clocked = (s: PlaybackState) => {
  const c = {
    state: s,
    actions: [] as PlaybackAction[],
    frames(...ts: number[]) {
      ts.forEach(tick);
      return c;
    },
  };
  const tick = playbackClock(s.speedIdx, (a) => {
    c.actions.push(a);
    c.state = playbackReducer(c.state, a);
  });
  return c;
};

describe('playbackReducer', () => {
  it('loads paused and clamps the start', () => {
    expect(loaded(5, 99).p).toBe(4);
    expect(loaded(5, -3).p).toBe(0);
    expect(loaded(0, 3).p).toBe(0);
    expect(loaded().playing).toBe(false);
  });

  it('seek and step clamp and pause', () => {
    const playing = run(loaded(), { type: 'play' });
    expect(playing.playing).toBe(true);
    const sought = playbackReducer(playing, { type: 'seek', index: 500 });
    expect(sought.p).toBe(99);
    expect(sought.playing).toBe(false);
    expect(run(loaded(), { type: 'step', delta: -50 }).p).toBe(0);
    expect(run(loaded(), { type: 'step', delta: 10 }).p).toBe(20);
  });

  it('play at the end restarts from the top', () => {
    const s = run(loaded(), { type: 'seek', index: 99 }, { type: 'play' });
    expect(s.p).toBe(0);
    expect(s.playing).toBe(true);
  });

  it('does not play a single-event session', () => {
    expect(run(loaded(1, 0), { type: 'play' }).playing).toBe(false);
  });

  it('toggle flips play and pause', () => {
    const s = run(loaded(), { type: 'toggle' });
    expect(s.playing).toBe(true);
    expect(playbackReducer(s, { type: 'toggle' }).playing).toBe(false);
  });

  it('cycles speeds 1x, 4x, 16x', () => {
    const s0 = loaded();
    expect(SPEEDS[s0.speedIdx]).toBe(4);
    const s1 = playbackReducer(s0, { type: 'cycleSpeed' });
    expect(SPEEDS[s1.speedIdx]).toBe(16);
    const s2 = playbackReducer(s1, { type: 'cycleSpeed' });
    expect(SPEEDS[s2.speedIdx]).toBe(1);
  });

  it('stops at the last event', () => {
    let s = run(loaded(20, 18), { type: 'play' });
    s = playbackReducer(s, { type: 'advance', by: 10 });
    expect(s.p).toBe(19);
    expect(s.playing).toBe(false);
    expect(playbackReducer(s, { type: 'advance', by: 1 })).toBe(s);
  });

  it('ignores advances while paused', () => {
    const s = loaded();
    expect(playbackReducer(s, { type: 'advance', by: 1 })).toBe(s);
  });
});

describe('playbackClock', () => {
  const playing = (n = 100, start = 10) =>
    run(loaded(n, start), { type: 'play' });
  const event4x = MS_PER_EVENT / 4;

  it('a frame that completes no event leaves the state untouched', () => {
    const s = playing();
    const c = clocked(s).frames(1000, 1016.7, 1033.4);
    expect(c.actions).toEqual([]);
    expect(c.state).toBe(s);
  });

  it('a frame that completes one event advances by exactly one', () => {
    const c = clocked(playing()).frames(1000, 1000 + event4x);
    expect(c.actions).toEqual([{ type: 'advance', by: 1 }]);
    expect(c.state.p).toBe(11);
  });

  it('carries the fraction of an event across frames', () => {
    const c = clocked(playing()).frames(1000, 1000 + event4x / 2);
    expect(c.state.p).toBe(10);
    c.frames(1000 + event4x, 1000 + event4x * 5);
    expect(c.state.p).toBe(15);
  });

  it('a long stalled frame counts as 250 ms, not as the stall', () => {
    const c = clocked(playing(1000, 0)).frames(1000, 31_000);
    expect(c.state.p).toBe(4);
    expect(c.state.playing).toBe(true);
  });

  it('plays at the speed it was created with', () => {
    const s = run(loaded(), { type: 'cycleSpeed' }, { type: 'play' });
    expect(SPEEDS[s.speedIdx]).toBe(16);
    expect(clocked(s).frames(1000, 1000 + MS_PER_EVENT).state.p).toBe(26);
  });

  it('keeps partial progress across a speed change when given the same carry', () => {
    const carry = { last: 0, acc: 0 };
    const actions: PlaybackAction[] = [];
    // Half an event at 1x, then a 4x clock picks up where it left off.
    const slow = playbackClock(0, (a) => actions.push(a), carry);
    slow(1000);
    slow(1000 + MS_PER_EVENT / 2);
    expect(actions).toEqual([]);
    const fast = playbackClock(1, (a) => actions.push(a), carry);
    fast(1000 + MS_PER_EVENT / 2 + event4x / 2);
    expect(actions).toEqual([{ type: 'advance', by: 1 }]);
  });
});

describe('replayKeyAction', () => {
  const key = (
    k: string,
    mods: Partial<Record<string, boolean>> = {},
    target: EventTarget = document.createElement('div')
  ) =>
    replayKeyAction({
      key: k,
      shiftKey: false,
      metaKey: false,
      ctrlKey: false,
      altKey: false,
      defaultPrevented: false,
      target,
      ...mods,
    });
  const keysOn = (target: Element) =>
    [' ', 'ArrowRight', 'e'].map((k) => key(k, {}, target)?.type ?? null);
  const focused = (html: string) => {
    const host = document.createElement('div');
    host.innerHTML = html;
    return host.querySelector('[data-t]')!;
  };

  it('space toggles, arrows step (Shift steps 10)', () => {
    expect(key(' ')).toEqual({ type: 'toggle' });
    expect(key('ArrowRight')).toEqual({ type: 'step', delta: 1 });
    expect(key('ArrowLeft')).toEqual({ type: 'step', delta: -1 });
    expect(key('ArrowLeft', { shiftKey: true })).toEqual({
      type: 'step',
      delta: -10,
    });
  });

  it('E / X / D / O / R jump, either case', () => {
    expect(key('e')).toEqual({ type: 'jump', kind: 'edit' });
    expect(key('X')).toEqual({ type: 'jump', kind: 'err' });
    expect(key('d')).toEqual({ type: 'jump', kind: 'delay' });
    expect(key('O')).toEqual({ type: 'jump', kind: 'out' });
    expect(key('r')).toEqual({ type: 'jump', kind: 'retry' });
  });

  it('other keys and browser shortcuts map to nothing', () => {
    expect(key('q')).toBeNull();
    expect(key('Enter')).toBeNull();
    expect(key('constructor')).toBeNull();
    expect(key('e', { metaKey: true })).toBeNull();
    expect(key(' ', { ctrlKey: true })).toBeNull();
    expect(key('ArrowRight', { altKey: true })).toBeNull();
  });

  it('the replay region, a map dot and the page body get every shortcut', () => {
    for (const target of [
      focused('<div role="region" tabindex="-1" data-t><p>map</p></div>'),
      focused('<svg role="img"><circle tabindex="0" data-t></circle></svg>'),
      focused('<div contenteditable="false" data-t>14:05</div>'),
      document.body,
    ])
      expect(keysOn(target)).toEqual(['toggle', 'step', 'jump']);
  });

  it('Space presses a focused button, link or summary instead of playing', () => {
    for (const html of [
      '<button data-t>4×</button>',
      '<a href="#" data-t>PR #12</a>',
      '<details><summary data-t>Tool categories</summary></details>',
      '<svg><text role="button" tabindex="0" data-t>index.ts</text></svg>',
    ])
      expect(keysOn(focused(html)), html).toEqual([null, 'step', 'jump']);
  });

  it('tabs and radios keep Space and the arrows', () => {
    for (const html of [
      '<div role="tablist"><button role="tab" data-t>Details</button></div>',
      '<div role="radiogroup"><button role="radio" data-t>PR</button></div>',
    ])
      expect(keysOn(focused(html)), html).toEqual([null, null, 'jump']);
  });

  it('text fields, lists, menus and popups keep every key', () => {
    for (const html of [
      '<input data-t>',
      '<textarea data-t></textarea>',
      '<select data-t></select>',
      '<div contenteditable data-t></div>',
      '<div contenteditable="plaintext-only" data-t></div>',
      '<input role="combobox" data-t>',
      '<div role="listbox"><button role="option" data-t>14:05</button></div>',
      '<div role="menu"><div role="menuitem" tabindex="-1" data-t>Next</div></div>',
      '<div role="dialog" tabindex="-1" data-t><p>All models</p></div>',
      '<div role="dialog"><button data-t>claude-opus</button></div>',
    ])
      expect(keysOn(focused(html)), html).toEqual([null, null, null]);
  });

  it('a key a control already handled maps to nothing', () => {
    expect(key(' ', { defaultPrevented: true })).toBeNull();
    expect(key('ArrowRight', { defaultPrevented: true })).toBeNull();
  });
});
