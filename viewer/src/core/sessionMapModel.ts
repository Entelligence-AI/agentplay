/**
 * Pure model for the Session Explorer: tree building, radial layout, playhead
 * state, review metrics and event descriptions. No React, no DOM, so every
 * rule here is unit-tested in `sessionMapModel.test.ts`.
 */
import type {
  FileOp,
  SessionMap,
  SessionMapAgent,
  SessionMapEvent,
} from './schema';

export type NodeType =
  | 'root'
  | 'repo'
  | 'dir'
  | 'file'
  | 'group'
  | 'place'
  | 'server'
  | 'mcptool'
  | 'skill'
  | 'agent'
  | 'web'
  | 'errsrc'
  | 'errclass'
  // The model that drove each request, and what the session produced
  | 'model'
  | 'outgroup'
  | 'output';

export interface TreeNode {
  id: string;
  name: string;
  type: NodeType;
  children: TreeNode[];
  parent: TreeNode | null;
  depth: number;
  /** repo-relative path, files only */
  path?: string;
  repoName?: string;
  /** error source, errsrc/errclass nodes only */
  src?: ErrorSource;
  /** output event kind, output nodes only (drives the node colour) */
  kind?: string;
  /** angle (radians) and radius from layoutTree */
  a: number;
  r: number;
  x: number;
  y: number;
}

export interface SessionTree {
  root: TreeNode;
  /** every id, including ids folded away by chain collapsing, -> canonical node */
  byId: Map<string, TreeNode>;
  leaves: TreeNode[];
  maxDepth: number;
}

/** 1 = seen (hit), 2 = read, 3 = edit. Non-file nodes carry no level. */
export type TouchLevel = 1 | 2 | 3;
export const TOUCH_LEVEL: Record<'hit' | 'read' | 'edit', TouchLevel> = {
  hit: 1,
  read: 2,
  edit: 3,
};

export interface NodeRef {
  id: string;
  lvl?: TouchLevel;
  /** the error-class node an errored event lights */
  errNode?: boolean;
  /** this event failed while touching this node */
  failed?: boolean;
  /** counted and coloured, but not part of the playback trail or the active
   * label set (model nodes: every request lights one) */
  passive?: boolean;
}

const GROUP_ORDER: Partial<Record<NodeType, number>> = { repo: 0, group: 1 };

// --- signal vocabulary ---------------------------------

export type ErrorSource = 'tool' | 'mcp' | 'llm' | 'hook' | 'orchestration';
export const ERROR_SOURCES: ErrorSource[] = [
  'tool',
  'mcp',
  'llm',
  'hook',
  'orchestration',
];
export const ERROR_SOURCE_LABEL: Record<ErrorSource, string> = {
  tool: 'Tool call',
  mcp: 'MCP',
  llm: 'LLM / API',
  hook: 'Hook',
  orchestration: 'Agent orchestration',
};
export const ERROR_CLASS_LABEL: Record<string, string> = {
  exit_code: 'non-zero exit',
  timeout: 'timeout',
  not_found: 'not found',
  validation: 'invalid input',
  needs_approval: 'needs approval',
  permission_denied: 'permission denied',
  blocked: 'blocked',
  browser: 'browser action failed',
  patch_failed: 'patch did not apply',
  stale_file: 'stale file',
  model_unavailable: 'model unavailable',
  rate_limit: 'rate limited',
  api_error: 'API error',
  interrupted: 'interrupted by user',
  no_result: 'no result (killed)',
  spend_limit: 'spend limit hit',
  usage_limit: 'usage limit hit',
  server_error: 'server error',
  overloaded: 'overloaded',
  auth: 'auth failed',
  bad_request: 'bad request',
  refusal_fallback: 'refusal, model fallback',
  hook_blocking: 'hook blocked',
  hook_error: 'hook failed',
  subagent_failed: 'subagent failed',
  subagent_killed: 'subagent killed',
  other: 'other error',
};
export const DELAY_CAUSE_LABEL: Record<string, string> = {
  tool: 'slow tool call',
  subagent: 'slow subagent',
  llm: 'slow LLM response',
  stall: 'stall (no activity)',
};
/** Output kinds. Each is a confirmed annotation the CLI
 * emits next to the tool call that produced it, so none counts as a call. */
export const OUTPUT_KINDS = new Set([
  'commit',
  'pr_merge',
  'pr_close',
  'pr_comment',
  'ticket',
  'artifact',
  'skill_write',
]);

/** Kinds that are not tool calls (contract: `calls` excludes these). */
export const NON_CALL = new Set([
  'llm',
  'user',
  'compaction',
  'error',
  'delay',
  'model_switch',
  ...OUTPUT_KINDS,
]);

/** An output of the session: an output kind, or a confirmed PR
 * creation (recorded on the creating tool call's `vcs`). */
export const isOutputEvent = (e: SessionMapEvent): boolean =>
  OUTPUT_KINDS.has(e.kind) || e.vcs?.action === 'pr_create';

type OutputBucket = 'github' | 'tickets' | 'artifacts' | 'skills';
const OUTPUT_BUCKET_LABEL: Record<OutputBucket, string> = {
  github: 'GitHub',
  tickets: 'tickets',
  artifacts: 'artifacts',
  skills: 'skills written',
};

/** Where an output event sits on the map: bucket, stable node id and label.
 * Every event on the same PR (opened, commented, merged, closed) lands on
 * one `pr:` node so the PR reads as one thing that happened to it. */
export function outputNodeOf(
  e: SessionMapEvent,
  i: number
): { bucket: OutputBucket; id: string; name: string } | null {
  const prNum = e.number ?? e.vcs?.number;
  const prRepo = e.repo ?? e.vcs?.repo ?? '';
  if (
    e.kind === 'pr_merge' ||
    e.kind === 'pr_close' ||
    e.kind === 'pr_comment' ||
    e.vcs?.action === 'pr_create'
  ) {
    const key = prNum != null ? `${prRepo}#${prNum}` : `seq${i}`;
    return {
      bucket: 'github',
      id: `pr:${key}`,
      name: prNum != null ? `PR #${prNum}` : 'pull request',
    };
  }
  switch (e.kind) {
    case 'commit':
      return {
        bucket: 'github',
        id: `commit:${e.sha ?? `seq${i}`}`,
        name: e.sha ? `commit ${e.sha.slice(0, 7)}` : 'commit',
      };
    case 'ticket':
      return {
        bucket: 'tickets',
        id: `ticket:${e.id ?? `seq${i}`}`,
        name: e.id ?? `${e.tracker ?? 'ticket'} ${e.action ?? ''}`.trim(),
      };
    case 'artifact':
      return {
        bucket: 'artifacts',
        id: `artifact:${e.title ?? `seq${i}`}`,
        name: e.title ?? 'artifact',
      };
    case 'skill_write':
      return {
        bucket: 'skills',
        id: `skillw:${e.name ?? `seq${i}`}`,
        name: e.name ?? 'skill',
      };
    default:
      return null;
  }
}

