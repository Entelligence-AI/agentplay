import { z } from 'zod';

/**
 * Session Explorer API boundary.
 *
 * The three data endpoints (`status`, `sessions`, `map`) answer in camelCase,
 * and the map document itself is `agentplay/v1` (the older
 * `entelligence.session-map/v1` name is the same document shape and is
 * accepted too), fetched from whatever URL the data source's `map` batch hands back.
 *
 * The map schema is validated loosely: every object is `passthrough` and
 * optional fields default, so a CLI that adds a field never breaks the page.
 * Only the `schema` discriminator is strict, because a v2 map is not something
 * this renderer can draw.
 */

export const SESSION_MAP_SCHEMA_V1 = 'agentplay/v1';
/** The same v1 document under its original name; readers accept both. */
export const SESSION_MAP_SCHEMA_V1_LEGACY = 'entelligence.session-map/v1';

export const sessionExplorerStatusSchema = z
  .object({
    success: z.boolean().optional(),
    available: z.boolean().catch(false).default(false),
    mapCount: z.number().catch(0).default(0),
    latestSessionDate: z.string().nullable().catch(null).default(null),
    /** the local server is still scanning for sessions, so more may land */
    scanning: z.boolean().catch(false).default(false),
  })
  .passthrough();
export type SessionExplorerStatus = z.infer<typeof sessionExplorerStatusSchema>;

/** Session-list `repos`: a server sends either plain names or
 * `{name, remote}` objects reduced from the commit summary. Normalize both
 * to the display name `SessionList` renders. */
const sessionRepoNameSchema = z
  .union([
    z.string(),
    z
      .object({ name: z.string().optional(), remote: z.string().optional() })
      .passthrough(),
  ])
  .transform((repo) =>
    (typeof repo === 'string' ? repo : (repo.name ?? repo.remote ?? '')).trim()
  )
  .catch('');

export const sessionExplorerSessionSchema = z
  .object({
    sessionId: z.string(),
    sessionDate: z.string().nullable().default(null),
    startedAt: z.string(),
    endedAt: z.string().nullable().default(null),
    durationMs: z.number().default(0),
    agentTool: z.string().default('claude-code'),
    models: z.array(z.string()).default([]),
    repos: z
      .array(sessionRepoNameSchema)
      .default([])
      .transform((repos) => repos.filter((r) => r.length > 0)),
    calls: z.number().default(0),
    requests: z.number().default(0),
    subagents: z.number().default(0),
    errors: z.number().default(0),
    filesTouched: z.number().default(0),
    costUsd: z.number().default(0),
    tokens: z.number().default(0),
    userTurns: z.number().default(0),
    sessionTitle: z.string().nullable().default(null),
    contentHash: z.string().nullable().default(null),
    bytesGz: z.number().nullable().default(null),
    updatedAt: z.string().nullable().default(null),
    // optional signal summary (render when present)
    errorsBySource: z
      .record(z.string(), z.number())
      .optional()
      .catch(undefined),
    retries: z.number().optional().catch(undefined),
    delays: z.number().optional().catch(undefined),
    maxContext: z.number().optional().catch(undefined),
    compactions: z.number().optional().catch(undefined),
    // All optional: an older server omits them.
    role: z.string().nullable().optional().catch(undefined),
    parentSessionId: z.string().nullable().optional().catch(undefined),
    tasks: z.number().optional().catch(undefined),
    outputs: z.record(z.string(), z.number()).optional().catch(undefined),
    prs: z
      .array(
        z
          .object({
            repo: z.string().optional(),
            number: z.number().optional(),
            url: z.string().optional(),
            state: z.string().optional(),
          })
          .passthrough()
      )
      .optional()
      .catch(undefined),
    // Present when the list was searched and this session matched on what it
    // touched (files, commands, MCP servers...), not only on its title.
    search: z
      .object({
        count: z.number().default(0),
        seqs: z.array(z.number()).default([]),
        chips: z
          .array(
            z.object({
              label: z.string(),
              touch: z.string(),
              seq: z.number().nullable().default(null),
              n: z.number().default(1),
            })
          )
          .default([]),
      })
      .optional()
      .catch(undefined),
  })
  .passthrough();
