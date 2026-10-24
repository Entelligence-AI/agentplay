// @vitest-environment node
import fs from 'node:fs';
import path from 'node:path';
import { gzipSync } from 'node:zlib';
import { describe, expect, it } from 'vitest';
import { SESSION_MAP_SCHEMA_V1_LEGACY, sessionMapSchema } from '@/core/schema';
import { listRows } from './memoryListing';
import { dedupeSessions, fnv1a, parseMapBytes, parseMapJson, rowFromMap } from './parse';
import { fileSource } from './fileSource';

const DEMO = path.resolve(__dirname, '../../public/demo');
const demoFiles = fs.readdirSync(DEMO).filter((f) => f !== 'index.json');
const read = (f: string) => JSON.parse(fs.readFileSync(path.join(DEMO, f), 'utf8'));

describe('demo data', () => {
  it('ships at least three valid map documents', () => {
    expect(demoFiles.length).toBeGreaterThanOrEqual(3);
    for (const f of demoFiles) {
      expect(sessionMapSchema.safeParse(read(f)).success).toBe(true);
    }
  });

  it('indexes every map file, with titles marked as demo', () => {
    const index = read('index.json') as { sessions: { file: string; sessionTitle: string }[] };
    expect(index.sessions.map((s) => s.file).sort()).toEqual([...demoFiles].sort());
    for (const s of index.sessions) expect(s.sessionTitle.startsWith('Demo: ')).toBe(true);
  });
});

describe('parseMapBytes', () => {
  const doc = read(demoFiles[0]);
  const bytes = new TextEncoder().encode(JSON.stringify(doc));

  it('reads plain JSON', async () => {
    const map = await parseMapBytes(bytes);
    expect(map.session.id).toBe(doc.session.id);
  });

  it('reads gzipped JSON', async () => {
    const map = await parseMapBytes(new Uint8Array(gzipSync(bytes)));
    expect(map.events.length).toBe(doc.events.length);
  });

  it('accepts the legacy schema name', async () => {
    const legacy = { ...doc, schema: SESSION_MAP_SCHEMA_V1_LEGACY };
    const map = await parseMapBytes(new TextEncoder().encode(JSON.stringify(legacy)));
    expect(map.schema).toBe('entelligence.session-map/v1');
  });

  it('rejects another schema with a typed code', async () => {
    const v2 = new TextEncoder().encode(JSON.stringify({ ...doc, schema: 'agentplay/v2' }));
    await expect(parseMapBytes(v2)).rejects.toMatchObject({ code: 'unsupported_schema' });
    await expect(parseMapBytes(new TextEncoder().encode('{'))).rejects.toMatchObject({
      code: 'invalid_file',
    });
  });

  it('hashes deterministically', () => {
    expect(fnv1a(new TextEncoder().encode('abc'))).toBe('1a47e90b');
  });
});

describe('parseMapJson', () => {
  type Node = { $ref?: string; type?: string | string[]; enum?: unknown[]; properties?: Record<string, Node>; items?: Node };
  type Path = (string | number)[];
  const schema = JSON.parse(
    fs.readFileSync(path.resolve(__dirname, '../../../schema/agentplay-v1.schema.json'), 'utf8')
  );
  const resolve = (n: Node): Node =>
    n.$ref ? resolve(n.$ref.slice(2).split('/').reduce((o, k) => o[k], schema)) : n;
  const nullablePaths = (node: Node, at: Path = []): Path[] => {
    const n = resolve(node);
    const nullable = (Array.isArray(n.type) && n.type.includes('null')) || !!n.enum?.includes(null);
    return [
      ...(at.length && nullable ? [at] : []),
      ...Object.entries(n.properties ?? {}).flatMap(([k, v]) => nullablePaths(v, [...at, k])),
      ...(n.items ? nullablePaths(n.items, [...at, 0]) : []),
    ];
  };
  const get = (o: unknown, p: Path) => p.reduce<any>((x, k) => x?.[k], o);
  const withoutNulls = (v: unknown) =>
    JSON.parse(JSON.stringify(v, (_k, x) => (x === null ? undefined : x)));
  const oneOfEach = () => ({
    schema: 'agentplay/v1',
    session: { id: 's1', startedAt: '2026-10-01T00:00:00Z' },
    totals: {},
    repos: [{ id: 'r1', name: 'api' }],
    files: [{ id: 0, repo: 'r1', path: 'src/a.ts' }],
    prs: [{ provider: 'github', number: 1, url: 'https://example.test/pr/1', seq: 0 }],
    agents: [{ id: 'main' }],
    tasks: [{ n: 1 }],
    events: [
      { t: 0, kind: 'tool', targets: [{ touch: 'read' }], vcs: { provider: 'git', action: 'commit' } },
    ],
  });

  it('reads every field the JSON Schema allows as null the same as a missing field', () => {
    const paths = nullablePaths(schema);
    expect(paths.length).toBeGreaterThan(100);
    const doc = oneOfEach();
    for (const p of [...paths].reverse()) get(doc, p.slice(0, -1))[p[p.length - 1]] = null;
    expect(withoutNulls(parseMapJson(doc))).toEqual(withoutNulls(parseMapJson(oneOfEach())));
  });

  it('names the first invalid field without echoing its value', async () => {
    const bad = { ...oneOfEach(), files: [{ id: 0, repo: 'r1', path: 'src/a.ts', reads: 'do not echo' }] };
    expect(() => parseMapJson(bad)).toThrow(expect.objectContaining({ code: 'invalid_map' }));
    const res = await fileSource().addFiles([
      new File([JSON.stringify(bad)], 'bad-field.json'),
      new File([JSON.stringify({ ...bad, schema: 'agentplay/v2' })], 'v2.json'),
    ]);
    const [field, v2] = res.failed;
    expect(field.reason).toContain('files.0.reads');
    expect(field.reason).not.toContain('do not echo');
    expect(v2.reason).toBe('not an agentplay v1 document');
  });
});