const isErrorSource = (v: unknown): v is ErrorSource =>
  typeof v === 'string' && (ERROR_SOURCES as string[]).includes(v);

/** Error source per contract: `error.source`; else `mcp` for MCP events,
 * `orchestration` for subagent events, `tool` otherwise. */
export function errSource(e: SessionMapEvent): ErrorSource {
  if (e.kind === 'error') return isErrorSource(e.source) ? e.source : 'tool';
  if (e.kind === 'mcp') return 'mcp';
  if (e.kind === 'subagent') return 'orchestration';
  return 'tool';
}

export const errClassOf = (e: SessionMapEvent): string => e.errClass || 'other';
export const errClassLabel = (cls: string): string =>
  ERROR_CLASS_LABEL[cls] ?? cls;

/** "PostToolUse:Bash hook error" or "Stop hook blocked" for a failed hook, so a
 * row names the hook instead of "error". Undefined for any other event. */
export const hookErrorTitle = (e: SessionMapEvent): string | undefined => {
  if (e.source !== 'hook') return undefined;
  const verdict = e.errClass === 'hook_blocking' ? 'blocked' : 'error';
  return `${e.hook ? `${e.hook} hook` : 'Hook'} ${verdict}`;
};

/** Context sent with one LLM request: fresh + cache write + cache read. */
export const ctxOf = (e: SessionMapEvent): number =>
  (e.in ?? 0) + (e.cw ?? 0) + (e.cr ?? 0);

// --- PR creation, tool categories, file ops, outcome, agent lens
// ----------------------------------------------------------------------------

/** `events[].cat`: closed vocabulary for the tool-category breakdown.
 * `test` and `build` share the base `verify` kind and land in the same
 * label/colour bucket (contract). */
export type ToolCategory =
  | 'file'
  | 'shell'
  | 'vcs'
  | 'test'
  | 'build'
  | 'cloud'
  | 'http'
  | 'fs'
  | 'package'
  | 'mcp'
  | 'subagent'
  | 'skill'
  | 'web'
  | 'tool';

export const CAT_LABEL: Record<ToolCategory, string> = {
  file: 'File',
  shell: 'Shell',
  vcs: 'Git / VCS',
  test: 'Test / verify',
  build: 'Test / verify',
  cloud: 'Cloud / infra',
  http: 'HTTP',
  fs: 'Filesystem',
  package: 'Package manager',
  mcp: 'MCP (other)',
  subagent: 'Subagent',
  skill: 'Skill',
  web: 'Web',
  tool: 'Other tool',
};
const isToolCategory = (v: unknown): v is ToolCategory =>
  typeof v === 'string' && v in CAT_LABEL;
export const catLabel = (cat: string): string =>
  isToolCategory(cat) ? CAT_LABEL[cat] : cat;

/** `targets[].op` / `files[].op` glyph and past-tense label. */
export const OP_GLYPH: Record<FileOp, string> = {
  create: '+',
  modify: '~',
  delete: '−',
  'rename-from': '→',
  'rename-to': '→',
};
export const OP_LABEL: Record<FileOp, string> = {
  create: 'created',
  modify: 'modified',
  delete: 'deleted',
  'rename-from': 'renamed from',
  'rename-to': 'renamed to',
};

/** `session.outcome` / `agents[].outcome`: closed 7-way vocabulary. */
export type SessionOutcome =
  | 'pr_created'
  | 'verified'
  | 'completed'
  | 'blocked'
  | 'unresolved_errors'
  | 'interrupted'
  | 'incomplete';

export interface OutcomeMeta {
  label: string;
  /** shadcn Badge variant. `success` is a genuine terminal status (PR
   * opened / verified), not a positive metric, so it is the documented
   * exception to the rule that positive metrics use the brand badge. */
  variant: 'success' | 'secondary' | 'error' | 'warning' | 'outline';
}
export const OUTCOME_META: Record<SessionOutcome, OutcomeMeta> = {
  pr_created: { label: 'PR opened', variant: 'success' },
  verified: { label: 'Verified', variant: 'success' },
  completed: { label: 'Completed', variant: 'secondary' },
  blocked: { label: 'Blocked', variant: 'error' },
  unresolved_errors: { label: 'Unresolved errors', variant: 'error' },
  interrupted: { label: 'Interrupted', variant: 'warning' },
  incomplete: { label: 'Incomplete', variant: 'outline' },
};
const isSessionOutcome = (v: unknown): v is SessionOutcome =>
  typeof v === 'string' && v in OUTCOME_META;
export const outcomeMeta = (outcome: string | undefined | null): OutcomeMeta =>
  isSessionOutcome(outcome)
    ? OUTCOME_META[outcome]
    : { label: outcome ?? 'Unknown', variant: 'outline' };

/** `events[].vcs.action`: open vocabulary (git subcommand, or
 * `<noun>_<verb>` for gh/glab); the three PR-creation states are closed. */
const VCS_ACTION_LABEL: Record<string, string> = {
  pr_create: 'opened a pull request',
  pr_create_attempt: 'tried to open a pull request',
  pr_create_failed: 'failed to open a pull request',
  pr_merge: 'merged',
  pr_view: 'viewed',
  pr_checks: 'checked CI status',
  commit: 'committed',
  push: 'pushed',
  pull: 'pulled',
  fetch: 'fetched',
  checkout: 'checked out a branch',
  branch: 'branched',
  merge: 'merged',
  rebase: 'rebased',
  status: 'checked status',
  diff: 'diffed',
  log: 'read the log',
  add: 'staged files',
  reset: 'reset',
  tag: 'tagged',
  clone: 'cloned',
  worktree: 'used a worktree',
  api: 'called the API',
  api_graphql: 'called the GraphQL API',
};
export const vcsActionLabel = (action: string): string =>
  VCS_ACTION_LABEL[action] ?? action.replace(/_/g, ' ');

