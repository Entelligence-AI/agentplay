/**
 * Canvas painters for the Session Explorer timeline (`SessionTimeline`).
 * Each takes the shared draw environment and one lane; none keeps state.
 */
import type { SessionMapEvent } from './schema';
import {
  CATEGORY_COLOR,
  CONTEXT_COLOR,
  KIND_META,
  kindMeta,
  kindStyle,
} from './eventColors';
import {
  categoryOf,
  displayCategory,
  magnitude,
  matchesAnyCategory,
  sizeMetric,
  type Category,
} from './sessionLayout';
import { clockAt, fmtTok, llmTokens } from './sessionMapModel';
import { LANE_LABEL, type LaneKey, type TaskWindow } from './sessionScope';

/** Row geometry (CSS px). */
export const TL = {
  labelW: 112,
  top: 6,
  right: 8,
  gap: 4,
  subGap: 2,
  axisH: 14,
};

const ROW_H: Record<LaneKey | 'sub', number> = {
  tasks: 30,
  events: 30,
  ctx: 32,
  main: 14,
  subs: 16,
  sub: 12,
};

export interface Lane {
  /** the lane key, or the agent id of one expanded subagent row */
  id: string;
  key: LaneKey | 'sub';
  label: string;
  h: number;
  y: number;
}

/** Lane geometry: the visible lanes in fixed order, then (when the
 * Subagents row is expanded) one row per subagent.
 *
 * `subRows` is the session's full subagent list, always -- the collapsed
 * row's "(N)" count reads its length regardless of `expanded`, so it never
 * shows a stale "(0)" before the row has ever been opened.
 * `expanded` alone decides whether the individual rows are also drawn.
 * `tasksLabel` names the first lane (`taskNoun`). */
export function buildLanes(
  visible: ReadonlySet<LaneKey>,
  subRows: readonly { id: string; label: string }[] | null,
  expanded: boolean,
  tasksLabel = LANE_LABEL.tasks
): { lanes: Lane[]; bottom: number } {
  const rows: Omit<Lane, 'y'>[] = [];
  for (const key of Object.keys(LANE_LABEL) as LaneKey[]) {
    if (!visible.has(key)) continue;
    const label =
      key === 'subs'
        ? `Subagents (${subRows?.length ?? 0})`
        : key === 'tasks'
          ? tasksLabel
          : LANE_LABEL[key];
    rows.push({ id: key, key, label, h: ROW_H[key] });
    if (key === 'subs' && expanded && subRows)
      for (const s of subRows)
        rows.push({ id: s.id, key: 'sub', label: s.label, h: ROW_H.sub });
  }
  let y = TL.top;
  const lanes = rows.map((l) => {
    const lane = { ...l, y };
    y += l.h + (l.key === 'sub' ? TL.subGap : TL.gap);
    return lane;
  });
  return { lanes, bottom: y };
}

/** The height the optional lanes (Context, Subagents and its expanded rows)
 * add over the always-on ones. */
export function optionalLanesHeight(
  visible: ReadonlySet<LaneKey>,
  subRows: readonly { id: string; label: string }[] | null,
  expanded: boolean
): number {
  const base = new Set([...visible].filter((k) => k !== 'ctx' && k !== 'subs'));
  return (
    buildLanes(visible, subRows, expanded).bottom -
    buildLanes(base, subRows, false).bottom
  );
}

/** Events drawn on the agent rows (the tool-ish work of that agent). */
const onAgentRow = (e: SessionMapEvent) =>
  e.kind !== 'user' &&
  e.kind !== 'compaction' &&
  e.kind !== 'error' &&
  e.kind !== 'llm' &&
  e.kind !== 'delay';

/** Which events the hover snaps to on a lane (none: the raw index). On the
 * Events bar that is the selected categories' events, if any are selected. */
