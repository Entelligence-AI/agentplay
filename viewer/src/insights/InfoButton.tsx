import { Info, X } from '@phosphor-icons/react';
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import { cn } from '@/lib/utils';
import { GLOSSARY, type TermKey } from './glossary';

/** A small (i) next to a term that opens its plain-language definition. */
export function InfoButton({ term, className }: { term: TermKey; className?: string }) {
  const t = GLOSSARY[term];
  return (
    <Dialog>
      <DialogTrigger
        aria-label={`What is ${t.title.toLowerCase()}?`}
        className={cn(
          'inline-flex size-6 shrink-0 items-center justify-center rounded-full text-ds-text-subtle hover:bg-ds-gray-subtlest hover:text-ds-text-darker focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ds-brand-subtler',
          className
        )}
      >
        <Info size={15} aria-hidden />
      </DialogTrigger>
      <DialogContent className="w-[min(34rem,calc(100vw-2rem))] gap-3 p-5">
        <div className="flex items-start gap-3">
          <DialogTitle className="flex-1 ds-text-title-sm-semibold text-ds-text-darker">{t.title}</DialogTitle>
          <DialogClose
            aria-label="Close"
            className="inline-flex size-8 items-center justify-center rounded-md border border-ds-border-subtlest text-ds-text-subtle hover:bg-ds-gray-subtlest"
          >
            <X size={14} aria-hidden />
          </DialogClose>
        </div>
        {t.paras.map((p) => (
          <p key={p} className="ds-text-body-sm-medium text-ds-text-default">
            {p}
          </p>
        ))}
        {'rows' in t && t.rows ? (
          <dl className="grid grid-cols-[max-content_1fr] gap-x-4 border-t border-ds-border-subtlest">
            {t.rows.map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="border-b border-ds-border-subtlest py-2 ds-text-body-xs-semibold text-ds-text-darker">{k}</dt>
                <dd className="border-b border-ds-border-subtlest py-2 ds-text-body-xs-medium text-ds-text-default">{v}</dd>
              </div>
            ))}
          </dl>
        ) : null}
        {'formula' in t && t.formula ? (
          <p className="rounded-lg border border-ds-border-subtlest bg-ds-gray-subtlest px-3 py-2 font-mono text-xs text-ds-text-default">
            {t.formula}
          </p>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
