import { useCallback, useEffect, useRef } from 'react';
import {
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query';
import {
  isMapFound,
  type SessionExplorerMapBatch,
  type SessionExplorerMapMeta,
} from '@/core/schema';
import { fetchMapWithRetry, mapRequestIds } from '@/core/maps';
import { SessionExplorerError } from './errors';
import { useSessionMapSource } from './SourceContext';

export const sessionExplorerStatusKey = (sourceId: string) =>
  ['sessionExplorerStatus', sourceId] as const;

/** How often to re-ask while the server is still scanning. */
const SCANNING_POLL_MS = 2000;
/** The slower re-ask after a failed status call, while it still says scanning. */
const SCANNING_ERROR_POLL_MS = 30_000;

/** Whether the injected source has any maps. Fails closed: any error reads
 * as "not available" in the caller. Polls while the server reports a scan
 * in progress, and stops once it is done. */
export function useSessionExplorerStatus() {
  const source = useSessionMapSource();
  return useQuery({
    queryKey: sessionExplorerStatusKey(source.id),
    queryFn: () => source.status(),
    staleTime: 3 * 60 * 1000,
    retry: false,
    // An error keeps the last `scanning: true` in `data`, so a stopped server
    // would be polled every 2s forever. Back off instead of stopping, so a
    // server restarted on the same port is picked up again.
    refetchInterval: (q) => {
      if (!q.state.data?.scanning) return false;
      return q.state.status === 'error' ? SCANNING_ERROR_POLL_MS : SCANNING_POLL_MS;
    },
  });
}

const SESSION_EXPLORER_PAGE_SIZE = 50;

export function useSessionExplorerSessions(
  search: string,
  range: { from?: string; to?: string } = {},
  enabled = true
) {
  const source = useSessionMapSource();
  const queryClient = useQueryClient();
  // A background scan adds sessions after the list first loaded: refetch it
  // whenever the count moves or the scan ends. The first answer is only
  // recorded, so a cold load does not fetch the list twice.
  const status = useSessionExplorerStatus();
  const stamp = status.data ? `${status.data.mapCount}:${status.data.scanning}` : null;
  const lastStamp = useRef<string | null>(null);
  useEffect(() => {
    if (stamp == null) return;
    const prev = lastStamp.current;
    lastStamp.current = stamp;
    if (prev != null && prev !== stamp) {
      void queryClient.invalidateQueries({
        queryKey: ['sessionExplorerSessions', source.id],
      });
    }
  }, [stamp, queryClient, source.id]);
  return useInfiniteQuery({
    queryKey: [
      'sessionExplorerSessions',
      source.id,
      search,
      range.from ?? null,
      range.to ?? null,
    ],
    queryFn: ({ pageParam }) =>
      source.sessions({
        search: search || undefined,
        from: range.from,
        to: range.to,
        pageSize: SESSION_EXPLORER_PAGE_SIZE,
        cursor: pageParam,
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => (last.hasMore ? last.nextCursor : null),
    enabled,
    staleTime: 60 * 1000,
    // Search is in the key: keep the old list up while a new term loads so
    // the filter input is never unmounted mid-typing.
    placeholderData: keepPreviousData,
  });
}

export const sessionExplorerMapBatchKey = (
  sourceId: string,
  ids: readonly string[]
) => ['sessionExplorerMapBatch', sourceId, ids.join(',')] as const;

export const sessionExplorerMapDocKey = (
  sourceId: string,
  contentHash: string | null
) => ['sessionExplorerMap', sourceId, contentHash] as const;

/** Map URLs come from the data source (relative server URLs, or demo/file
 * sources). Some sources may issue URLs that expire, so re-resolve often. */
const MAP_URL_STALE_MS = 60 * 1000;

/**
 * Batch-resolve the selected session plus the next two in the list, load the
 * selected map now and prefetch the other two into the contentHash-keyed
 * cache (documents are immutable per hash, so they never go stale). A map
 * whose URL expired or whose download failed on the network is re-resolved
 * once before the caller shows its inline error.
 */
export function useSessionExplorerMap(
  sessionId: string | null,
  listIds: readonly string[]
) {
  const source = useSessionMapSource();
  const queryClient = useQueryClient();
  const batchRoot = ['sessionExplorerMapBatch', source.id] as const;
  const unavailable = new Set<string>();
  for (const [, data] of queryClient.getQueriesData<SessionExplorerMapBatch>({
    queryKey: batchRoot,
  })) {
    for (const m of data?.maps ?? []) if (!isMapFound(m)) unavailable.add(m.sessionId);
  }
  const ids = sessionId ? mapRequestIds(listIds, sessionId, unavailable) : [];
  const batchKey = sessionExplorerMapBatchKey(source.id, ids);
  const batch = useQuery({
    queryKey: batchKey,
    queryFn: () => source.maps(ids),
    enabled: ids.length > 0,
    staleTime: MAP_URL_STALE_MS,
    retry: 1,
    // Learning a neighbour has no map changes the id set; keep the answer
    // we have so the selected map does not flash back to a skeleton.
    placeholderData: keepPreviousData,
  });
  for (const m of batch.data?.maps ?? []) if (!isMapFound(m)) unavailable.add(m.sessionId);

  const items = batch.data?.maps ?? [];
  const selected = items.find((m) => m.sessionId === sessionId);
  const found = selected && isMapFound(selected) ? selected : null;

  const loadDoc = useCallback(
    (item: SessionExplorerMapMeta) =>
      fetchMapWithRetry(
        item.url,
        async () => {
          const fresh = await source.maps([item.sessionId]);
          const m = fresh.maps.find((x) => x.sessionId === item.sessionId);
          if (!m || !isMapFound(m)) {
            throw new SessionExplorerError('Session map not found', 'not_found');
          }
          return m.url;
        },
        (url) => source.fetchMap({ ...item, url })
      ),
    [source]
  );

  const doc = useQuery({
    queryKey: sessionExplorerMapDocKey(source.id, found?.contentHash ?? null),
    queryFn: () => loadDoc(found as SessionExplorerMapMeta),
    enabled: !!found,
    staleTime: Infinity,
    gcTime: 10 * 60 * 1000,
    retry: false,
  });

  // Prefetch the neighbours without blocking the selected map.
  useEffect(() => {
    for (const m of batch.data?.maps ?? []) {
      if (m.sessionId === sessionId || !isMapFound(m)) continue;
      void queryClient.prefetchQuery({
        queryKey: sessionExplorerMapDocKey(source.id, m.contentHash),
        queryFn: () => loadDoc(m),
        staleTime: Infinity,
        gcTime: 10 * 60 * 1000,
      });
    }
  }, [batch.data, sessionId, queryClient, loadDoc, source.id]);

  const notFound = !!selected && !found;
  const retry = async () => {
    await queryClient.invalidateQueries({ queryKey: batchKey });
    if (found) await doc.refetch();
  };
  return {
    data: doc.data ?? null,
    isLoading: batch.isLoading || (!!found && doc.isLoading),
    isError: batch.isError || doc.isError || notFound,
    error: notFound
      ? new SessionExplorerError('sessionMap not found', 'not_found')
      : (batch.error ?? doc.error ?? null),
    unavailable: unavailable as ReadonlySet<string>,
    retry,
  };
}

export const promptImagesKey = (sourceId: string, sessionId: string, throughTurn: number) =>
  ['promptImages', sourceId, sessionId, throughTurn] as const;

/**
 * Which images were pasted into each turn of a session. Idle unless the
 * source can serve them and the caller has images to show. No retry: a
 * refusal (other machine, imported, transcript gone) is a settled answer.
 *
 * Keyed by the last image turn the caller needs: a live session only appends
 * turns, so an answer that covers `throughTurn` never goes stale, and a newly
 * pasted image (a higher turn) asks again instead of reading as unavailable.
 */
export function usePromptImages(sessionId: string, throughTurn: number, enabled: boolean) {
  const source = useSessionMapSource();
  const promptImages = source.promptImages?.bind(source);
  return useQuery({
    queryKey: promptImagesKey(source.id, sessionId, throughTurn),
    queryFn: () => promptImages!(sessionId),
    enabled: enabled && !!promptImages,
    retry: false,
    staleTime: Infinity,
  });
}

export const hookDetailKey = (sourceId: string, sessionId: string, uuid: string) =>
  ['hookDetail', sourceId, sessionId, uuid] as const;

/**
 * What one failed hook said and ran. Idle unless the source can serve it and
 * the event carries a `srcUuid`. No retry: a refusal (other machine, imported,
 * transcript gone) is a settled answer, and a transcript record never changes.
 */
export function useHookDetail(sessionId: string, uuid: string | undefined) {
  const source = useSessionMapSource();
  const hookDetail = source.hookDetail?.bind(source);
  return useQuery({
    queryKey: hookDetailKey(source.id, sessionId, uuid ?? ''),
    queryFn: () => hookDetail!(sessionId, uuid!),
    enabled: !!uuid && !!hookDetail,
    retry: false,
    staleTime: Infinity,
  });
}

/** Per-session facts for the Insights view over a date range. Refetches when
 * a background scan adds maps, like the session list. Disabled on a source
 * without `stats` (opened files). */
export function useInsightsStats(range: { from?: string; to?: string }) {
  const source = useSessionMapSource();
  const status = useSessionExplorerStatus();
  const stamp = status.data ? `${status.data.mapCount}:${status.data.scanning}` : '';
  return useQuery({
    queryKey: ['insightsStats', source.id, range.from ?? '', range.to ?? '', stamp] as const,
    queryFn: () => {
      if (!source.stats) throw new SessionExplorerError('Insights need the local store', 'unsupported');
      return source.stats(range);
    },
    enabled: !!source.stats,
    staleTime: 60 * 1000,
    placeholderData: keepPreviousData,
    retry: false,
  });
}

/** Run health for a range. Local server only. */
export function useHealth(range: { from?: string; to?: string }) {
  const source = useSessionMapSource();
  const status = useSessionExplorerStatus();
  // Same as Insights: a scan adding maps changes the score, so it changes the key.
  const stamp = status.data ? `${status.data.mapCount}:${status.data.scanning}` : '';
  return useQuery({
    queryKey: ['health', source.id, range.from ?? '', range.to ?? '', stamp] as const,
    queryFn: () => {
      if (!source.health) throw new SessionExplorerError('Run health needs the local CLI', 'unsupported');
      return source.health(range);
    },
    enabled: !!source.health,
    staleTime: 30 * 1000,
    placeholderData: keepPreviousData,
    retry: false,
  });
}

/** Autotune status. Local server only. */
export function useAutotune() {
  const source = useSessionMapSource();
  return useQuery({
    queryKey: ['autotune', source.id] as const,
    queryFn: () => {
      if (!source.autotune) throw new SessionExplorerError('Autotune needs the local CLI', 'unsupported');
      return source.autotune();
    },
    enabled: !!source.autotune,
    staleTime: 15 * 1000,
    retry: false,
  });
}

/** A write to the local server; refreshes health and Autotune afterwards. */
export function useWrite() {
  const source = useSessionMapSource();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (v: { path: '/api/fix' | '/api/repos' | '/api/autotune'; body: Record<string, unknown> }) => {
      if (!source.write) throw new SessionExplorerError('Writing fixes needs the local CLI', 'unsupported');
      return source.write(v.path, v.body);
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ['health'] });
      void queryClient.invalidateQueries({ queryKey: ['autotune'] });
    },
  });
}