export function laneSnap(
  lane: Lane,
  selected: ReadonlySet<Category> = new Set()
): ((e: SessionMapEvent) => boolean) | undefined {
  switch (lane.key) {
    case 'events':
      return selected.size ? (e) => matchesAnyCategory(e, selected) : undefined;
    case 'ctx':
      return (e) => e.kind === 'llm' || e.kind === 'compaction';
    case 'main':
      return (e) => e.agent === 'main' && onAgentRow(e);
    case 'subs':
      return (e) => e.agent !== 'main' && onAgentRow(e);
    case 'sub':
      return (e) => e.agent === lane.id && onAgentRow(e);
    default:
      return undefined;
  }
}

/** Everything a painter needs: the context, colour resolver and x scale. */
export interface DrawEnv {
  ctx: CanvasRenderingContext2D;
  col: (v: string) => string;
  /** event index -> x (CSS px) */
  x: (i: number) => number;
  /** width of one event column on the steps axis */
  cw: number;
  width: number;
  plotW: number;
  bottom: number;
  sans: string;
  mono: string;
  events: readonly SessionMapEvent[];
}

/** Lane backgrounds and labels (lanes in `htmlLabels` get HTML buttons). */
export function drawLaneFrames(
  g: DrawEnv,
  lanes: readonly Lane[],
  htmlLabels: (l: Lane) => boolean
) {
  const { ctx, col } = g;
  for (const ln of lanes) {
    const chart = ln.key === 'ctx';
    if (!chart) {
      ctx.fillStyle = col('var(--ds-gray-subtlest)');
      ctx.fillRect(TL.labelW, ln.y, g.plotW, ln.h);
    }
    if (htmlLabels(ln)) continue;
    ctx.fillStyle = col('var(--ds-text-subtle)');
    ctx.fillText(ln.label, 4, ln.y + (chart ? ln.h / 2 - 6 : ln.h / 2));
  }
}

/** Task brackets: one labelled segment per task window. */
export function drawTasks(
  g: DrawEnv,
  TK: Lane,
  windows: readonly TaskWindow[],
  task: number | null,
  startedAt: string
) {
  const { ctx, col, x, cw } = g;
  for (const w of windows) {
    const x0 = x(w.start) - cw / 2;
    const x1 = x(w.end) + cw / 2;
    const sel = task === w.n;
    ctx.globalAlpha = 1;
    // Was --ds-surface-white for an unselected bracket: on the timeline's
    // own dark-mode canvas that's a near-black fill under --ds-text-default
    // (a mid gray meant for the page's body text, not this specific
    // backdrop) -- low contrast, hard to read. Same
    // gray the lane rows themselves already use (proven legible here) as
    // the fill, with the higher-emphasis text token as the label colour.
    ctx.fillStyle = col(
      sel ? 'var(--ds-brand-lightest)' : 'var(--ds-gray-subtlest)'
    );
    ctx.fillRect(x0 + 1, TK.y, Math.max(2, x1 - x0 - 2), TK.h);
    ctx.strokeStyle = col(
      sel ? 'var(--ds-brand-default)' : 'var(--ds-border-darker)'
    );
    ctx.lineWidth = 1;
    ctx.strokeRect(x0 + 1.5, TK.y + 0.5, Math.max(2, x1 - x0 - 3), TK.h - 1);
    ctx.fillStyle = col('var(--ds-brand-default)');
    ctx.fillRect(x0 + 1, TK.y, Math.max(2, x1 - x0 - 2), 2.5);
    ctx.globalAlpha = 1;
    // Always `n · name`, clipped to the bracket: no width threshold that
    // drops the label (the full name is on the canvas title on hover).
    const room = x1 - x0 - 10;
    if (room <= 0) continue;
    ctx.save();
    ctx.beginPath();
    ctx.rect(x0 + 4, TK.y, room, TK.h);
    ctx.clip();
    ctx.fillStyle = col(
      sel ? 'var(--ds-brand-default)' : 'var(--ds-text-darker)'
    );
    ctx.fillText(`${w.n} · ${w.name}`, x0 + 6, TK.y + 11);
    ctx.fillStyle = col(
      sel ? 'var(--ds-brand-default)' : 'var(--ds-text-subtle)'
    );
    ctx.font = g.mono;
    ctx.fillText(
      `${clockAt(startedAt, w.startT).slice(0, 5)}–${clockAt(startedAt, w.endT).slice(0, 5)}`,
      x0 + 6,
      TK.y + 22
    );
    ctx.font = g.sans;
    ctx.restore();
  }
}

