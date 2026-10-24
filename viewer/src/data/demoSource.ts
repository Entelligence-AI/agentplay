import {
  sessionExplorerSessionSchema,
  type SessionExplorerSession,
} from '@/core/schema';
import { insightsStatsSchema } from '@/core/insights';
import { SessionExplorerError } from './errors';
import { listRows } from './memoryListing';
import { parseMapBytes } from './parse';
import type { SessionMapSource } from './source';

type DemoRow = SessionExplorerSession & { file?: string };

/**
 * The bundled synthetic sessions: `demo/index.json` (`{sessions: [row +
 * file]}`) and one `demo/<file>` map document per row, as static files next
 * to the page. `root` is resolved against the page, so the build works from
 * any path.
 */
/**
 * A demo file URL, refused unless it is on the page's own origin: `file`
 * entries come from `index.json`, so they must not point the page elsewhere.
 */
export function sameOriginUrl(path: string, base: URL): URL {
  const url = new URL(path, base);
  if (url.origin !== base.origin) throw new SessionExplorerError('demo file is not on this site', 'not_found');
  return url;
}

export function demoSource(root = 'demo/'): SessionMapSource {
  const base = new URL(root, document.baseURI);
  let raw: Promise<{ sessions?: unknown[] }> | null = null;
  let index: Promise<DemoRow[]> | null = null;
  const loadRaw = () =>
    // Same-origin only (sameOriginUrl), and this runs in the browser: no server-side request.
    // nosemgrep: app.repo_health.semgrep_rules.entelligence.javascript.ssrf.fetch-dynamic-url
    (raw ??= fetch(sameOriginUrl('index.json', base))
      .then((r) => {
        if (!r.ok) throw new SessionExplorerError('demo index missing', 'not_found');
        return r.json() as Promise<{ sessions?: unknown[] }>;
      })
      .catch((err) => {
        raw = null;
        throw err;
      }));
  const load = () =>
    (index ??= loadRaw()
      .then((body) =>
        (body.sessions ?? []).map((raw) => {
          const row = sessionExplorerSessionSchema.parse(raw) as DemoRow;
          return row;
        })
      )
      .catch((err) => {
        index = null;
        throw err;
      }));
  return {
    id: 'demo',
    label: 'Demo sessions',
    async status() {
      const rows = await load();
      const latest = rows.map((r) => r.sessionDate ?? '').sort().pop() || null;
      return { success: true, available: rows.length > 0, mapCount: rows.length, latestSessionDate: latest, scanning: false };
    },
    async sessions(params) {
      return listRows(await load(), params);
    },
    async maps(ids) {
      const rows = await load();
      return {
        success: true,
        expiresIn: 3600,
        maps: ids.map((id) => {
          const row = rows.find((r) => r.sessionId === id);
          return row?.file
            ? { sessionId: id, url: new URL(row.file, base).href, schemaVersion: 1, contentHash: row.contentHash ?? id, bytesGz: row.bytesGz }
            : { sessionId: id, status: 'not_found' as const };
        }),
      };
    },
    async stats(range) {
      // The demo index rows carry the same facts `/api/stats` serves
      // (`scripts/demo_facts.py`); the row schema above drops them, so read raw.
      const body = await loadRaw();
      const rows = (body.sessions ?? []).filter((r) => {
        const day = (r as { sessionDate?: string | null }).sessionDate ?? '';
        return (!range.from || day >= range.from) && (!range.to || day <= range.to);
      });
      return insightsStatsSchema.parse({ sessions: rows });
    },
    async fetchMap(meta) {
      // Same-origin only (sameOriginUrl); index entries cannot point the page elsewhere.
      // nosemgrep: app.repo_health.semgrep_rules.entelligence.javascript.ssrf.fetch-dynamic-url
      const r = await fetch(sameOriginUrl(meta.url, base));
      if (!r.ok) throw new SessionExplorerError(`demo map ${r.status}`, 'not_found');
      return parseMapBytes(new Uint8Array(await r.arrayBuffer()));
    },
  };
}