export function subagentOrdinal(map: SessionMap): Map<string, number> {
  const out = new Map<string, number>();
  let k = 0;
  for (const a of map.agents) if (a.id !== 'main') out.set(a.id, ++k);
  return out;
}

export function agentLabel(
  map: SessionMap,
  agentId: string,
  ordinals: Map<string, number> = subagentOrdinal(map)
): string {
  if (agentId === 'main') return 'Main agent';
  const a = map.agents.find((x) => x.id === agentId);
  const k = ordinals.get(agentId);
  const type = a?.subagentType || 'subagent';
  return k ? `Sub ${k}: ${type}` : type;
}

export function buildTree(map: SessionMap): SessionTree {
  const root: TreeNode = mkNode('root', 'session', 'root', null);
  const byId = new Map<string, TreeNode>([['root', root]]);
  const add = (
    parent: TreeNode,
    id: string,
    name: string,
    type: NodeType,
    extra: Partial<TreeNode> = {}
  ): TreeNode => {
    const existing = byId.get(id);
    if (existing) return existing;
    const n = { ...mkNode(id, name, type, parent), ...extra };
    parent.children.push(n);
    byId.set(id, n);
    return n;
  };
  const repoName = new Map(map.repos.map((r) => [r.id, r.name]));
  for (const r of map.repos) add(root, `repo:${r.id}`, r.name, 'repo');
  for (const f of map.files) {
    let p = byId.get(`repo:${f.repo}`);
    if (!p) p = add(root, `repo:${f.repo}`, f.repo, 'repo');
    const parts = f.path.split('/').filter(Boolean);
    let acc = f.repo;
    for (let i = 0; i < parts.length - 1; i++) {
      acc += `/${parts[i]}`;
      p = add(p, `dir:${acc}`, parts[i], 'dir');
    }
    add(p, `file:${f.id}`, parts[parts.length - 1] ?? f.path, 'file', {
      path: f.path,
      repoName: repoName.get(f.repo) ?? f.repo,
    });
  }
  const group = (id: string, name: string) => add(root, id, name, 'group');
  for (const e of map.events) {
    for (const t of e.targets ?? []) {
      if (t.scope && t.label) {
        add(
          group('g:outside', 'outside repos'),
          `place:${t.label}`,
          t.label,
          'place'
        );
      }
    }
    if (e.kind === 'mcp' && e.server) {
      const s = add(
        group('g:mcp', 'MCP'),
        `srv:${e.server}`,
        e.server,
        'server'
      );
      add(s, `mcp:${e.server}/${e.tool ?? ''}`, e.tool ?? 'tool', 'mcptool');
    }
    if (e.kind === 'skill' && e.skill) {
      add(group('g:skill', 'skills'), `skill:${e.skill}`, e.skill, 'skill');
    }
    if (e.kind === 'web' && e.host) {
      add(group('g:web', 'web'), `web:${e.host}`, e.host, 'web');
    }
    if (e.err) {
      const src = errSource(e);
      const cls = errClassOf(e);
      const sn = add(
        group('g:errors', 'errors'),
        `errsrc:${src}`,
        ERROR_SOURCE_LABEL[src],
        'errsrc',
        { src }
      );
      add(sn, `errc:${src}/${cls}`, errClassLabel(cls), 'errclass', { src });
    }
  }
  map.events.forEach((e, i) => {
    if ((e.kind === 'llm' || e.kind === 'model_switch') && (e.model || e.to)) {
      const m = (e.kind === 'model_switch' ? e.to : e.model) as string;
      add(group('g:models', 'models'), `model:${m}`, m, 'model');
    }
    const out = isOutputEvent(e) ? outputNodeOf(e, i) : null;
    if (out) {
      const b = add(
        group('g:outputs', 'outputs'),
        `outg:${out.bucket}`,
        OUTPUT_BUCKET_LABEL[out.bucket],
        'outgroup'
      );
      add(b, out.id, out.name, 'output', {
        kind: e.vcs?.action === 'pr_create' ? 'pr_create' : e.kind,
      });
    }
  });
  const ordinals = subagentOrdinal(map);
  for (const a of map.agents) {
    if (a.id === 'main') continue;
    add(
      group('g:agents', 'subagents'),
      `agent:${a.id}`,
      agentLabel(map, a.id, ordinals),
      'agent'
    );
  }

  // Collapse single-child dir chains (a/b/c with nothing beside b) into one
  // node so deep monorepo paths do not eat the radius. Folded ids keep
  // resolving to the surviving node.
  const aliasOf = new Map<TreeNode, string[]>();
  const compress = (n: TreeNode) => {
    for (const c of n.children) compress(c);
    if (
      n.type === 'dir' &&
      n.children.length === 1 &&
      n.children[0].type === 'dir'
    ) {
      const c = n.children[0];
      n.name = `${n.name}/${c.name}`;
      n.children = c.children;
      for (const k of n.children) k.parent = n;
      const folded = [c.id, ...(aliasOf.get(c) ?? [])];
      for (const id of folded) byId.set(id, n);
      aliasOf.set(n, [...(aliasOf.get(n) ?? []), ...folded]);
    }
  };
  compress(root);

  // repos, then groups; the errors group always last
  root.children.sort(
    (a, b) =>
      (GROUP_ORDER[a.type] ?? 2) - (GROUP_ORDER[b.type] ?? 2) ||
      Number(a.id === 'g:errors') - Number(b.id === 'g:errors')
  );

  let maxDepth = 0;
  const leaves: TreeNode[] = [];
  const walk = (n: TreeNode, d: number) => {
    n.depth = d;
    maxDepth = Math.max(maxDepth, d);
    if (!n.children.length && n !== root) leaves.push(n);
    for (const c of n.children) walk(c, d + 1);
  };
  walk(root, 0);
  return { root, byId, leaves, maxDepth };
}

function mkNode(
  id: string,
  name: string,
  type: NodeType,
  parent: TreeNode | null
): TreeNode {
  return {
    id,
    name,
    type,
    children: [],
    parent,
    depth: 0,
    a: 0,
    r: 0,
    x: 0,
    y: 0,
  };
}

/** Internal nodes start on this fraction of R so group labels do not pile up
 * at the centre, and end at INNER_MAX; leaves sit on the outer ring R. */
export const INNER_MIN = 0.32;
export const INNER_MAX = 0.78;

/**
 * Radial cluster layout. Every leaf sits on the outer ring (where there is
 * circumference for labels); internal nodes spread from 0.32R to 0.78R by
 * depth. A small angular gap separates top-level branches. Mutates the nodes
 * and returns them in pre-order.
 */
