import {
  sessionExplorerSessionSchema,
  sessionMapSchema,
  type SessionExplorerSession,
  type SessionMap,
} from '@/core/schema';
import { SessionExplorerError } from './errors';

/**
 * Rows with a repeated `sessionId` dropped, first occurrence kept. Pages use
 * an offset cursor, so a scan inserting newer rows between fetches shifts
 * later rows into the next page and repeats them.
 */
export function dedupeSessions<T extends { sessionId: string }>(
  rows: readonly T[]
): T[] {
  const seen = new Set<string>();
  return rows.filter((r) => !seen.has(r.sessionId) && !!seen.add(r.sessionId));
}

const isGzip = (b: Uint8Array) => b.length > 2 && b[0] === 0x1f && b[1] === 0x8b;

/** Inflate gzip bytes with the browser's own `DecompressionStream`. */
export async function gunzip(bytes: Uint8Array): Promise<Uint8Array> {
  if (typeof DecompressionStream === 'undefined') {
    throw new SessionExplorerError(
      'gzip is not supported by this browser',
      'unsupported_file'
    );
  }
  const stream = new Response(bytes as BodyInit).body!.pipeThrough(
    new DecompressionStream('gzip')
  );
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

/** Bytes (plain or gzipped JSON) to a validated map document. */
export async function parseMapBytes(bytes: Uint8Array): Promise<SessionMap> {
  const raw = isGzip(bytes) ? await gunzip(bytes) : bytes;
  let json: unknown;
  try {
    json = JSON.parse(new TextDecoder().decode(raw));
  } catch {
    throw new SessionExplorerError('map is not JSON', 'invalid_file');
  }
  return parseMapJson(json);
}

export function parseMapJson(json: unknown): SessionMap {
  const parsed = sessionMapSchema.safeParse(json);
  if (parsed.success) return parsed.data;
  const { path, code, message } = parsed.error.issues[0];
  if (!path.length || path[0] === 'schema') {
    throw new SessionExplorerError(
      'This file is not a valid agentplay session map',
      'unsupported_schema'
    );
  }
  // Name the field, never its value: zod's enum message quotes the value,
  // while a type error names only the two types.
  throw new SessionExplorerError(
    `${path.join('.')}: ${code === 'invalid_type' ? message : 'invalid value'}`,
    'invalid_map'
  );
}

/** A cheap, stable content hash (FNV-1a, 32 bit) for cache keys. */
export function fnv1a(bytes: Uint8Array): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < bytes.length; i++) {
    h ^= bytes[i];
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(16).padStart(8, '0');
}

/**
 * The session-list row for a map document, for sources that only have the
 * documents themselves (dropped files). Mirrors what a server reports.
 */
export function rowFromMap(
  map: SessionMap,
  extra: { contentHash: string; bytesGz?: number | null; title?: string | null }
): SessionExplorerSession {
  const s = map.session;
  const t = map.totals;
  const started = Date.parse(s.startedAt);
  const ended = Number.isFinite(started)
    ? new Date(started + (s.durationMs || 0)).toISOString()
    : null;
  const sessionTitle =
    extra.title ??
    (typeof (s as Record<string, unknown>).title === 'string'
      ? ((s as Record<string, unknown>).title as string)
      : null);
  const outputs: Record<string, number> = {};
  for (const e of map.events) {
    if (
      [
        'commit',
        'pr_merge',
        'pr_close',
        'pr_comment',
        'ticket',
        'artifact',
        'skill_write',
      ].includes(e.kind)
    )
      outputs[e.kind] = (outputs[e.kind] ?? 0) + 1;
  }
  return sessionExplorerSessionSchema.parse({
    sessionId: s.id,
    sessionDate: s.startedAt.slice(0, 10),
    startedAt: s.startedAt,
    endedAt: ended,
    durationMs: s.durationMs,
    agentTool: s.agentTool,
    models: s.models,
    repos: map.repos.map((r) => r.name),
    calls: t.calls,
    requests: t.requests,
    subagents: t.subagents,
    errors: t.errors,
    filesTouched: t.filesTouched,
    costUsd: t.costUsd,
    tokens: t.tokens,
    userTurns: t.userTurns,
    sessionTitle,
    contentHash: extra.contentHash,
    bytesGz: extra.bytesGz ?? null,
    updatedAt: null,
    errorsBySource: t.errorsBySource,
    retries: t.retries,
    delays: t.delays,
    maxContext: t.maxContext,
    compactions: t.compactions,
    role: s.role ?? null,
    parentSessionId: s.parentSessionId ?? null,
    tasks: map.tasks?.length,
    outputs,
    prs: map.prs.map((p) => ({
      repo: p.repo,
      number: p.number,
      url: p.url,
      state: p.state,
    })),
  });
}