export type SessionExplorerSession = z.infer<
  typeof sessionExplorerSessionSchema
>;

export const sessionExplorerSessionsSchema = z
  .object({
    success: z.boolean().optional(),
    sessions: z.array(sessionExplorerSessionSchema).default([]),
    nextCursor: z.string().nullable().default(null),
    hasMore: z.boolean().default(false),
    // Set when a search named command arguments, which are never stored, and
    // the results are for the command alone.
    searchNote: z
      .object({ code: z.string(), command: z.string().optional() })
      .nullable()
      .optional()
      .catch(undefined),
  })
  .passthrough();
export type SessionExplorerSessionsPage = z.infer<
  typeof sessionExplorerSessionsSchema
>;

/** One map entry in the batch answer: where to fetch it and its hash. */
export const sessionExplorerMapMetaSchema = z
  .object({
    sessionId: z.string(),
    url: z.string().min(1),
    schemaVersion: z.number().default(1),
    contentHash: z.string().min(1),
    bytesGz: z.number().nullable().default(null),
  })
  .passthrough();
export type SessionExplorerMapMeta = z.infer<
  typeof sessionExplorerMapMetaSchema
>;

/** An id the server has no map for (unknown or removed row). */
export const sessionExplorerMapMissingSchema = z
  .object({ sessionId: z.string(), status: z.literal('not_found') })
  .passthrough();

/** `map/?session_id=A&session_id=B…` (1-20 ids). */
export const sessionExplorerMapBatchSchema = z
  .object({
    success: z.boolean().optional(),
    expiresIn: z.number().default(300),
    maps: z
      .array(
        z.union([sessionExplorerMapMetaSchema, sessionExplorerMapMissingSchema])
      )
      .default([]),
  })
  .passthrough();
export type SessionExplorerMapBatch = z.infer<
  typeof sessionExplorerMapBatchSchema
>;
export type SessionExplorerMapItem = SessionExplorerMapBatch['maps'][number];

/** `GET /api/prompt-images`: the images pasted into each user turn. `turn` is
 * 1-based over the session's main-agent user events (see `promptImageTurns`). */
export const promptImagesResponseSchema = z
  .object({
    success: z.boolean().optional(),
    turns: z
      .array(
        z
          .object({
            turn: z.number().int(),
            images: z
              .array(
                z
                  .object({
                    i: z.number().int(),
                    pasteId: z.number().nullable().catch(null).default(null),
                    mediaType: z.string().catch('').default(''),
                    available: z.boolean().catch(false).default(false),
                  })
                  .passthrough()
              )
              .default([]),
          })
          .passthrough()
      )
      .default([]),
  })
  .passthrough();
export type PromptImagesResponse = z.infer<typeof promptImagesResponseSchema>;

/** `GET /api/hook-detail`: what one failed hook said and ran. Free text that
 * a map never carries; the server reads it from the transcript on demand. */
export const hookDetailResponseSchema = z
  .object({
    success: z.boolean().optional(),
    message: z.string().nullable().catch(null).default(null),
    command: z.string().nullable().catch(null).default(null),
  })
  .passthrough();
export type HookDetailResponse = z.infer<typeof hookDetailResponseSchema>;

export const isMapFound = (
  item: SessionExplorerMapItem
): item is SessionExplorerMapMeta =>
  'url' in item && typeof item.url === 'string';

// --- map document ---------------------------------------------------------

