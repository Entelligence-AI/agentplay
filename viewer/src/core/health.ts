/**
 * Run health, fixes and Autotune: the shapes `/api/health`, `/api/fix-preview`
 * and `/api/autotune` answer with (computed by the CLI's `health.py`,
 * `fixes.py` and `autotune.py`). Lenient on purpose: an older or newer CLI
 * should degrade a field, not the page.
 */
import { z } from 'zod';

const n = z.number().catch(0);

export const fixActionSchema = z
  .object({
    kind: z.string(),
    label: z.string().catch('Apply'),
    file: z.string().catch(''),
    lines: z.array(z.string()).optional().catch(undefined),
    entry: z.string().optional().catch(undefined),
    command: z.string().optional().catch(undefined),
  })
  .passthrough();
export type FixAction = z.infer<typeof fixActionSchema>;

export const fixSchema = z.object({
  id: z.string(),
  rule: z.string().catch(''),
  repo: z.string().catch(''),
  component: z.string().catch(''),
  title: z.string(),
  pattern: z.string().catch(''),
  effect: z.string().catch(''),
  points: n,
  sessions: z.array(z.string()).catch([]),
  actions: z.array(fixActionSchema).catch([]),
});
export type Fix = z.infer<typeof fixSchema>;

const componentSchema = z.object({
  key: z.string(),
  name: z.string(),
  value: z.number().nullable().catch(null),
  points: z.number().nullable().catch(null),
  prevPoints: z.number().nullable().catch(null),
  inputs: z.array(z.tuple([z.string(), z.union([z.string(), z.number()])])).catch([]),
  worst: z.array(z.object({ sessionId: z.string(), why: z.string() })).catch([]),
});
export type HealthComponent = z.infer<typeof componentSchema>;

const repoSchema = z.object({
  name: z.string(),
  root: z.string().nullable().catch(null),
  granted: z.boolean().catch(false),
});
export type RepoAccess = z.infer<typeof repoSchema>;

export const healthReportSchema = z.object({
  sessions: n,
  minSessions: z.number().catch(10),
  score: z.number().nullable().catch(null),
  prevScore: z.number().nullable().catch(null),
  components: z.array(componentSchema).catch([]),
  fixes: z.array(z.unknown()).catch([]).transform((xs) =>
    xs.flatMap((x) => {
      const p = fixSchema.safeParse(x);
      return p.success ? [p.data] : [];
    })
  ),
  titles: z.record(z.string(), z.string().nullable()).catch({}),
  repos: z.array(repoSchema).catch([]),
  applied: z
    .array(z.object({ id: z.string(), fixId: z.string(), file: z.string(), kind: z.string().catch('') }))
    .catch([]),
});
export type HealthReport = z.infer<typeof healthReportSchema>;

export const fixPreviewSchema = z.object({
  file: z.string(),
  hash: z.string(),
  before: z.string().catch(''),
  after: z.string().catch(''),
});
export type FixPreview = z.infer<typeof fixPreviewSchema>;

const effectSchema = z
  .object({
    id: z.string(),
    title: z.string().nullable().catch(null),
    repo: z.string().catch(''),
    file: z.string().catch(''),
    source: z.string().catch(''),
    at: z.string().catch(''),
    baseline: z.number().nullable().catch(null),
    current: z.number().nullable().catch(null),
    sessionsSince: n,
    metric: z.object({ name: z.string() }).passthrough().nullable().catch(null),
  })
  .passthrough();
export type AppliedEffect = z.infer<typeof effectSchema>;

export const autotuneStatusSchema = z.object({
  when: z.string().nullable().catch('manual'),
  lastRun: z.string().nullable().catch(null),
  hookInstalled: z.boolean().catch(false),
  repos: z
    .array(repoSchema.extend({ enabled: z.boolean().catch(false), claudeMd: z.string().catch('ask') }))
    .catch([]),
  pending: z
    .array(
      z.object({
        fixId: z.string(),
        title: z.string(),
        pattern: z.string().catch(''),
        points: n,
        repo: z.string().catch(''),
        action: fixActionSchema,
      })
    )
    .catch([]),
  log: z
    .array(
      z
        .object({
          at: z.string().catch(''),
          kind: z.string(),
          title: z.string().nullable().catch(null),
          repo: z.string().nullable().catch(null),
          file: z.string().nullable().optional().catch(undefined),
          detail: z.string().nullable().optional().catch(undefined),
          entry: z.string().nullable().optional().catch(undefined),
        })
        .passthrough()
    )
    .catch([]),
  active: z.array(effectSchema).catch([]),
});
export type AutotuneStatus = z.infer<typeof autotuneStatusSchema>;

/** Lines `after` adds to `before`, for a compact preview (the writer only ever adds). */
export function addedLines(before: string, after: string): { text: string; added: boolean }[] {
  const old = new Set(before.split('\n'));
  return after
    .split('\n')
    .filter((l, i, all) => l.trim() || (i > 0 && i < all.length - 1))
    .map((text) => ({ text, added: !old.has(text) }));
}

/** The fix text an action writes, for copying when agentplay can't write it. */
export function actionText(a: FixAction): string {
  if (a.kind === 'claude_md') return (a.lines ?? []).map((l) => `- ${l}`).join('\n');
  if (a.kind === 'settings_allow') return JSON.stringify({ permissions: { allow: [a.entry] } }, null, 2);
  if (a.kind === 'stop_hook')
    return JSON.stringify({ hooks: { Stop: [{ hooks: [{ type: 'command', command: a.command }] }] } }, null, 2);
  return '';
}