describe('rowFromMap + listRows', () => {
  const rows = demoFiles.map((f) =>
    rowFromMap(sessionMapSchema.parse(read(f)), { contentHash: f })
  );

  it('derives a list row from the document totals', () => {
    const doc = sessionMapSchema.parse(read(demoFiles[0]));
    const row = rowFromMap(doc, { contentHash: 'h' });
    expect(row.calls).toBe(doc.totals.calls);
    expect(row.repos).toEqual(doc.repos.map((r) => r.name));
    expect(row.sessionDate).toBe(doc.session.startedAt.slice(0, 10));
  });

  it('sorts newest first and pages with an opaque cursor', () => {
    const p1 = listRows(rows, { pageSize: 2 });
    expect(p1.sessions).toHaveLength(2);
    expect(p1.hasMore).toBe(true);
    expect(p1.sessions[0].startedAt >= p1.sessions[1].startedAt).toBe(true);
    const p2 = listRows(rows, { pageSize: 2, cursor: p1.nextCursor });
    expect(p2.hasMore).toBe(false);
    expect(p2.sessions.map((s) => s.sessionId)).not.toContain(p1.sessions[0].sessionId);
  });

  it('filters by inclusive date window and search', () => {
    const day = rows[0].sessionDate as string;
    expect(listRows(rows, { from: day, to: day }).sessions.every((s) => s.sessionDate === day)).toBe(true);
    expect(listRows(rows, { search: 'codex' }).sessions.map((s) => s.agentTool)).toEqual(['codex']);
  });
});

describe('fileSource', () => {
  it('holds opened files in memory and reports bad ones', async () => {
    const src = fileSource();
    const good = new File([fs.readFileSync(path.join(DEMO, demoFiles[0]))], demoFiles[0]);
    const bad = new File(['nope'], 'bad.json');
    const res = await src.addFiles([good, bad]);
    expect(res.added).toHaveLength(1);
    expect(res.failed).toEqual([{ name: 'bad.json', reason: 'not valid JSON' }]);
    const batch = await src.maps([res.added[0], 'missing']);
    expect(batch.maps[1]).toEqual({ sessionId: 'missing', status: 'not_found' });
    const status = await src.status();
    expect(status.mapCount).toBe(1);
  });
});

describe('dedupeSessions', () => {
  it('keeps the first occurrence of a repeated sessionId, in order', () => {
    const rows = [
      { sessionId: 'a', n: 1 },
      { sessionId: 'b', n: 2 },
      { sessionId: 'a', n: 3 },
      { sessionId: 'c', n: 4 },
      { sessionId: 'b', n: 5 },
    ];
    expect(dedupeSessions(rows)).toEqual([
      { sessionId: 'a', n: 1 },
      { sessionId: 'b', n: 2 },
      { sessionId: 'c', n: 4 },
    ]);
  });

  it('returns an empty list for no rows', () => {
    expect(dedupeSessions([])).toEqual([]);
  });
});
