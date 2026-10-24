/**
 * Squarified treemap layout (Bruls, Huizing, van Wijk 2000).
 *
 * Lays out weighted items in a rectangle so each item's area is proportional
 * to its value and its aspect ratio stays close to 1. Pure geometry: the
 * caller decides units (the heatmap uses percentages of its box).
 */

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface Placed<T> extends Rect {
  item: T;
}

export function squarify<T>(items: T[], value: (t: T) => number, box: Rect): Placed<T>[] {
  const entries = items
    .map((item) => ({ item, v: Math.max(0, value(item)) }))
    .filter((e) => e.v > 0)
    .sort((a, b) => b.v - a.v);
  const total = entries.reduce((s, e) => s + e.v, 0);
  if (!total || box.w <= 0 || box.h <= 0) return [];
  const k = (box.w * box.h) / total;
  const nodes = entries.map((e) => ({ item: e.item, a: e.v * k }));
  const out: Placed<T>[] = [];
  let { x, y, w, h } = box;

  const worst = (row: { a: number }[], side: number) => {
    const s = row.reduce((t, n) => t + n.a, 0);
    let max = -Infinity;
    let min = Infinity;
    for (const n of row) {
      max = Math.max(max, n.a);
      min = Math.min(min, n.a);
    }
    return Math.max((side * side * max) / (s * s), (s * s) / (side * side * min));
  };

  const lay = (row: { item: T; a: number }[]) => {
    const s = row.reduce((t, n) => t + n.a, 0);
    if (w >= h) {
      const cw = s / h;
      let cy = y;
      for (const n of row) {
        const ch = n.a / cw;
        out.push({ item: n.item, x, y: cy, w: cw, h: ch });
        cy += ch;
      }
      x += cw;
      w -= cw;
    } else {
      const ch = s / w;
      let cx = x;
      for (const n of row) {
        const cw = n.a / ch;
        out.push({ item: n.item, x: cx, y, w: cw, h: ch });
        cx += cw;
      }
      y += ch;
      h -= ch;
    }
  };

  let row: { item: T; a: number }[] = [];
  let i = 0;
  while (i < nodes.length) {
    const side = Math.min(w, h);
    const next = nodes[i];
    if (!row.length || worst([...row, next], side) <= worst(row, side)) {
      row.push(next);
      i += 1;
    } else {
      lay(row);
      row = [];
    }
  }
  if (row.length) lay(row);
  return out;
}
