import {
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { MagnifyingGlass } from '@phosphor-icons/react';
import type { SessionExplorerSession } from '@/core/schema';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';
import { clockAt, dayLabel, fmtDur, fmtUsd } from '@/core/sessionMapModel';
import { groupSessionsByDay } from '@/core/sessionLayout';

const AGENT_TOOL_LABEL: Record<string, string> = {
  'claude-code': 'Claude Code',
  codex: 'Codex',
  cursor: 'Cursor',
  kiro: 'Kiro',
  opencode: 'OpenCode',
  copilot: 'Copilot',
};

export const agentToolLabel = (tool: string): string =>
  AGENT_TOOL_LABEL[tool] ?? tool;

/** Subagent and helper runs are reached from inside their parent session;
 * the server already drops them from the list, this guards an older
 * server. A missing role is a main session (older CLIs). */
export const isMainSession = (s: SessionExplorerSession): boolean =>
  s.role !== 'subagent' && s.role !== 'helper';

export const sessionTitle = (s: SessionExplorerSession): string =>
  s.sessionTitle?.trim() ||
  `${agentToolLabel(s.agentTool)} session${s.repos[0] ? ` in ${s.repos[0]}` : ''}`;

interface Props {
  sessions: SessionExplorerSession[];
  selectedId: string | null;
  onSelect: (sessionId: string) => void;
  /** open the session in the sidebar: its tasks, errors and subagents */
  onOpen: (sessionId: string) => void;
  search: string;
  onSearch: (value: string) => void;
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
  /** sessions whose map the server reported as not_found */
  unavailable?: ReadonlySet<string>;
  /** sidebar header row (title + collapse button), above the search */
  header?: ReactNode;
  /** extra controls under the search (the window range select) */
  toolbar?: ReactNode;
  /** open a session at one event (a search match chip) */
  onJump?: (sessionId: string, seq: number) => void;
  /** a note about the search itself, under the box */
  searchNote?: ReactNode;
}

/**
 * The list's scroll offset, kept outside React on purpose.
 *
 * The sidebar renders inside `MapPlayerBody`, which `MapPlayer` keys on the
 * scope so playback re-initialises for the new session -- so picking a
 * session unmounts this list and mounts a fresh one, scrolled to the top,
 * taking the row that was just clicked off screen with it. Component state
 * cannot survive that; a module-level value can.
 *
 * Tagged with the filter it was taken under, so an offset from one list is
 * never restored onto a different one.
 */
let savedScroll: { top: number; tag: string } | null = null;

export function SessionList({
  sessions,
  selectedId,
  onSelect,
  onOpen,
  search,
  onSearch,
  hasMore,
  loadingMore,
  onLoadMore,
  unavailable,
  header,
  toolbar,
  onJump,
  searchNote,
}: Props) {
  const [tool, setTool] = useState<string>('all');
  const main = sessions.filter(isMainSession);
  const present = [...new Set(main.map((s) => s.agentTool))];
  // Keep a selected tool's chip even when a search leaves it no sessions, so
  // the filter stays visible and effective rather than silently
  // widening to every session.
  const tools =
    tool !== 'all' && !present.includes(tool) ? [...present, tool] : present;
  const visible =
    tool === 'all' ? main : main.filter((s) => s.agentTool === tool);
  const groups = groupSessionsByDay(visible);

  const scrollRef = useRef<HTMLDivElement>(null);
  const selectedRef = useRef<HTMLDivElement>(null);
  // What the saved offset belongs to. A search or a tool filter builds a
  // different list, and an offset from the old one would land anywhere.
  const tag = `${search}\u0000${tool}`;
  useLayoutEffect(() => {
    const box = scrollRef.current;
    if (!box) return;
    if (savedScroll && savedScroll.tag === tag) box.scrollTop = savedScroll.top;
    // The offset is restored first so the list looks untouched; this only
    // moves anything when it could not do its job -- a deep link, a
    // filter change, or a row that has shifted out of view since.
    const row = selectedRef.current;
    if (!row) return;
    const r = row.getBoundingClientRect();
    const b = box.getBoundingClientRect();
    if (r.top < b.top || r.bottom > b.bottom) {
      row.scrollIntoView({ block: 'nearest' });
    }
    // Mount only: this restores a position across the remount that picking a
    // session causes, and must not fight the user's own scrolling after.
  }, []);

  return (
    <nav
      aria-label="Sessions with maps"
      className="flex h-full min-h-0 flex-col gap-2 rounded-xl border border-ds-border-subtlest bg-ds-surface-white p-3"
    >
      {header}
      <div className="relative">
        <MagnifyingGlass
          size={14}
          className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ds-text-subtle"
        />
        <Input
          value={search}
          onChange={(e) => onSearch(e.target.value)}
          placeholder="Search titles, files, commands, MCP…"
          aria-label="Search sessions"
          className="h-8 pl-8"
        />
      </div>
      {searchNote}
      {toolbar}
      {tools.length > 1 ? (
        <ToolFilter tools={tools} tool={tool} onTool={setTool} />
      ) : null}
      <div className="ds-text-body-xs-medium text-ds-text-subtle">
        {visible.length} session{visible.length === 1 ? '' : 's'} with maps
        {hasMore ? ' loaded' : ''} · main sessions only
      </div>
      <div
        ref={scrollRef}
        onScroll={(e) => {
          savedScroll = { top: e.currentTarget.scrollTop, tag };
        }}
        className="-mx-1 flex min-h-0 flex-1 flex-col overflow-y-auto px-1"
      >
        {groups.length === 0 ? (
          <div className="py-6 text-center ds-text-body-xs-medium text-ds-text-subtle">
            No sessions match this filter
          </div>
        ) : null}
        {groups.map((g) => (
          <div key={g.day} className="flex flex-col gap-1 pb-2">
            <div className="sticky top-0 z-10 bg-ds-surface-white py-1 ds-text-body-xs-semibold text-ds-text-subtle">
              {dayLabel(`${g.day}T00:00:00Z`)}
            </div>
            {g.items.map((s) => (
              <SessionRow
                key={s.sessionId}
                s={s}
                selected={s.sessionId === selectedId}
                unavailable={!!unavailable?.has(s.sessionId)}
                rowRef={s.sessionId === selectedId ? selectedRef : undefined}
                onSelect={() => onSelect(s.sessionId)}
                onOpen={() => onOpen(s.sessionId)}
                onJump={onJump ? (seq) => onJump(s.sessionId, seq) : undefined}
              />
            ))}
          </div>
        ))}
        {hasMore ? (
          <Button
            variant="outline"
            size="sm"
            onClick={onLoadMore}
            disabled={loadingMore}
            className="mt-1"
          >
            {loadingMore ? 'Loading…' : 'Load more sessions'}
          </Button>
        ) : null}
      </div>
    </nav>
  );
}

function ToolFilter({
  tools,
  tool,
  onTool,
}: {
  tools: string[];
  tool: string;
  onTool: (tool: string) => void;
}) {
  return (
    <div
      role="group"
      aria-label="Filter sessions by coding agent"
      className="flex flex-wrap gap-1"
    >
      {['all', ...tools].map((t) => (
        <button
          key={t}
          type="button"
          aria-pressed={tool === t}
          onClick={() => onTool(t)}
          className={cn(
            'rounded-full border px-2 py-0.5 ds-text-body-xs-medium transition-colors',
            tool === t
              ? 'border-transparent bg-ds-brand-lightest text-ds-text-darker'
              : 'border-ds-border-subtlest text-ds-text-subtle hover:border-ds-border-default'
          )}
        >
          {t === 'all' ? 'All' : agentToolLabel(t)}
        </button>
      ))}
    </div>
  );
}

/**
 * A row picks a session; "Open" is what descends into it. Picking does not
 * replace the list with one session's tasks, so two sessions can be compared
 * without going back out each time: the map and Details follow the selection
 * while the list stays put, and Open is the deliberate step.
 *
 * A div, not a button: Open sits inside the card, and a button inside a
 * button is invalid and unreachable by keyboard.
 */
function SessionRow({
  s,
  selected,
  unavailable,
  onSelect,
  onOpen,
  onJump,
  rowRef,
}: {
  s: SessionExplorerSession;
  selected: boolean;
  unavailable: boolean;
  onSelect: () => void;
  onOpen: () => void;
  onJump?: (seq: number) => void;
  rowRef?: React.Ref<HTMLDivElement>;
}) {
  return (
    <div
      ref={rowRef}
      className={cn(
        'group flex flex-col rounded-lg border transition-colors',
        selected
          ? 'border-ds-brand-subtler bg-ds-brand-lightest'
          : 'border-transparent hover:bg-ds-gray-subtlest'
      )}
    >
      <button
        type="button"
        aria-current={selected ? 'true' : undefined}
        onClick={onSelect}
        onDoubleClick={onOpen}
        className="flex cursor-pointer flex-col gap-1 rounded-lg p-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ds-brand-subtle focus-visible:ring-offset-1 focus-visible:ring-offset-ds-surface-white"
      >
        <span className="line-clamp-2 ds-text-body-xs-medium text-ds-text-darker">
          {sessionTitle(s)}
        </span>
        <span className="ds-text-body-xs-medium text-ds-text-subtle">
          {clockAt(s.startedAt, 0).slice(0, 5)} UTC · {fmtDur(s.durationMs)} ·{' '}
          {s.calls} calls · {fmtUsd(s.costUsd)}
        </span>
        <SessionRowBadges s={s} unavailable={unavailable} />
      </button>
      {s.search && onJump ? (
        <SearchMatches key={s.search.seqs.join(',')} search={s.search} selected={selected} onJump={onJump} />
      ) : null}
      {/* Always there for the selected row -- you just clicked it, so that
       * is where the next step belongs. Quiet on the rest until pointed at
       * or tabbed to, so a long list does not grow a button per row. */}
      <div
        className={cn(
          'px-2 pb-2 transition-opacity',
          selected
            ? 'opacity-100'
            : 'opacity-0 focus-within:opacity-100 group-hover:opacity-100'
        )}
      >
        <Button
          variant="secondary"
          size="sm"
          onClick={onOpen}
          className="h-6 w-full cursor-pointer px-2"
          aria-label={`Open ${sessionTitle(s)}: its prompts, errors and subagents`}
        >
          Open
        </Button>
      </div>
    </div>
  );
}

const plural = (n: number, one: string) => `${n} ${one}${n > 1 ? 's' : ''}`;

function SessionRowBadges({
  s,
  unavailable,
}: {
  s: SessionExplorerSession;
  unavailable: boolean;
}) {
  const merged = !!s.prs?.some((pr) => pr.state === 'merged');
  // The row carries no task source, so count user prompts (every local task
  // window is one). A segmented session with no user turns shows no count
  // rather than its task count under the prompt label.
  const prompts = s.userTurns;
  return (
    <span className="flex flex-wrap gap-1">
      <Badge variant="secondary">{agentToolLabel(s.agentTool)}</Badge>
      {s.repos.map((r) => (
        <Badge key={r} variant="outline">
          {r}
        </Badge>
      ))}
      {prompts > 1 ? (
        <Badge variant="outline">{plural(prompts, 'prompt')}</Badge>
      ) : null}
      {merged ? <Badge variant="outline">PR merged</Badge> : null}
      {s.subagents ? (
        <Badge variant="outline">{plural(s.subagents, 'subagent')}</Badge>
      ) : null}
      {s.errors ? (
        <Badge variant="error">{plural(s.errors, 'error')}</Badge>
      ) : null}
      {s.compactions ? (
        <Badge variant="warning">{plural(s.compactions, 'compaction')}</Badge>
      ) : null}
      {unavailable ? (
        <Badge variant="secondary">map not available</Badge>
      ) : null}
    </span>
  );
}

const TOUCH_DOT: Record<string, string> = {
  hit: 'bg-ds-gray-subtler',
  read: 'bg-ds-brand-subtler',
  edit: 'bg-ds-brand-default',
  fail: 'bg-ds-danger-default',
};

/**
 * What a searched session matched on, as chips that open it at that event,
 * and on the selected row a stepper through every matching event. Callers
 * key it on the match list so the stepper restarts when the matches change.
 */
function SearchMatches({
  search,
  selected,
  onJump,
}: {
  search: NonNullable<SessionExplorerSession['search']>;
  selected: boolean;
  onJump: (seq: number) => void;
}) {
  const [i, setI] = useState(0);
  const seqs = search.seqs;
  const go = (next: number) => {
    const k = (next + seqs.length) % seqs.length;
    setI(k);
    onJump(seqs[k]);
  };
  return (
    <div className="flex flex-col gap-1.5 px-2 pb-2">
      <div className="flex flex-wrap gap-1">
        {search.chips.map((c) => (
          <button
            key={`${c.label}:${c.touch}`}
            type="button"
            disabled={c.seq == null}
            onClick={() => c.seq != null && onJump(c.seq)}
            aria-label={c.seq != null ? `${c.label}: open at that event` : c.label}
            className="inline-flex items-center gap-1.5 rounded-full border border-ds-border-subtlest bg-ds-surface-white px-2 py-0.5 ds-text-body-xs-medium text-ds-text-default hover:border-ds-brand-subtler disabled:cursor-default"
          >
            <span aria-hidden className={cn('size-2 rounded-full', TOUCH_DOT[c.touch] ?? TOUCH_DOT.hit)} />
            {c.label}
          </button>
        ))}
      </div>
      {selected && seqs.length > 1 ? (
        <div className="flex items-center gap-1 ds-text-body-xs-medium text-ds-text-subtle">
          <Button variant="ghost" size="iconSm" className="size-6" aria-label="Previous match" onClick={() => go(i - 1)}>
            ‹
          </Button>
          Match {i + 1} of {seqs.length}
          <Button variant="ghost" size="iconSm" className="size-6" aria-label="Next match" onClick={() => go(i + 1)}>
            ›
          </Button>
        </div>
      ) : null}
    </div>
  );
}
