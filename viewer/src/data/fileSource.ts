import type {
  SessionExplorerSession,
  SessionMap,
} from '@/core/schema';
import { SessionExplorerError } from './errors';
import { listRows } from './memoryListing';
import { fnv1a, parseMapBytes, rowFromMap } from './parse';
import type { SessionMapSource } from './source';

export interface OpenResult {
  added: string[];
  failed: { name: string; reason: string }[];
}

export interface FileSource extends SessionMapSource {
  /** parse and hold `.json` / `.json.gz` map files; re-opening a session
   * replaces its earlier copy */
  addFiles(files: Iterable<File>): Promise<OpenResult>;
  readonly size: number;
}

const REASON: Record<string, string> = {
  unsupported_schema: 'not an agentplay v1 document',
  invalid_file: 'not valid JSON',
  unsupported_file: 'gzip is not supported by this browser',
};

/**
 * Maps the user opened (drag-and-drop or file picker), held in memory only.
 * Nothing is uploaded anywhere: parsing happens in this tab.
 */
export function fileSource(): FileSource {
  const docs = new Map<string, { map: SessionMap; row: SessionExplorerSession }>();
  let generation = 0;
  const src: FileSource = {
    get id() {
      return `files:${generation}`;
    },
    label: 'Opened files',
    get size() {
      return docs.size;
    },
    async addFiles(files) {
      const out: OpenResult = { added: [], failed: [] };
      for (const f of files) {
        try {
          const bytes = new Uint8Array(await f.arrayBuffer());
          const map = await parseMapBytes(bytes);
          const row = rowFromMap(map, {
            contentHash: fnv1a(bytes),
            bytesGz: f.name.endsWith('.gz') ? bytes.length : null,
          });
          docs.set(row.sessionId, { map, row });
          out.added.push(row.sessionId);
        } catch (err) {
          const code = err instanceof SessionExplorerError ? err.code : '';
          const reason = code === 'invalid_map' ? (err as Error).message : REASON[code];
          out.failed.push({ name: f.name, reason: reason ?? 'could not be read' });
        }
      }
      if (out.added.length) generation++;
      return out;
    },
    async status() {
      const rows = [...docs.values()].map((d) => d.row);
      const latest = rows.map((r) => r.sessionDate ?? '').sort().pop() || null;
      return { success: true, available: rows.length > 0, mapCount: rows.length, latestSessionDate: latest, scanning: false };
    },
    async sessions(params) {
      return listRows([...docs.values()].map((d) => d.row), params);
    },
    async maps(ids) {
      return {
        success: true,
        expiresIn: 3600,
        maps: ids.map((id) => {
          const d = docs.get(id);
          return d
            ? { sessionId: id, url: `memory:${encodeURIComponent(id)}`, schemaVersion: 1, contentHash: d.row.contentHash ?? id, bytesGz: d.row.bytesGz }
            : { sessionId: id, status: 'not_found' as const };
        }),
      };
    },
    async fetchMap(meta) {
      const d = docs.get(meta.sessionId);
      if (!d) throw new SessionExplorerError('map not open', 'not_found');
      return d.map;
    },
  };
  return src;
}
