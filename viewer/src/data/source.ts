import type {
  HookDetailResponse,
  PromptImagesResponse,
  SessionExplorerMapBatch,
  SessionExplorerMapMeta,
  SessionExplorerSessionsPage,
  SessionExplorerStatus,
  SessionMap,
} from '@/core/schema';
import type { InsightsStats } from '@/core/insights';
import type { AutotuneStatus, FixPreview, HealthReport } from '@/core/health';

export interface SessionsParams {
  /** inclusive UTC `YYYY-MM-DD` */
  from?: string;
  to?: string;
  search?: string;
  pageSize?: number;
  cursor?: string | null;
}

/**
 * Where the explorer gets its data. Every method answers with the same
 * camelCase shapes the zod schemas in `core/schema.ts` parse, so a source is
 * swappable without touching a component.
 */
export interface SessionMapSource {
  /** stable id, folded into every query key so switching sources never
   * serves one source's cached answer for another */
  readonly id: string;
  /** short human label for the app bar ("Local server", "Demo", ...) */
  readonly label: string;
  status(): Promise<SessionExplorerStatus>;
  sessions(params: SessionsParams): Promise<SessionExplorerSessionsPage>;
  /** 1-20 ids; unknown ids come back as `{sessionId, status: 'not_found'}` */
  maps(ids: string[]): Promise<SessionExplorerMapBatch>;
  /** load one map document from its batch entry */
  fetchMap(meta: SessionExplorerMapMeta): Promise<SessionMap>;
  /**
   * Optional. Which images were pasted into each user turn of a session.
   * Only a source that sits next to the recording machine can serve them;
   * rejects when they are unavailable (another machine, imported, gone).
   * Absent on sources that never have the bytes (file, demo).
   */
  promptImages?(sessionId: string): Promise<PromptImagesResponse>;
  /** Optional. Same-origin URL of one pasted image's bytes (`turn` is
   * 1-based, `i` is the image's index within that turn). */
  promptImageUrl?(sessionId: string, turn: number, i: number): string;
  /**
   * Optional. The message and command of one failed hook, by the `srcUuid`
   * its error event carries. Same rules as `promptImages`: only a source next
   * to the recording machine can answer, and it rejects otherwise.
   */
  hookDetail?(sessionId: string, uuid: string): Promise<HookDetailResponse>;
  /**
   * Optional. Per-session facts for the Insights view, for root sessions in
   * the range (inclusive UTC `YYYY-MM-DD`; omitted = all time). Absent on a
   * source with no precomputed facts (opened files).
   */
  stats?(range: { from?: string; to?: string }): Promise<InsightsStats>;
  /** Optional, local server only: Run health and its fixes for the range. */
  health?(range: { from?: string; to?: string }): Promise<HealthReport>;
  /** Optional, local server only: a fix action's file before and after. */
  fixPreview?(p: { fix: string; i: number; file?: string; from?: string; to?: string }): Promise<FixPreview>;
  /** Optional, local server only: Autotune's settings, queue and log. */
  autotune?(): Promise<AutotuneStatus>;
  /** Optional, local server only: a write (`/api/fix`, `/api/repos`, `/api/autotune`). */
  write?(path: '/api/fix' | '/api/repos' | '/api/autotune', body: Record<string, unknown>): Promise<unknown>;
}

/** Max ids per `maps()` call. */
export const SESSION_MAP_BATCH_MAX = 20;