export function layoutTree(tree: SessionTree, R: number): TreeNode[] {
  const { root } = tree;
  const top = root.children;
  const leafCount = (n: TreeNode): number =>
    n.children.length ? n.children.reduce((s, c) => s + leafCount(c), 0) : 1;
  let slots = 0;
  for (const c of top) slots += leafCount(c) + 1.2;
  const unit = (2 * Math.PI) / Math.max(slots, 1);
  let a = -Math.PI / 2 + unit * 0.6;
  const place = (n: TreeNode) => {
    if (!n.children.length) {
      n.a = a;
      a += unit;
      return;
    }
    for (const c of n.children) place(c);
    n.a = (n.children[0].a + n.children[n.children.length - 1].a) / 2;
  };
  for (const c of top) {
    place(c);
    a += unit * 1.2;
  }
  let maxInternal = 1;
  const md = (n: TreeNode) => {
    if (n.children.length && n !== root)
      maxInternal = Math.max(maxInternal, n.depth);
    for (const c of n.children) md(c);
  };
  md(root);
  const step = (R * (INNER_MAX - INNER_MIN)) / Math.max(maxInternal - 1, 1);
  const all: TreeNode[] = [];
  const walk = (n: TreeNode) => {
    n.r =
      n === root
        ? 0
        : n.children.length
          ? R * INNER_MIN + (n.depth - 1) * step
          : R;
    n.x = Math.cos(n.a) * n.r;
    n.y = Math.sin(n.a) * n.r;
    all.push(n);
    for (const c of n.children) walk(c);
  };
  walk(root);
  return all;
}

/** Canonical map nodes each event touches (files, places, MCP tool, skill,
 * web host, spawned subagent, and the error class when the event failed). */
export function nodeRefsFor(
  e: SessionMapEvent,
  tree: SessionTree,
  i = -1
): NodeRef[] {
  const out: NodeRef[] = [];
  const get = (id: string) => tree.byId.get(id)?.id ?? null;
  for (const t of e.targets ?? []) {
    const id = t.scope
      ? t.label
        ? get(`place:${t.label}`)
        : null
      : t.file != null
        ? get(`file:${t.file}`)
        : null;
    if (id) out.push({ id, lvl: TOUCH_LEVEL[t.touch] });
  }
  let extra: string | null = null;
  if (e.kind === 'mcp') extra = get(`mcp:${e.server}/${e.tool ?? ''}`);
  else if (e.kind === 'skill') extra = get(`skill:${e.skill}`);
  else if (e.kind === 'web') extra = get(`web:${e.host}`);
  else if (e.kind === 'subagent' && e.child) extra = get(`agent:${e.child}`);
  if (extra) out.push({ id: extra });
  const modelId =
    e.kind === 'llm' && e.model
      ? get(`model:${e.model}`)
      : e.kind === 'model_switch' && e.to
        ? get(`model:${e.to}`)
        : null;
  if (modelId) out.push({ id: modelId, passive: e.kind === 'llm' });
  if (isOutputEvent(e)) {
    const o = outputNodeOf(e, i);
    const id = o ? get(o.id) : null;
    if (id) out.push({ id });
  }
  if (e.err) {
    for (const r of out) r.failed = true;
    const id = get(`errc:${errSource(e)}/${errClassOf(e)}`);
    if (id) out.push({ id, errNode: true });
  }
  return out;
}

export function eventNodeRefs(map: SessionMap, tree: SessionTree): NodeRef[][] {
  return map.events.map((e, i) => nodeRefsFor(e, tree, i));
}

export interface NodeState {
  /** highest touch level so far (2 when the node has no levels, e.g. MCP) */
  lvl: TouchLevel;
  cnt: number;
  first: number;
  last: number;
  /** events that failed at this node */
  err: number;
}

/** Per-node touch state for events [0..p]. */
export function stateAt(refs: NodeRef[][], p: number): Map<string, NodeState> {
  const st = new Map<string, NodeState>();
  for (let i = 0; i <= p && i < refs.length; i++) {
    for (const x of refs[i]) {
      let s = st.get(x.id);
      if (!s) {
        s = { lvl: 1, cnt: 0, first: i, last: i, err: 0 };
        st.set(x.id, s);
      }
      s.cnt++;
      s.last = i;
      if (x.failed) s.err++;
      const lvl = x.lvl ?? 2;
      if (lvl > s.lvl) s.lvl = lvl;
    }
  }
  return st;
}

/** The ~`limit` most recently touched distinct nodes at the playhead. These
 * get full colour and labels; everything touched earlier renders gray. */
export function activeSet(
  refs: NodeRef[][],
  p: number,
  limit = 10
): Set<string> {
  const active = new Set<string>();
  for (
    let i = Math.min(p, refs.length - 1);
    i >= 0 && active.size < limit;
    i--
  ) {
    for (const x of refs[i]) {
      if (x.passive) continue;
      active.add(x.id);
      if (active.size >= limit) break;
    }
  }
  return active;
}

/** The latest event at or before `p` that touches a node (passive refs,
 * e.g. an LLM request's model, don't count): its index and node ids. The
 * map lights this one step, so across the LLM requests and prompts between
 * two touches it stays on the last real one instead of going blank. */
export function lastTouch(
  refs: NodeRef[][],
  p: number
): { i: number; ids: string[] } | null {
  for (let i = Math.min(p, refs.length - 1); i >= 0; i--) {
    const ids = refs[i].filter((x) => !x.passive).map((x) => x.id);
    if (ids.length) return { i, ids };
  }
  return null;
}

/** Nodes (and their ancestors) touched up to the playhead. */
export function litSet(
  tree: SessionTree,
  st: Map<string, NodeState>
): Set<string> {
  const lit = new Set<string>();
  for (const id of st.keys()) {
    let q: TreeNode | null | undefined = tree.byId.get(id);
    while (q && !lit.has(q.id)) {
      lit.add(q.id);
      q = q.parent;
    }
  }
  return lit;
}

export function ancestorsOf(
  tree: SessionTree,
  ids: Iterable<string>
): Set<string> {
  const out = new Set<string>();
  for (const id of ids) {
    let q: TreeNode | null | undefined = tree.byId.get(id);
    while (q) {
      out.add(q.id);
      q = q.parent;
    }
  }
  return out;
}

/** Up to `limit` recent events that touched a node, oldest first, for the
 * trail. Each item is the event index and its first node id. */