/** Main-agent context, carried forward; compactions drop pre -> post. */
export function drawContext(
  g: DrawEnv,
  C: Lane,
  series: {
    max: number;
    values: ArrayLike<number>;
    before: Map<number, number>;
  }
) {
  const { ctx, col, x, events } = g;
  const N = events.length;
  const cy = (v: number) => C.y + C.h - (v / series.max) * (C.h - 4);
  const tracePath = () => {
    ctx.moveTo(x(0), cy(0));
    for (let i = 0; i < N; i++) {
      const pre = series.before.get(i);
      if (pre != null) ctx.lineTo(x(i), cy(pre));
      ctx.lineTo(x(i), cy(series.values[i]));
    }
  };
  ctx.beginPath();
  tracePath();
  ctx.lineTo(x(N - 1), cy(0));
  ctx.closePath();
  ctx.globalAlpha = 0.16;
  ctx.fillStyle = col(CONTEXT_COLOR);
  ctx.fill();
  ctx.globalAlpha = 1;
  ctx.beginPath();
  tracePath();
  ctx.strokeStyle = col(CONTEXT_COLOR);
  ctx.lineWidth = 1.4;
  ctx.stroke();
  ctx.fillStyle = col('var(--ds-text-subtle)');
  ctx.fillText(`${fmtTok(series.max)} max`, 4, C.y + C.h / 2 + 8);
  ctx.font = g.mono;
  ctx.fillStyle = col(KIND_META.compaction.color);
  events.forEach((e, i) => {
    if (e.kind !== 'compaction') return;
    const cx = x(i);
    ctx.fillRect(cx - 0.75, C.y, 1.5, C.h);
    ctx.fillText(
      `${fmtTok(e.preTokens ?? 0)}→${fmtTok(e.postTokens ?? 0)}`,
      Math.min(cx + 5, g.width - 70),
      C.y + 8
    );
  });
  ctx.font = g.sans;
}

const line = (g: DrawEnv, xc: number, y: number, h: number, w: number) =>
  g.ctx.fillRect(xc - w / 2, y, w, h);

function diamond(g: DrawEnv, xc: number, yc: number, r: number) {
  const { ctx } = g;
  ctx.beginPath();
  ctx.moveTo(xc, yc - r);
  ctx.lineTo(xc + r, yc);
  ctx.lineTo(xc, yc + r);
  ctx.lineTo(xc - r, yc);
  ctx.closePath();
}

/** What the Events bar sizes its marks against (the scope's largest). */
export interface EventScales {
  maxChars: number;
  maxDelayMs: number;
  maxCost: number;
  maxTok: number;
}

/** Opacity of a mark outside the selected categories. */
const DIM_ALPHA = 0.15;
/** A dim mark's height, as a share of the bar. */
const DIM_H = 0.45;

/** Height share and width of a selected mark: prompts by their size,
 * delays by their length, LLM requests by cost / tokens while Cost or Tokens
 * is selected (`sizeMetric`), everything else full height. */
