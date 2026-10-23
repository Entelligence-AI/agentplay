import { useEffect, useRef, useState, type ReactNode, type RefObject } from 'react';
import { CaretDown, Check, Link } from '@phosphor-icons/react';
import { Button } from '@/components/ui/button';
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from '@/components/ui/command';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
import { cn } from '@/lib/utils';
import { taskFilterLabel, type AgentOption } from '@/core/sessionLayout';
import { fmtUsd } from '@/core/sessionMapModel';
import {
  taskNoun,
  type ModelUsage,
  type TaskWindow,
} from '@/core/sessionScope';

interface Props {
  tasks: TaskWindow[];
  task: number | null;
  onTask: (n: number | null) => void;
  /** the Agent picker's rows (`agentOptions`); empty hides the picker */
  agents: AgentOption[];
  /** the agent lens: `main` or a subagent id */
  lens: string;
  lensLabel: string | null;
  onLens: (id: string) => void;
  models: ModelUsage[];
  model: string | null;
  onModel: (model: string | null) => void;
  /** a filter or subagent is narrowing the replay */
  filtered: boolean;
  onClear: () => void;
}

/**
 * The Task / Agent / Model filters, rendered inline at the right of the
 * session header's one line (fit-viewport layout). Each filter opens a
 * popover; the URL params and what each choice does are unchanged. What the
 * timeline, map and Activity highlight is the dock's category chips.
 */
export function ExplorerToolbar(props: Props) {
  return (
    <div className="flex shrink-0 flex-wrap items-center gap-1.5">
      <div
        role="group"
        aria-label="Filter the map, timeline and side panel"
        className="flex flex-wrap items-center gap-1.5"
      >
        {props.tasks.length > 1 ? <TaskPicker {...props} /> : null}
        {props.agents.length ? <AgentPicker {...props} /> : null}
        {props.models.length > 1 ? <ModelPicker {...props} /> : null}
        {props.filtered ? (
          <Button
            variant="link"
            size="sm"
            onClick={props.onClear}
            className="h-7 px-1.5 text-ds-text-subtle"
          >
            Clear filters
          </Button>
        ) : null}
      </div>
      <CopyLinkButton />
    </div>
  );
}

/** A local button that copies the current viewer URL to clipboard. */
function CopyLinkButton() {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      setCopied(true);
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => {
        setCopied(false);
      }, 2000);
    } catch {
      // Fallback or ignore clipboard errors quietly in non-secure contexts
    }
  };

  return (
    <Button
      variant="outline"
      size="sm"
      onClick={handleCopy}
      aria-label={copied ? 'Link copied' : 'Copy link to current view'}
      aria-live="polite"
      className={cn(
        'h-7 gap-1.5 px-2.5 font-normal text-ds-text-darker transition-colors',
        copied && 'border-ds-brand-subtler bg-ds-brand-lightest text-ds-brand-default'
      )}
    >
      {copied ? (
        <>
          <Check size={12} aria-hidden className="shrink-0" />
          <span>Copied</span>
        </>
      ) : (
        <>
          <Link size={12} aria-hidden className="shrink-0 text-ds-text-subtle" />
          <span>Copy link</span>
        </>
      )}
    </Button>
  );
}

/** The trigger's "Label: value ▾" contents. */
function TriggerLabel({
  label,
  value,
  active,
}: {
  label: string;
  value: string;
  active: boolean;
}) {
  return (
    <>
      <span className="text-ds-text-subtle">{label}:</span>
      <span
        className={cn(
          'min-w-0 truncate',
          active ? 'text-ds-brand-default' : 'text-ds-text-darker'
        )}
      >
        {value}
      </span>
      <CaretDown size={10} aria-hidden className="shrink-0" />
    </>
  );
}

/** A filter popover: trigger button + content, closed on a pick. */
function Picker({
  label,
  value,
  active,
  width = 'w-80',
  initialFocus,
  children,
}: {
  label: string;
  value: string;
  active: boolean;
  width?: string;
  initialFocus?: RefObject<HTMLElement | null>;
  children: (close: () => void) => ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        render={
          <Button
            variant="outline"
            size="sm"
            aria-label={`${label}: ${value}`}
            className={cn(
              'h-7 max-w-[260px] gap-1 px-2.5 font-normal',
              active && 'border-ds-brand-subtler bg-ds-brand-lightest'
            )}
          >
            <TriggerLabel label={label} value={value} active={active} />
          </Button>
        }
      />
      <PopoverContent
        align="start"
        initialFocus={initialFocus}
        className={cn(width, 'p-0')}
      >
        {children(() => setOpen(false))}
      </PopoverContent>
    </Popover>
  );
}