export function recentTargets(
  refs: NodeRef[][],
  p: number,
  limit = 9
): { i: number; id: string }[] {
  const out: { i: number; id: string }[] = [];
  for (
    let i = Math.min(p, refs.length - 1);
    i >= 0 && out.length < limit;
    i--
  ) {
    const x = refs[i].find((r) => !r.errNode && !r.passive);
    if (x) out.push({ i, id: x.id });
  }
  return out.reverse();
}

function isUnder(tree: SessionTree, id: string, ancestor: TreeNode): boolean {
  let q: TreeNode | null | undefined = tree.byId.get(id);
  while (q) {
    if (q === ancestor) return true;
    q = q.parent;
  }
  return false;
}

/** Next event after `p` (wrapping) that touches `nodeId` or anything under it. */
export function nextTouchOf(
  tree: SessionTree,
  refs: NodeRef[][],
  p: number,
  nodeId: string
): number | null {
  const node = tree.byId.get(nodeId);
  if (!node) return null;
  const hit = (i: number) => refs[i].some((x) => isUnder(tree, x.id, node));
  for (let i = p + 1; i < refs.length; i++) if (hit(i)) return i;
  for (let i = 0; i <= p && i < refs.length; i++) if (hit(i)) return i;
  return null;
}

export type JumpKind =
  | 'edit'
  | 'err'
  | 'user'
  | 'subagent'
  | 'delay'
  | 'retry'
  | 'compaction'
  | 'out';

const JUMP_TEST: Record<JumpKind, (e: SessionMapEvent) => boolean> = {
  edit: (e) => e.kind === 'edit',
  err: (e) => e.err,
  user: (e) => e.kind === 'user',
  subagent: (e) => e.kind === 'subagent',
  delay: (e) => e.kind === 'delay',
  retry: (e) => e.retryOf != null,
  compaction: (e) => e.kind === 'compaction',
  out: isOutputEvent,
};

/** Next event after `p` (wrapping) matching the jump kind, or null. */
export function findNext(
  events: SessionMapEvent[],
  p: number,
  kind: JumpKind
): number | null {
  return findNextWhere(events, p, JUMP_TEST[kind]);
}

/** Next event after `p` (wrapping) passing `test`, or null. */
export function findNextWhere(
  events: SessionMapEvent[],
  p: number,
  test: (e: SessionMapEvent) => boolean
): number | null {
  for (let i = p + 1; i < events.length; i++) if (test(events[i])) return i;
  for (let i = 0; i <= p && i < events.length; i++)
    if (test(events[i])) return i;
  return null;
}

/** A meaningful first frame: just past the first error (else the first
 * edit), else 40% in. */
export function initialPlayhead(events: SessionMapEvent[]): number {
  if (!events.length) return 0;
  const firstErr = events.findIndex((e) => e.err);
  const firstEdit = events.findIndex((e) => e.kind === 'edit');
  const anchor = firstErr > 0 ? firstErr : firstEdit;
  return anchor > 0
    ? Math.min(events.length - 1, anchor + 12)
    : Math.floor(events.length * 0.4);
}

// --- counters / costs ------------------------------------------------------

export const llmTokens = (e: SessionMapEvent): number =>
  (e.in ?? 0) + (e.cw ?? 0) + (e.cr ?? 0) + (e.out ?? 0);

/** Prefix sums of advisory LLM cost: cum[i] = cost of events [0..i]. */
export function cumulativeCost(events: SessionMapEvent[]): Float64Array {
  const cum = new Float64Array(events.length);
  let s = 0;
  events.forEach((e, i) => {
    s += e.cost ?? 0;
    cum[i] = s;
  });
  return cum;
}

export interface PlayheadCounters {
  calls: number;
  requests: number;
  tokens: number;
  cost: number;
  errors: number;
  mcp: number;
  skills: number;
  subagents: number;
  filesRead: number;
  filesEdited: number;
}

export function countersAt(
  events: SessionMapEvent[],
  p: number
): PlayheadCounters {
  const c: PlayheadCounters = {
    calls: 0,
    requests: 0,
    tokens: 0,
    cost: 0,
    errors: 0,
    mcp: 0,
    skills: 0,
    subagents: 0,
    filesRead: 0,
    filesEdited: 0,
  };
  const reads = new Set<number>();
  const edits = new Set<number>();
  for (let i = 0; i <= p && i < events.length; i++) {
    const e = events[i];
    if (e.err) c.errors++;
    if (e.kind === 'llm') {
      c.requests++;
      c.tokens += llmTokens(e);
      c.cost += e.cost ?? 0;
      continue;
    }
    if (NON_CALL.has(e.kind)) continue;
    c.calls++;
    if (e.kind === 'mcp') c.mcp++;
    if (e.kind === 'skill') c.skills++;
    if (e.kind === 'subagent') c.subagents++;
    for (const t of e.targets ?? []) {
      if (t.scope || t.file == null) continue;
      if (t.touch === 'edit') edits.add(t.file);
      else if (t.touch === 'read') reads.add(t.file);
    }
  }
  c.filesRead = reads.size;
  c.filesEdited = edits.size;
  return c;
}

// --- review (whole session) -------------------------------------------------

export type VerificationState =
  'never' | 'after_final_edit' | 'edits_after_verify';

export interface ReviewMetrics {
  errors: number;
  /** edits to a file that was never read first; a first Write that creates
   * the file is not blind */
  blindEdits: number;
  /** files edited 3+ times */
  churnFiles: number;
  /** % of reads that re-read a file unchanged since its last read */
  reReadPct: number;
  verification: VerificationState;
}

const CREATE_TOOLS = new Set(['Write', 'write', 'create_file', 'CreateFile']);

