import {
  hookDetailResponseSchema,
  promptImagesResponseSchema,
  sessionExplorerMapBatchSchema,
  sessionExplorerSessionsSchema,
  sessionExplorerStatusSchema,
  type SessionMap,
} from '@/core/schema';
import { insightsStatsSchema } from '@/core/insights';
import { autotuneStatusSchema, fixPreviewSchema, healthReportSchema } from '@/core/health';
import { SessionExplorerError } from './errors';
import { parseMapBytes } from './parse';
import { SESSION_MAP_BATCH_MAX, type SessionMapSource } from './source';

async function failure(r: Response, what: string): Promise<SessionExplorerError> {
  let code = r.status === 404 ? 'not_found' : 'internal_error';
  try {
    const body: unknown = await r.json();
    if (
      body &&
      typeof body === 'object' &&
      'code' in body &&
      typeof body.code === 'string'
    ) {
      code = body.code;
    }
  } catch {
    // non-JSON error body: keep the status-derived code
  }
  return new SessionExplorerError(`${what} failed: ${r.status}`, code);
}

/** Parse a JSON API answer, refusing an HTML fallback page (a static host
 * that answers every path with index.html is not an agentplay server). */
async function json(r: Response, what: string): Promise<unknown> {
  const type = r.headers.get('content-type') ?? '';
  if (!type.includes('json')) {
    throw new SessionExplorerError(`${what}: not a JSON answer`, 'no_server');
  }
  return r.json();
}

/**
 * The local `agentplay` CLI server: `GET {base}/api/status`,
 * `/api/sessions`, `/api/map`, then each map's own `url`. `baseUrl` is ''
 * for same-origin (the CLI serves this viewer), or e.g.
 * `http://127.0.0.1:8765` when the viewer runs from `vite dev`.
 */
export function httpSource(baseUrl: string): SessionMapSource {
  const base = baseUrl.replace(/\/+$/, '');
  const get = (path: string) =>
    fetch(`${base}${path}`, { headers: { Accept: 'application/json' } });
  return {
    id: `http:${base}`,
    label: 'Local sessions',
    async status() {
      const r = await get('/api/status');
      if (!r.ok) throw await failure(r, 'status');
      return sessionExplorerStatusSchema.parse(await json(r, 'status'));
    },
    async sessions(params) {
      const qs = new URLSearchParams();
      if (params.from) qs.set('from', params.from);
      if (params.to) qs.set('to', params.to);
      if (params.search) qs.set('search', params.search);
      if (params.pageSize) qs.set('pageSize', String(params.pageSize));
      if (params.cursor) qs.set('cursor', params.cursor);
      const r = await get(`/api/sessions?${qs.toString()}`);
      if (!r.ok) throw await failure(r, 'sessions');
      return sessionExplorerSessionsSchema.parse(await json(r, 'sessions'));
    },
    async maps(ids) {
      const qs = new URLSearchParams();
      for (const id of ids.slice(0, SESSION_MAP_BATCH_MAX)) {
        qs.append('session_id', id);
      }
      const r = await get(`/api/map?${qs.toString()}`);
      if (!r.ok) throw await failure(r, 'map');
      return sessionExplorerMapBatchSchema.parse(await json(r, 'map'));
    },
    async fetchMap(meta): Promise<SessionMap> {
      // A relative url is the server's own map-doc route; an absolute one
      // is supplied by the source (not this server), so it must not carry credentials.
      const absolute = /^[a-z]+:\/\//i.test(meta.url);
      const r = await fetch(absolute ? meta.url : `${base}${meta.url}`, {
        credentials: absolute ? 'omit' : 'same-origin',
      });
      if (!r.ok) {
        throw new SessionExplorerError(
          `Could not load the session map (HTTP ${r.status})`,
          r.status === 403 ? 'expired' : r.status === 404 ? 'not_found' : 'internal_error'
        );
      }
      return parseMapBytes(new Uint8Array(await r.arrayBuffer()));
    },
    async promptImages(sessionId) {
      const qs = new URLSearchParams({ id: sessionId });
      const r = await get(`/api/prompt-images?${qs.toString()}`);
      if (!r.ok) throw await failure(r, 'promptImages');
      return promptImagesResponseSchema.parse(await json(r, 'promptImages'));
    },
    async stats(range) {
      const qs = new URLSearchParams();
      if (range.from) qs.set('from', range.from);
      if (range.to) qs.set('to', range.to);
      const r = await get(`/api/stats?${qs.toString()}`);
      if (!r.ok) throw await failure(r, 'stats');
      return insightsStatsSchema.parse(await json(r, 'stats'));
    },
    async health(range) {
      const qs = new URLSearchParams();
      if (range.from) qs.set('from', range.from);
      if (range.to) qs.set('to', range.to);
      const r = await get(`/api/health?${qs.toString()}`);
      if (!r.ok) throw await failure(r, 'health');
      return healthReportSchema.parse(await json(r, 'health'));
    },
    async fixPreview(p) {
      const qs = new URLSearchParams({ fix: p.fix, i: String(p.i) });
      if (p.file) qs.set('file', p.file);
      if (p.from) qs.set('from', p.from);
      if (p.to) qs.set('to', p.to);
      const r = await get(`/api/fix-preview?${qs.toString()}`);
      if (!r.ok) throw await failure(r, 'fixPreview');
      return fixPreviewSchema.parse(await json(r, 'fixPreview'));
    },
    async autotune() {
      const r = await get('/api/autotune');
      if (!r.ok) throw await failure(r, 'autotune');
      return autotuneStatusSchema.parse(await json(r, 'autotune'));
    },
    async write(path, body) {
      const r = await fetch(`${base}${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify(body),
      });
      const out: unknown = await r.json().catch(() => ({}));
      if (!r.ok) {
        const o = out as { code?: string; error?: string };
        throw new SessionExplorerError(o.error ?? `write failed: ${r.status}`, o.code ?? 'internal_error');
      }
      return out;
    },
    async hookDetail(sessionId, uuid) {
      const qs = new URLSearchParams({ id: sessionId, uuid });
      const r = await get(`/api/hook-detail?${qs.toString()}`);
      if (!r.ok) throw await failure(r, 'hookDetail');
      return hookDetailResponseSchema.parse(await json(r, 'hookDetail'));
    },
    promptImageUrl(sessionId, turn, i) {
      const qs = new URLSearchParams({
        id: sessionId,
        turn: String(turn),
        i: String(i),
      });
      return `${base}/api/prompt-image?${qs.toString()}`;
    },
  };
}

/** True when `{base}/api/status` answers like an agentplay server. */
export async function probeServer(baseUrl: string): Promise<boolean> {
  try {
    const r = await fetch(`${baseUrl.replace(/\/+$/, '')}/api/status`, {
      headers: { Accept: 'application/json' },
    });
    if (!r.ok || !(r.headers.get('content-type') ?? '').includes('json')) {
      return false;
    }
    const body: unknown = await r.json();
    return !!body && typeof body === 'object' && 'available' in body;
  } catch {
    return false;
  }
}