function markSize(
  e: SessionMapEvent,
  metric: 'cost' | 'tokens' | null,
  s: EventScales
): { f: number; w: number } {
  if (metric && e.kind === 'llm') {
    const m =
      metric === 'cost'
        ? magnitude(e.cost ?? 0, s.maxCost)
        : magnitude(llmTokens(e), s.maxTok);
    return { f: Math.max(0.12, m), w: 2 + 3.5 * m };
  }
  if (e.kind === 'user')
    return { f: 0.45 + 0.55 * Math.sqrt((e.chars ?? 0) / s.maxChars), w: 2 };
  if (e.kind === 'delay') {
    const logMax = Math.log(1 + s.maxDelayMs / 1000) || 1;
    const f = Math.log(1 + (e.ms ?? 0) / 1000) / logMax;
    return { f: 0.35 + 0.65 * Math.min(1, f), w: 2 };
  }
  return { f: 1, w: 2 };
}

/** One selected mark: an output is a diamond in its kind's colour, anything
 * else a bottom-anchored line in its category colour. */
function drawSelectedMark(
  g: DrawEnv,
  E: Lane,
  i: number,
  cat: Category,
  size: { f: number; w: number },
  emph: boolean
) {
  const { ctx, col, x, events } = g;
  const e = events[i];
  const xc = x(i);
  if (cat === 'out') {
    const kind = e.vcs?.action === 'pr_create' ? 'pr_create' : e.kind;
    ctx.fillStyle = col(kindMeta(kind).color);
    diamond(g, xc, E.y + E.h / 2, 5);
    ctx.fill();
    if (emph) {
      diamond(g, xc, E.y + E.h / 2, 8);
      ctx.strokeStyle = col('var(--ds-text-darker)');
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }
    return;
  }
  const hh = (E.h - 4) * size.f;
  ctx.fillStyle = col(CATEGORY_COLOR[cat]);
  line(g, xc, E.y + E.h - 2 - hh, hh, emph ? size.w + 2 : size.w);
}

/**
 * The Events bar: every event, in its category colour. Events in a selected
 * category draw at full strength (and on top); the rest dim to a short,
 * faint tick so the selection lights up on the same bar. `emph` marks
 * (pinned / hovered) draw wider.
 */
export function drawEvents(
  g: DrawEnv,
  E: Lane,
  selected: ReadonlySet<Category>,
  scale: EventScales,
  emph: ReadonlySet<number>
) {
  const { ctx, col, x, events } = g;
  const on: number[] = [];
  const dimH = (E.h - 4) * DIM_H;
  ctx.globalAlpha = DIM_ALPHA;
  events.forEach((e, i) => {
    if (matchesAnyCategory(e, selected)) {
      on.push(i);
      return;
    }
    ctx.fillStyle = col(CATEGORY_COLOR[categoryOf(e)]);
    line(g, x(i), E.y + E.h - 2 - dimH, dimH, emph.has(i) ? 3 : 1.5);
  });
  ctx.globalAlpha = 1;
  const metric = sizeMetric(selected);
  for (const i of on) {
    const e = events[i];
    drawSelectedMark(
      g,
      E,
      i,
      displayCategory(e, selected),
      markSize(e, metric, scale),
      emph.has(i)
    );
  }
}

/** Agent rows: main, all subagents combined, and each expanded subagent. */
export function drawAgentRows(g: DrawEnv, lanes: readonly Lane[]) {
  const { ctx, col, x, cw, events } = g;
  const main = lanes.find((l) => l.key === 'main');
  const subs = lanes.find((l) => l.key === 'subs');
  const rows = new Map(
    lanes.filter((l) => l.key === 'sub').map((l) => [l.id, l])
  );
  if (!main && !subs && !rows.size) return;
  events.forEach((e, i) => {
    if (!onAgentRow(e)) return;
    ctx.fillStyle = col(kindStyle(e).color);
    const bx = x(i) - cw / 2;
    const targets =
      e.agent === 'main' ? [main] : [subs, rows.get(e.agent ?? '')];
    for (const ln of targets) if (ln) ctx.fillRect(bx, ln.y + 1, cw, ln.h - 2);
  });
}