export function reviewMetrics(map: SessionMap): ReviewMetrics {
  const readSoFar = new Set<number>();
  const lastEditAt = new Map<number, number>();
  const lastReadAt = new Map<number, number>();
  let blind = 0;
  let reReads = 0;
  let readsN = 0;
  let errors = 0;
  let lastVerify = -1;
  let lastEdit = -1;
  map.events.forEach((e, i) => {
    if (e.err) errors++;
    if (e.kind === 'verify') lastVerify = i;
    if (e.kind === 'edit') lastEdit = i;
    for (const t of e.targets ?? []) {
      if (t.scope || t.file == null) continue;
      const f = t.file;
      if (t.touch === 'read') {
        readsN++;
        const lr = lastReadAt.get(f);
        const le = lastEditAt.get(f);
        if (lr != null && !(le != null && le > lr)) reReads++;
        lastReadAt.set(f, i);
        readSoFar.add(f);
      } else if (t.touch === 'edit') {
        const creates =
          CREATE_TOOLS.has(e.tool ?? '') &&
          !lastEditAt.has(f) &&
          !readSoFar.has(f);
        if (!readSoFar.has(f) && !creates) blind++;
        readSoFar.add(f);
        lastEditAt.set(f, i);
      }
    }
  });
  return {
    errors,
    blindEdits: blind,
    churnFiles: map.files.filter((f) => f.edits >= 3).length,
    reReadPct: readsN ? Math.round((100 * reReads) / readsN) : 0,
    verification:
      lastVerify < 0
        ? 'never'
        : lastVerify > lastEdit
          ? 'after_final_edit'
          : 'edits_after_verify',
  };
}

export interface FileCoverage {
  edited: number;
  read: number;
  seen: number;
}

/**
 * Derived from `map.events`, not `map.files[].reads/edits/hits`: those are
 * a document-level aggregate across every agent, so once an agent lens
 * (`deriveLensDoc`) scopes `events` to one lane while leaving the shared
 * `files` array untouched, reading the file records would leak the OTHER
 * agents' touches into this lens's coverage.
 * `seen` counts a file touched only as a dependency (`hit`), never read or
 * edited; a file never touched at all is not counted here at all.
 */
export function fileCoverage(map: SessionMap): FileCoverage {
  const touch = new Map<number, { read: number; edit: number; hit: number }>();
  for (const e of map.events) {
    for (const t of e.targets ?? []) {
      if (t.scope || t.file == null) continue;
      const c = touch.get(t.file) ?? { read: 0, edit: 0, hit: 0 };
      c[t.touch]++;
      touch.set(t.file, c);
    }
  }
  let edited = 0;
  let read = 0;
  for (const c of touch.values()) {
    if (c.edit > 0) edited++;
    else if (c.read > 0) read++;
  }
  return { edited, read, seen: touch.size - edited - read };
}

export function touchesOfFile(
  map: SessionMap,
  fileId: number
): { read: number; edit: number; hit: number } {
  const c = { read: 0, edit: 0, hit: 0 };
  for (const e of map.events) {
    for (const t of e.targets ?? [])
      if (!t.scope && t.file === fileId) c[t.touch]++;
  }
  return c;
}

// --- derived documents ------------------------------------------------------

/**
 * `startedAt` moved forward by `offsetMs`, as ISO. The schema only requires a
 * string, so an unparseable stamp (an imported map can carry anything) is
 * returned as-is: `new Date(NaN).toISOString()` throws a RangeError mid-render.
 */
export function shiftStartedAt(startedAt: string, offsetMs: number): string {
  const ms = Date.parse(startedAt);
  return Number.isNaN(ms) ? startedAt : new Date(ms + offsetMs).toISOString();
}

/**
 * The event's sequence number in the document it was derived from, which is
 * the one every reference taken outside the viewer is in terms of.
 *
 * `deriveScopeDoc` and `deriveLensDoc` keep a subset of the events and
 * renumber `seq` so that `seq === index` still holds in the copy. That is
 * what keeps every other index in the copy (`ref`, `retryOf`, `prs[].seq`)
 * consistent, but it also means a reference taken against the full document
 * -- a review comment's causal path, say -- no longer resolves against
 * `seq`: it finds whatever event now sits at that position, or none.
 * Those derivations stamp the original number on `srcSeq` instead.
 *
 * `srcSeq` rides the event schema's `passthrough` rather than being a field
 * of it, because it is the viewer's own and never on the wire; a document
 * that happens to carry one is not trusted to have put a number there. An
 * underived event has none, and its own `seq` is already the answer.
 */
export function srcSeqOf(e: SessionMapEvent): number | null | undefined {
  const src = e.srcSeq;
  return typeof src === 'number' ? src : e.seq;
}

// --- agent lens -------------------------------------

/**
 * Recompute `totals` from an event subset. Used by `deriveLensDoc` so a
 * lens's totals reflect only its own events, never the full document's
 * cross-agent aggregate (the same hazard `fileCoverage` guards against).
 */
export function recomputeTotals(
  events: SessionMapEvent[]
): SessionMap['totals'] {
  const llm = events.filter((e) => e.kind === 'llm');
  const errorsBySource: Record<string, number> = {};
  const errorsByClass: Record<string, number> = {};
  for (const e of events) {
    if (!e.err) continue;
    const src = errSource(e);
    errorsBySource[src] = (errorsBySource[src] ?? 0) + 1;
    const cls = errClassOf(e);
    errorsByClass[cls] = (errorsByClass[cls] ?? 0) + 1;
  }
  const files = new Set<number>();
  const created = new Set<number>();
  const deleted = new Set<number>();
  for (const e of events) {
    for (const t of e.targets ?? []) {
      if (t.scope || t.file == null) continue;
      files.add(t.file);
      if (t.op === 'create') created.add(t.file);
      if (t.op === 'delete') deleted.add(t.file);
    }
  }
  const latencies = events
    .filter((e) => e.kind === 'llm' && e.latency != null)
    .map((e) => e.latency ?? 0)
    .sort((a, b) => a - b);
  const delays = events.filter((e) => e.kind === 'delay');
  const contexts = llm.map((e) => ctxOf(e));
  return {
    calls: events.filter((e) => !NON_CALL.has(e.kind)).length,
    requests: llm.length,
    userTurns: events.filter((e) => e.kind === 'user').length,
    // a lens has no further nesting (contract), so it never has its own
    // subagents even when the full session did
    subagents: 0,
    errors: events.filter((e) => e.err).length,
    tokens: llm.reduce((s, e) => s + ctxOf(e) + (e.out ?? 0), 0),
    costUsd:
      Math.round(llm.reduce((s, e) => s + (e.cost ?? 0), 0) * 10000) / 10000,
    filesTouched: files.size,
    errorsBySource,
    errorsByClass,
    retries: events.filter((e) => e.retryOf != null).length,
    delays: delays.length,
    delayMs: delays.reduce((s, e) => s + (e.ms ?? 0), 0),
    maxContext: Math.max(0, ...contexts),
    compactions: events.filter((e) => e.kind === 'compaction').length,
    llmLatencyP50: latencies.length
      ? latencies[Math.floor(latencies.length / 2)]
      : 0,
    llmLatencyP95: latencies.length
      ? latencies[
          Math.min(latencies.length - 1, Math.floor(latencies.length * 0.95))
        ]
      : 0,
    outputTokens: llm.reduce((s, e) => s + (e.out ?? 0), 0),
    prsCreated: events.filter((e) => e.vcs?.action === 'pr_create').length,
    filesCreated: created.size,
    filesDeleted: deleted.size,
    ...outputTotals(events),
  };
}