// The CLI writes JSON, where an absent value is `null` (`"name": null`), not a
// missing key. `.optional()` alone rejects null, which took whole blocks down:
// `tasks` is wrapped in `.catch(undefined)`, so one null name silently discarded
// every task in the map and the viewer fell back to "Task 1", "Task 2", ...
const optNum = z.number().nullish();
const optStr = z.string().nullish();
const nullAsMissing = <T extends z.ZodTypeAny>(schema: T) =>
  z.preprocess((v) => (v === null ? undefined : v), schema);
const num0 = nullAsMissing(z.number().default(0));

const touchSchema = z.enum(['hit', 'read', 'edit']).catch('hit');

/** `targets[].op` / `files[].op`: closed vocabulary, but optional and
 * `.catch(undefined)` since most tool events touch no file at all. */
const fileOpSchema = z
  .enum(['create', 'modify', 'delete', 'rename-from', 'rename-to'])
  .optional()
  .catch(undefined);

const targetSchema = z
  .object({
    file: optNum,
    scope: z.string().optional(),
    label: optStr,
    touch: touchSchema,
    op: fileOpSchema,
  })
  .passthrough();

/** `events[].vcs`: a VCS/PR-hosting operation. `action` is an open
 * vocabulary (git subcommands, `<noun>_<verb>` for gh/glab) except the three
 * closed PR-creation states (`pr_create`, `pr_create_attempt`,
 * `pr_create_failed`), so it stays `z.string()` rather than an enum. */
const vcsSchema = z
  .object({
    provider: z.string(),
    action: z.string(),
    repo: optStr,
    number: optNum,
  })
  .passthrough()
  .optional();

export const sessionMapEventSchema = z
  .object({
    seq: optNum,
    t: z.number().default(0),
    kind: z.string(),
    agent: nullAsMissing(z.string().default('main')),
    err: nullAsMissing(z.boolean().default(false)),
    tool: optStr,
    verb: optStr,
    targets: z.array(targetSchema).optional(),
    // PR creation, tool categories
    vcs: vcsSchema,
    cat: optStr,
    outBytes: optNum,
    dur: optNum,
    server: optStr,
    skill: optStr,
    subagentType: optStr,
    model: optStr,
    child: optStr,
    host: optStr,
    in: optNum,
    cw: optNum,
    cr: optNum,
    out: optNum,
    cost: optNum,
    chars: optNum,
    // signals: tool/MCP/subagent failures and retries
    errClass: optStr,
    exitCode: optNum,
    retryOf: optNum,
    attempt: optNum,
    // llm response metadata (never text)
    latency: optNum,
    stop: optStr,
    thinking: z.boolean().nullish(),
    toolCalls: optNum,
    textChars: optNum,
    // error events
    source: optStr,
    hook: optStr,
    hookEvent: optStr,
    srcUuid: optStr,
    retry: z.boolean().nullish(),
    // delay events
    cause: optStr,
    ms: optNum,
    ref: optNum,
    // compaction events
    trigger: optStr,
    preTokens: optNum,
    postTokens: optNum,
    // Additive in v1: task number + driving model on
    // every event, prompt shape on user turns (counts only, never content),
    // and the fields of the output kinds (commit / pr_merge / pr_close /
    // pr_comment / ticket / artifact / skill_write / model_switch).
    task: optNum,
    images: optNum,
    urls: optNum,
    repo: optStr,
    sha: optStr,
    files: optNum,
    ins: optNum,
    del: optNum,
    number: optNum,
    url: optStr,
    tracker: optStr,
    action: optStr,
    id: optStr,
    title: optStr,
    bytes: optNum,
    version: optNum,
    name: optStr,
    from: optStr,
    to: optStr,
  })
  .passthrough();

/** `tasks[]`: the CLI's own segmentation (`source: "segments"`) or
 * one task per user-turn window (`"turns"`). `startSeq`/`endSeq` are
 * inclusive event sequence numbers (seq === index). */
export const sessionMapTaskSchema = z
  .object({
    n: z.number(),
    id: optStr,
    name: optStr,
    source: optStr,
    startSeq: optNum,
    endSeq: optNum,
    startT: optNum,
    endT: optNum,
    firstTurn: optNum,
    lastTurn: optNum,
  })
  .passthrough();