/** Outside an open subagent's window: shaded, the window outlined. */
export function drawShade(g: DrawEnv, shade: { start: number; end: number }) {
  const { ctx, col, x, cw, width, bottom } = g;
  const sx0 = x(shade.start) - cw / 2;
  const sx1 = x(shade.end) + cw / 2;
  ctx.fillStyle = col('var(--ds-gray-subtlest)');
  ctx.globalAlpha = 0.8;
  if (sx0 > TL.labelW)
    ctx.fillRect(TL.labelW, TL.top, sx0 - TL.labelW, bottom - TL.top);
  if (sx1 < width - TL.right)
    ctx.fillRect(sx1, TL.top, width - TL.right - sx1, bottom - TL.top);
  ctx.globalAlpha = 1;
  ctx.strokeStyle = col('var(--ds-brand-default)');
  ctx.lineWidth = 1;
  ctx.strokeRect(
    sx0 + 0.5,
    TL.top + 0.5,
    Math.max(1, sx1 - sx0 - 1),
    bottom - TL.top - 1
  );
}

/** The pinned event: a dashed rule across every lane. */
export function drawPin(g: DrawEnv, i: number) {
  const { ctx, col, x, bottom } = g;
  ctx.save();
  ctx.strokeStyle = col('var(--ds-text-darker)');
  ctx.globalAlpha = 0.6;
  ctx.lineWidth = 2;
  ctx.setLineDash([4, 3]);
  ctx.beginPath();
  ctx.moveTo(x(i), 0);
  ctx.lineTo(x(i), bottom);
  ctx.stroke();
  ctx.restore();
}

/** The future dimmed, and the playhead. */
export function drawPlayhead(g: DrawEnv, p: number) {
  const { ctx, col, x, cw, width, bottom } = g;
  const px = x(p);
  ctx.fillStyle = col('var(--ds-surface-white)');
  ctx.globalAlpha = 0.55;
  ctx.fillRect(
    px + cw / 2,
    TL.top,
    width - TL.right - px - cw / 2,
    bottom - TL.top
  );
  ctx.globalAlpha = 1;
  ctx.strokeStyle = col('var(--ds-text-darker)');
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(px, 0);
  ctx.lineTo(px, bottom);
  ctx.stroke();
}

/**
 * "HH:MM" labels for axis ticks at `ts` ms into the session. A tick that lands
 * on a later UTC day than the tick before it also gets the date
 * ("Oct 22 02:23"), so a session that runs past midnight doesn't read as going
 * back in time.
 */
export function axisTickLabels(startedAt: string, ts: number[]): string[] {
  const base = Date.parse(startedAt);
  let prevDay = Math.floor(base / 86_400_000);
  return ts.map((t) => {
    const clock = clockAt(startedAt, t).slice(0, 5);
    if (Number.isNaN(base)) return clock;
    const day = Math.floor((base + t) / 86_400_000);
    if (day === prevDay) return clock;
    prevDay = day;
    const date = new Date(base + t).toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
      timeZone: 'UTC',
    });
    return `${date} ${clock}`;
  });
}

/** Five clock ticks along the bottom. */
export function drawAxis(
  g: DrawEnv,
  pos: Float64Array,
  startedAt: string,
  height: number
) {
  const { ctx, col, x, events } = g;
  const N = events.length;
  ctx.fillStyle = col('var(--ds-text-subtler)');
  ctx.font = g.mono;
  const ticks = [0, 1, 2, 3, 4].map((q) => {
    const i = pos.findIndex((v) => v >= q / 4);
    return i < 0 ? N - 1 : i;
  });
  const labels = axisTickLabels(
    startedAt,
    ticks.map((i) => events[i]?.t ?? 0)
  );
  ticks.forEach((i, q) => {
    ctx.textAlign = q === 0 ? 'left' : q === 4 ? 'right' : 'center';
    ctx.fillText(labels[q], x(i), height - 6);
  });
  ctx.textAlign = 'left';
  ctx.font = g.sans;
}