export interface OutputTotals {
  commits: number;
  prsMerged: number;
  prsClosed: number;
  prComments: number;
  ticketsCreated: number;
  ticketsUpdated: number;
  artifacts: number;
  skillsCreated: number;
  skillsUpdated: number;
  modelSwitches: number;
}

/** Output counts over an event subset (a task, a model, a lens). */
export function outputTotals(events: SessionMapEvent[]): OutputTotals {
  const n = (test: (e: SessionMapEvent) => boolean) =>
    events.filter(test).length;
  return {
    commits: n((e) => e.kind === 'commit'),
    prsMerged: n((e) => e.kind === 'pr_merge'),
    prsClosed: n((e) => e.kind === 'pr_close'),
    prComments: n((e) => e.kind === 'pr_comment'),
    ticketsCreated: n((e) => e.kind === 'ticket' && e.action === 'create'),
    ticketsUpdated: n((e) => e.kind === 'ticket' && e.action !== 'create'),
    artifacts: n((e) => e.kind === 'artifact'),
    skillsCreated: n((e) => e.kind === 'skill_write' && e.action === 'create'),
    skillsUpdated: n((e) => e.kind === 'skill_write' && e.action !== 'create'),
    modelSwitches: n((e) => e.kind === 'model_switch'),
  };
}

/**
 * Agent lens (viewer-only, no change to the map): scope the whole explorer to one
 * agent's own events, as if it were the primary session — same map/timeline
 * code, a filtered + reindexed event list. `retryOf`/`ref` are remapped to
 * the new local sequence numbers (both always resolve within one agent's
 * own lane by construction). A subagent has no further nesting in this data
 * model, so a lens's own `agents` list is always just itself.
 *
 * `'main'` (or an agent id with no events of its own) returns `full`
 * unchanged, so callers can pass the lens id straight through without a
 * branch.
 */
export function deriveLensDoc(full: SessionMap, agentId: string): SessionMap {
  if (agentId === 'main') return full;
  const src: { e: SessionMapEvent; oldIndex: number }[] = [];
  full.events.forEach((e, oldIndex) => {
    if (e.agent === agentId) src.push({ e, oldIndex });
  });
  if (!src.length) return full;

  // Keyed by each event's position in `full.events`, not its wire `seq`
  // (schema: optional - a map may omit it). The contract guarantees
  // seq === index, and `retryOf`/`ref` are seq references, so position is
  // the same identifier without depending on `seq` actually being present.
  const oldToNew = new Map<number, number>();
  src.forEach(({ oldIndex }, i) => oldToNew.set(oldIndex, i));
  const t0 = src[0].e.t;
  const events = src.map(({ e, oldIndex }, i) => {
    const ev: SessionMapEvent = {
      ...e,
      seq: i,
      // Set once, by the first derivation: a lens over an already-scoped
      // document must still name the full document's sequence number.
      srcSeq: typeof e.srcSeq === 'number' ? e.srcSeq : (e.seq ?? oldIndex),
      t: e.t - t0,
      agent: 'main',
    };
    if (ev.retryOf != null) ev.retryOf = oldToNew.get(ev.retryOf);
    if (ev.ref != null) ev.ref = oldToNew.get(ev.ref);
    return ev;
  });
  const lastEvent = events[events.length - 1];
  const durationMs = lastEvent ? lastEvent.t : 0;
  const meta = full.agents.find((a) => a.id === agentId);
  const prs = full.prs.filter((pr) =>
    src.some(({ oldIndex }) => oldIndex === pr.seq)
  );

  // Reduced approximation of the map's outcome heuristic, used only when
  // deriving an agent-lens view; it does not attempt `verified`,
  // `blocked` or `incomplete`, which need signal only the producer has.
  const tail = events.slice(-12);
  const outcome: SessionOutcome = events.some(
    (e) => e.vcs?.action === 'pr_create'
  )
    ? 'pr_created'
    : tail.some((e) => e.err && e.errClass === 'interrupted')
      ? 'interrupted'
      : tail.filter((e) => e.err).length >= 2
        ? 'unresolved_errors'
        : 'completed';

  return {
    ...full,
    session: {
      ...full.session,
      id: `${full.session.id}:${agentId}`,
      startedAt: shiftStartedAt(full.session.startedAt, t0),
      durationMs,
      outcome,
    },
    totals: recomputeTotals(events),
    prs,
    agents: [
      {
        id: 'main',
        parent: null,
        kind: meta?.kind ?? 'subagent',
        subagentType: meta?.subagentType,
        launchSeq: meta?.launchSeq,
        start: 0,
        end: durationMs,
        calls: events.filter((e) => !NON_CALL.has(e.kind)).length,
        requests: events.filter((e) => e.kind === 'llm').length,
        tokens: events.reduce(
          (s, e) => (e.kind === 'llm' ? s + ctxOf(e) + (e.out ?? 0) : s),
          0
        ),
        cost: meta?.cost ?? 0,
        outcome,
      },
    ],
    events,
  };
}

export function leafCount(n: TreeNode): number {
  return n.children.length
    ? n.children.reduce((s, c) => s + leafCount(c), 0)
    : 1;
}

// --- formatting -------------------------------------------------------------

export const fmtTok = (n: number): string =>
  n >= 1e9
    ? `${(n / 1e9).toFixed(1)}B`
    : n >= 1e6
      ? `${(n / 1e6).toFixed(1)}M`
      : n >= 1e3
        ? `${(n / 1e3).toFixed(1)}K`
        : String(Math.round(n));

export const fmtUsd = (n: number): string =>
  `$${n >= 100 ? n.toFixed(0) : n.toFixed(2)}`;

export const fmtBytes = (n: number): string =>
  n >= 1e6
    ? `${(n / 1e6).toFixed(1)} MB`
    : n >= 1e3
      ? `${(n / 1e3).toFixed(1)} KB`
      : `${Math.round(n)} B`;

export const fmtDur = (ms: number): string => {
  const s = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const x = s % 60;
  if (h) return `${h}h ${String(m).padStart(2, '0')}m`;
  if (m) return `${m}m ${String(x).padStart(2, '0')}s`;
  return `${(Math.max(0, ms) / 1000).toFixed(1)}s`;
};

