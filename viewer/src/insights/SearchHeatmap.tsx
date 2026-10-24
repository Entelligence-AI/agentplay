import { useMemo, useState } from 'react';
import { MagnifyingGlass } from '@phosphor-icons/react';
import { Input } from '@/components/ui/input';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import { SEQUENTIAL } from '@/components/dataviz';
import { cn } from '@/lib/utils';
import { fileHeat, type FileHeat, type InsightsSession } from '@/core/insights';
import { squarify } from '@/core/treemap';
import { Panel } from './panels';

/** Files drawn as tiles; the rest are counted in the footnote. Small tiles
 * would be unreadable slivers, and a 1,000-file store would make the layout
 * itself the slow part. */
const MAX_TILES = 150;
/** Folder header height inside its block, in % of the map's height. */
const HEAD = 4.5;
/** The map's aspect: width units per height unit, so squarify sees real shapes. */
const ASPECT = 2.6;

type Metric = 'sessions' | 'ratio';

const metricValue = (f: FileHeat, m: Metric) => (m === 'sessions' ? f.sessions : f.hits / (f.edits + 1));

export function SearchHeatmap({ rows }: { rows: InsightsSession[] }) {
  const [metric, setMetric] = useState<Metric>('sessions');
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const all = useMemo(() => fileHeat(rows).filter((f) => f.hits > 0), [rows]);
  const shown = useMemo(() => all.slice(0, MAX_TILES), [all]);

  const layout = useMemo(() => {
    const folders = new Map<string, FileHeat[]>();
    for (const f of shown) {
      const k = `${f.repo}:${f.dir}`;
      folders.set(k, [...(folders.get(k) ?? []), f]);
    }
    const groups = [...folders.entries()].map(([key, files]) => ({
      key,
      files,
      hits: files.reduce((s, f) => s + f.hits, 0),
    }));
    // Lay out in (ASPECT x 100) units so tiles come out square on screen,
    // then convert x and w to percentages of the width.
    const W = 100 * ASPECT;
    const blocks = squarify(groups, (g) => g.hits, { x: 0, y: 0, w: W, h: 100 });
    return blocks.map((b) => {
      const head = b.h > 12 && b.w > 30 ? HEAD : 0;
      const pad = 0.6;
      const tiles = squarify(b.item.files, (f) => f.hits, {
        x: b.x + pad,
        y: b.y + head + pad,
        w: Math.max(0, b.w - 2 * pad),
        h: Math.max(0, b.h - head - 2 * pad),
      });
      const toPct = (r: { x: number; y: number; w: number; h: number }) => ({
        left: `${(r.x / W) * 100}%`,
        top: `${r.y}%`,
        width: `${(r.w / W) * 100}%`,
        height: `${r.h}%`,
      });
      return { key: b.item.key, head, box: toPct(b), raw: b, tiles: tiles.map((t) => ({ f: t.item, box: toPct(t), raw: t })) };
    });
  }, [shown]);

  const values = shown.map((f) => metricValue(f, metric));
  const lo = Math.min(...values, 0);
  const hi = Math.max(...values, 1);
  // Four steps of the brand ramp: dark enough to read text on in both themes.
  const ramp = SEQUENTIAL.slice(0, 4);
  const level = (f: FileHeat) =>
    Math.min(ramp.length - 1, Math.floor((ramp.length * (metricValue(f, metric) - lo)) / ((hi - lo) || 1)));
  const q = query.trim().toLowerCase();
  const sel = selected ? shown.find((f) => f.key === selected) : undefined;

  return (
    <Panel
      title="Search heatmap"
      term="searchHeatmap"
      question={`Where do agents spend effort finding code? ${all.length} ${all.length === 1 ? 'file' : 'files'} came up in searches.`}
    >
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative w-full max-w-xs">
          <MagnifyingGlass size={14} aria-hidden className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ds-text-subtle" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Highlight a path or name"
            aria-label="Highlight files"
            className="h-8 pl-8 font-mono text-xs"
          />
        </div>
        <div className="flex items-center gap-2 ds-text-body-xs-medium text-ds-text-subtle">
          Colour
          <ToggleGroup
            value={[metric]}
            onValueChange={(v: unknown[]) => {
              const next = v.find((x): x is Metric => x === 'sessions' || x === 'ratio');
              if (next) setMetric(next);
            }}
            aria-label="Colour tiles by"
          >
            <ToggleGroupItem value="sessions">Sessions</ToggleGroupItem>
            <ToggleGroupItem value="ratio">Searched vs edited</ToggleGroupItem>
          </ToggleGroup>
        </div>
      </div>

      {shown.length ? (
        <div className="relative h-[420px] overflow-hidden rounded-lg bg-ds-gray-subtlest" role="group" aria-label="Searched files by folder">
          {layout.map((g) => (
            <div
              key={g.key}
              className="absolute overflow-hidden rounded-md border border-ds-border-subtler"
              style={g.box}
            >
              {g.head ? (
                <span className="block truncate px-1.5 pt-0.5 font-mono text-[10.5px] font-semibold text-ds-text-default">
                  {g.key}
                </span>
              ) : null}
            </div>
          ))}
          {layout.flatMap((g) =>
            g.tiles.map(({ f, box, raw }) => {
              const match = !q || f.key.toLowerCase().includes(q);
              // Room for a name and a count: about 70 x 34 px on a 1,300 px wide map.
              const label = raw.w > 14 && raw.h > 8;
              const tip = `${f.key}: ${f.sessions} sessions, ${f.hits} search hits, ${f.reads} reads, ${f.edits} edits`;
              return (
                <button
                  key={f.key}
                  type="button"
                  title={tip}
                  aria-label={tip}
                  aria-pressed={selected === f.key}
                  onClick={() => setSelected(selected === f.key ? null : f.key)}
                  className={cn(
                    'absolute overflow-hidden rounded-sm border border-ds-surface-white px-1.5 py-1 text-left text-ds-text-darker transition-opacity focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ds-brand-subtle',
                    selected === f.key && 'ring-2 ring-ds-text-darker'
                  )}
                  style={{ ...box, background: ramp[level(f)], opacity: match ? 1 : 0.2 }}
                >
                  {label ? (
                    <>
                      <span className="block truncate font-mono text-[11px] font-semibold">{f.name}</span>
                      <span className="block truncate text-[10.5px] opacity-80">
                        {metric === 'sessions'
                          ? `${f.sessions} ${f.sessions === 1 ? 'session' : 'sessions'}`
                          : f.edits
                            ? `${(f.hits / f.edits).toFixed(1)} hits per edit`
                            : 'never edited'}
                      </span>
                    </>
                  ) : null}
                </button>
              );
            })
          )}
        </div>
      ) : (
        <p className="py-6 text-center ds-text-body-xs-medium text-ds-text-subtle">No file searches in this window.</p>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2 ds-text-body-xs-medium text-ds-text-subtle">
        <span className="flex items-center gap-2">
          {metric === 'sessions' ? 'Fewer sessions' : 'Often edited'}
          <span className="flex gap-0.5" aria-hidden>
            {ramp.map((c) => (
              <span key={c} className="h-3 w-5 rounded-sm" style={{ background: c }} />
            ))}
          </span>
          {metric === 'sessions' ? 'More sessions' : 'Searched, rarely edited'}
        </span>
        <span>
          {all.length > shown.length
            ? `Top ${shown.length} files by search hits shown; ${all.length - shown.length} smaller ones hidden.`
            : 'Size is search hits.'}
        </span>
      </div>

      {sel ? (
        <div className="flex flex-wrap items-center gap-x-6 gap-y-1 rounded-lg border border-ds-border-subtlest bg-ds-gray-subtlest px-3 py-2 ds-text-body-xs-medium">
          <span className="min-w-0 flex-1 break-all font-mono">
            <span className="text-ds-text-subtle">
              {sel.repo}:{sel.dir}
            </span>
            <strong className="text-ds-text-darker">{sel.name}</strong>
          </span>
          {(
            [
              ['sessions', sel.sessions],
              ['search hits', sel.hits],
              ['reads', sel.reads],
              ['edits', sel.edits],
            ] as const
          ).map(([k, v]) => (
            <span key={k}>
              <strong className="text-ds-text-darker">{v}</strong> <span className="text-ds-text-subtle">{k}</span>
            </span>
          ))}
        </div>
      ) : null}
    </Panel>
  );
}
