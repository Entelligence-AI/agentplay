import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

// A Tailwind color or typography utility pointing at an undefined var() still
// compiles, then renders as nothing (no fill, inherited font size). Keep every
// var the config and source reference defined in index.css.
const root = path.resolve(__dirname, '../..');
const css = fs.readFileSync(path.join(root, 'src/styles/index.css'), 'utf8');
const defined = new Set([...css.matchAll(/(--[\w-]+)\s*:/g)].map((m) => m[1]));

function sourceFiles(dir: string): string[] {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) return sourceFiles(p);
    return /\.tsx?$/.test(e.name) && !/\.test\./.test(e.name) ? [p] : [];
  });
}

describe('design tokens', () => {
  it('every var() in tailwind.config.ts is defined', () => {
    const cfg = fs.readFileSync(path.join(root, 'tailwind.config.ts'), 'utf8');
    const missing = [...cfg.matchAll(/var\((--[\w-]+)\)/g)].map((m) => m[1]).filter((v) => !defined.has(v));
    expect([...new Set(missing)]).toEqual([]);
  });

  it('every ds-text-* typography utility used in src has its size token', () => {
    const missing = new Set<string>();
    for (const f of sourceFiles(path.join(root, 'src'))) {
      for (const [cls] of fs.readFileSync(f, 'utf8').matchAll(/\bds-text-(?:body|title|code)-[\w-]+/g)) {
        if (!cls.endsWith('-lh') && !defined.has(`--${cls}`)) missing.add(cls);
      }
    }
    expect([...missing]).toEqual([]);
  });
});