/** HH:MM:SS UTC at `ms` after `iso`. */
export const clockAt = (iso: string, ms: number): string => {
  const base = Date.parse(iso);
  if (Number.isNaN(base)) return '--:--:--';
  return new Date(base + ms).toISOString().slice(11, 19);
};

export const dayLabel = (iso: string): string => {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    weekday: 'short',
    timeZone: 'UTC',
  });
};

// --- event descriptions -----------------------------------------------------

export interface EventDescription {
  title: string;
  detail: string;
}

export function describeEvent(
  map: SessionMap,
  e: SessionMapEvent,
  ordinals?: Map<string, number>
): EventDescription {
  const f = e.targets?.find((t) => !t.scope && t.file != null);
  const fo = f ? map.files.find((x) => x.id === f.file) : undefined;
  const repo = fo
    ? (map.repos.find((r) => r.id === fo.repo)?.name ?? fo.repo)
    : '';
  const place = e.targets?.find((t) => t.scope);
  switch (e.kind) {
    case 'llm':
      return {
        title: `${e.model || 'model'} · ${fmtUsd(e.cost ?? 0)}`,
        detail: `${fmtTok(ctxOf(e))} context in · ${fmtTok(e.out ?? 0)} out${e.latency ? ` · ${fmtDur(e.latency)} latency` : ''}`,
      };
    case 'error':
      return {
        title: hookErrorTitle(e) ?? errClassLabel(errClassOf(e)),
        detail: `${isErrorSource(e.source) ? ERROR_SOURCE_LABEL[e.source] : (e.source ?? 'error')}${e.hook ? ` · ${e.hook}` : ''}${e.retry ? ' · retried' : ''}`,
      };
    case 'delay':
      return {
        title: `${DELAY_CAUSE_LABEL[e.cause ?? ''] ?? 'delay'} · ${fmtDur(e.ms ?? 0)}`,
        detail:
          e.ref != null
            ? `caused by event #${e.ref}`
            : 'no activity on the main agent',
      };
    case 'user': {
      const extras = [
        e.images ? `${e.images} image${e.images > 1 ? 's' : ''}` : '',
        e.urls ? `${e.urls} URL${e.urls > 1 ? 's' : ''}` : '',
      ].filter(Boolean);
      return {
        title: e.task != null ? `User turn · task ${e.task}` : 'User turn',
        detail: `${e.chars ?? 0} chars${extras.length ? ` · ${extras.join(' · ')}` : ''} (text not stored)`,
      };
    }
    case 'model_switch':
      return {
        title: `Model switched to ${e.to ?? 'another model'}`,
        detail: e.from ? `from ${e.from}` : 'model switch',
      };
    case 'commit':
      return {
        title: `Commit ${e.sha ? e.sha.slice(0, 7) : ''}`.trim(),
        detail: `${e.repo ?? ''}${e.files != null ? ` · ${e.files} file${e.files === 1 ? '' : 's'}` : ''}${e.ins != null || e.del != null ? ` · +${e.ins ?? 0} −${e.del ?? 0}` : ''}`,
      };
    case 'pr_merge':
    case 'pr_close':
    case 'pr_comment': {
      const verb =
        e.kind === 'pr_merge'
          ? 'merged'
          : e.kind === 'pr_close'
            ? 'closed without merging'
            : 'commented on';
      return {
        title: `PR ${e.number != null ? `#${e.number} ` : ''}${verb}`,
        detail: e.repo ?? e.url ?? 'pull request',
      };
    }
    case 'ticket':
      return {
        title: `Ticket ${e.action === 'create' ? 'created' : 'updated'}${e.id ? ` · ${e.id}` : ''}`,
        detail: `${e.tracker ?? 'tracker'}${e.tool ? ` · ${e.tool}` : ''}`,
      };
    case 'artifact':
      return {
        title: `Artifact published${e.title ? ` · ${e.title}` : ''}`,
        detail: `${e.bytes != null ? fmtBytes(e.bytes) : 'size unknown'}${e.version != null ? ` · version ${e.version}` : ''}`,
      };
    case 'skill_write':
      return {
        title: `Skill ${e.action === 'create' ? 'created' : 'updated'}${e.name ? ` · ${e.name}` : ''}`,
        detail: 'skill file written',
      };
    case 'compaction':
      return {
        title: `Context compacted${e.trigger ? ` (${e.trigger})` : ''}`,
        detail: `${fmtTok(e.preTokens ?? 0)} → ${fmtTok(e.postTokens ?? 0)} tokens`,
      };
    case 'mcp':
      return {
        title: `${e.server ?? 'MCP'} · ${e.tool ?? ''}`,
        detail: 'MCP call',
      };
    case 'skill':
      return { title: `/${e.skill ?? 'skill'}`, detail: 'Skill invoked' };
    case 'subagent':
      return {
        title: `Subagent · ${e.subagentType || 'general'}`,
        detail: e.child
          ? `spawned ${agentLabel(map, e.child, ordinals)}${e.model ? ` · ${e.model}` : ''}`
          : 'spawned a subagent',
      };
    case 'web':
      return { title: e.host ?? 'web', detail: e.tool ?? 'Web' };
    default: {
      const tool = e.tool ?? e.kind;
      const verb = e.verb ? ` · ${e.verb}` : '';
      if (e.vcs) {
        const prTail =
          e.vcs.action === 'pr_create' && e.vcs.number != null
            ? ` · ${e.vcs.provider} #${e.vcs.number}`
            : '';
        return {
          title: `${vcsActionLabel(e.vcs.action)}${e.vcs.repo ? ` · ${e.vcs.repo}` : ''}${e.vcs.number != null ? ` #${e.vcs.number}` : ''}`,
          detail: `${tool}${verb}${prTail}`,
        };
      }
      if (fo) {
        const op = f?.op;
        return {
          title: `${op ? `${OP_GLYPH[op]} ` : ''}${fo.path.split('/').pop() || fo.path}`,
          detail: `${repo}/${fo.path}${op ? ` · ${OP_LABEL[op]}` : ''}`,
        };
      }
      return {
        title: place?.label ?? e.verb ?? tool,
        detail: `${tool}${verb}`,
      };
    }
  }
}

export function agentById(
  map: SessionMap,
  id: string
): SessionMapAgent | undefined {
  return map.agents.find((a) => a.id === id);
}