export const sessionMapSchema = z
  .object({
    schema: z.union([
      z.literal(SESSION_MAP_SCHEMA_V1),
      z.literal(SESSION_MAP_SCHEMA_V1_LEGACY),
    ]),
    session: z
      .object({
        id: z.string(),
        agentTool: nullAsMissing(z.string().default('claude-code')),
        cliVersion: optStr,
        startedAt: z.string(),
        durationMs: num0,
        models: z.array(z.string()).default([]),
        pathMode: optStr,
        truncated: nullAsMissing(z.boolean().default(false)),
        // one of pr_created | verified | blocked | interrupted |
        // unresolved_errors | completed | incomplete; open string so an
        // unrecognized value degrades to its own label instead of failing.
        outcome: optStr,
        role: z.string().nullable().optional(),
        parentSessionId: z.string().nullable().optional(),
      })
      .passthrough(),
    totals: z
      .object({
        calls: num0,
        requests: num0,
        userTurns: num0,
        subagents: num0,
        errors: num0,
        tokens: num0,
        costUsd: num0,
        filesTouched: num0,
        errorsBySource: z.record(z.string(), z.number()).default({}),
        errorsByClass: z.record(z.string(), z.number()).default({}),
        retries: optNum,
        delays: optNum,
        delayMs: optNum,
        maxContext: optNum,
        compactions: optNum,
        llmLatencyP50: optNum,
        llmLatencyP95: optNum,
        outputTokens: optNum,
        // PR creation, file ops
        prsCreated: optNum,
        filesCreated: optNum,
        filesDeleted: optNum,
      })
      .passthrough(),
    repos: z
      .array(
        z
          .object({ id: z.string(), name: z.string(), remote: optStr })
          .passthrough()
      )
      .default([]),
    files: z
      .array(
        z
          .object({
            id: z.number(),
            repo: z.string(),
            path: z.string(),
            reads: num0,
            edits: num0,
            hits: num0,
            first: optNum,
            // last op seen for this file across the whole doc; a lens must
            // not read this for its own scope (see fileCoverage/recomputeTotals)
            op: fileOpSchema,
          })
          .passthrough()
      )
      .default([]),
    // Confirmed PR/MRs; `seq` points at the creating event.
    // `totals.prsCreated` = length, kept separately since the producer
    // writes both.
    prs: z
      .array(
        z
          .object({
            provider: z.string(),
            repo: optStr,
            number: z.number(),
            url: z.string(),
            seq: z.number(),
            state: optStr,
          })
          .passthrough()
      )
      .default([]),
    agents: z
      .array(
        z
          .object({
            id: z.string(),
            parent: z.string().nullable().default(null),
            kind: nullAsMissing(z.string().default('main')),
            subagentType: optStr,
            launchSeq: optNum,
            start: num0,
            end: num0,
            calls: num0,
            requests: num0,
            tokens: num0,
            cost: num0,
            outcome: optStr,
          })
          .passthrough()
      )
      .default([]),
    events: z.array(sessionMapEventSchema).default([]),
    // A malformed tasks block must never fail the whole map: drop it and
    // fall back to per-user-turn windows.
    tasks: z.array(sessionMapTaskSchema).optional().catch(undefined),
  })
  .passthrough();

export type SessionMap = z.infer<typeof sessionMapSchema>;
export type SessionMapEvent = SessionMap['events'][number];
export type SessionMapTarget = NonNullable<SessionMapEvent['targets']>[number];
export type SessionMapFile = SessionMap['files'][number];
export type SessionMapAgent = SessionMap['agents'][number];
export type SessionMapPr = SessionMap['prs'][number];
export type SessionMapTask = z.infer<typeof sessionMapTaskSchema>;
export type FileOp = NonNullable<SessionMapFile['op']>;