const Tick = ({ on }: { on: boolean }) => (
  <Check
    size={12}
    aria-hidden
    className={cn('shrink-0', on ? 'opacity-100' : 'opacity-0')}
  />
);

const Meta = ({ children }: { children: ReactNode }) => (
  <span className="ml-auto shrink-0 font-mono ds-text-body-xs-medium text-ds-text-subtler tabular-nums">
    {children}
  </span>
);

function TaskPicker({ tasks, task, onTask }: Props) {
  const noun = taskNoun(tasks);
  const many = noun.many.toLowerCase();
  return (
    <Picker
      label={noun.one}
      value={taskFilterLabel(tasks, task)}
      active={task != null}
    >
      {(close) => (
        <Command>
          <CommandInput placeholder={`Search ${many}…`} className="h-9" />
          <CommandList className="max-h-[320px]">
            <CommandEmpty>No {noun.one.toLowerCase()} matches.</CommandEmpty>
            <CommandGroup>
              <CommandItem
                value={`all ${many}`}
                onSelect={() => {
                  onTask(null);
                  close();
                }}
              >
                <Tick on={task == null} />
                All {many}
                <Meta>{tasks.length}</Meta>
              </CommandItem>
              {tasks.map((t) => (
                <CommandItem
                  key={t.n}
                  value={`${t.n} ${t.name}`}
                  onSelect={() => {
                    onTask(task === t.n ? null : t.n);
                    close();
                  }}
                >
                  <Tick on={task === t.n} />
                  <span className="shrink-0 font-mono tabular-nums">{t.n}</span>
                  <span className="min-w-0 truncate" title={t.name}>
                    {t.named ? t.name : ''}
                  </span>
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      )}
    </Picker>
  );
}

function AgentPicker({ agents, lens, lensLabel, onLens }: Props) {
  const subCount = agents.length - 1;
  const subNoun = `${subCount} subagent${subCount === 1 ? '' : 's'}`;
  return (
    <Picker
      label="Agent"
      value={lensLabel ?? `All agents (${subNoun})`}
      active={lens !== 'main'}
    >
      {(close) => (
        <Command>
          <CommandInput placeholder="Search subagents…" className="h-9" />
          <CommandList className="max-h-[320px]">
            <CommandEmpty>No subagent matches.</CommandEmpty>
            <CommandGroup>
              {agents.map((a) => (
                <CommandItem
                  key={a.id}
                  value={`${a.label} ${a.id}`}
                  className={cn(a.empty && 'opacity-50')}
                  onSelect={() => {
                    onLens(a.id);
                    close();
                  }}
                >
                  <Tick on={lens === a.id} />
                  <span className="min-w-0 truncate">{a.label}</span>
                  <Meta>
                    {a.calls} calls · {fmtUsd(a.cost)}
                  </Meta>
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
          <p className="border-t border-ds-border-subtlest px-3 py-1.5 ds-text-body-xs-medium text-ds-text-subtler">
            A subagent has no further nesting.
          </p>
        </Command>
      )}
    </Picker>
  );
}

function ModelPicker({ models, model, onModel }: Props) {
  const list = useRef<HTMLDivElement>(null);
  return (
    <Picker
      label="Model"
      value={model ?? 'All models'}
      active={model != null}
      width="w-72"
      initialFocus={list}
    >
      {(close) => (
        <Command defaultValue={model ?? 'all models'}>
          <CommandList ref={list} label="Models" className="max-h-[320px]">
            <CommandGroup>
              <CommandItem
                value="all models"
                onSelect={() => {
                  onModel(null);
                  close();
                }}
              >
                <Tick on={model == null} />
                All models
              </CommandItem>
              {models.map((m) => (
                <CommandItem
                  key={m.model}
                  value={m.model}
                  className={cn(m.requests === 0 && 'opacity-50')}
                  onSelect={() => {
                    onModel(model === m.model ? null : m.model);
                    close();
                  }}
                >
                  <Tick on={model === m.model} />
                  <span className="min-w-0 truncate">{m.model}</span>
                  <Meta>{m.requests} req</Meta>
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      )}
    </Picker>
  );
}