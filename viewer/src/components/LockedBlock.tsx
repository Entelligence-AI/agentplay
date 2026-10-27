import type { ReactNode } from 'react';
import { ArrowSquareOut } from '@phosphor-icons/react';
import { buttonVariants } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { connectUrl, type LinkSource } from '@/core/entelligence';

interface Props {
  title: string;
  blurb: string;
  /**
   * The sample rendering behind the glass. Always invented data -- the point
   * of the block is a capability the local map cannot have, so there is
   * nothing real to put here.
   */
  preview: ReactNode;
  ctaLabel: string;
  src: LinkSource;
  /** whether the sample is un-blurred; owned by the caller, because showing
   * it also swaps the session and the map underneath */
  shown: boolean;
  onToggle: () => void;
  /** rendered beside the buttons when the whole view is on the sample */
  exit?: ReactNode;
}

/**
 * A capability the viewer cannot do locally, shown rather than described: a
 * sample behind frosted glass, a sentence saying what it is, and a link out.
 *
 * Deliberately not a gate on anything that works offline. It appears where
 * the local answer is genuinely empty, it never interrupts playback, and
 * "See sample" un-blurs the sample so nobody has to hand over an email to
 * find out what is being offered. The link is the only thing that leaves
 * the page, and only when clicked.
 */
export function LockedBlock({
  title,
  blurb,
  preview,
  ctaLabel,
  src,
  shown,
  onToggle,
  exit,
}: Props) {
  return (
    // A 2px brand keyline, not the hairline every other card wears: this is
    // the one block in the panel offering something, and at a hairline it
    // read as another quiet row rather than a thing to act on.
    <section className="relative mt-3 shrink-0 overflow-hidden rounded-lg border-2 border-ds-brand-subtler bg-ds-surface-white">
      {/* Collapsed, the sample is wallpaper: it fills the card the text
       * below sizes, so the block is never taller than what it has to say,
       * and it is hidden from assistive tech and inert so nobody tabs into
       * a blurred card. Revealed, it joins the flow, takes its own height,
       * and anything interactive in it has to work. */}
      <div
        aria-hidden={shown ? undefined : true}
        className={cn(
          'p-3 transition-[filter,opacity]',
          shown
            ? 'opacity-100'
            : 'pointer-events-none absolute inset-0 select-none opacity-40 blur-[4px]'
        )}
      >
        {preview}
      </div>
      <div
        className={cn(
          'relative flex flex-col gap-2 p-3',
          shown
            ? 'border-t border-ds-border-subtlest'
            : // Nearly opaque, not a tint. The sample only has to read as
              // texture; the sentence over it has to be readable, and at
              // /70 the sample's own words showed through the blurb.
              'bg-ds-surface-white/[0.92] backdrop-blur-[6px]'
        )}
      >
        <div className="flex flex-col gap-0.5">
          <p className="ds-text-body-xs-semibold text-ds-text-default">
            {title}
          </p>
          <p className="ds-text-body-xs-medium text-ds-text-default">{blurb}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {/* The link is the primary action and is dressed as one: brand
           * fill, and a soft brand halo so it carries across the panel's
           * quiet greys in both themes. "See sample" stays ghost -- two
           * loud buttons is no emphasis at all. */}
          <a
            href={connectUrl(src)}
            target="_blank"
            rel="noopener noreferrer"
            className={cn(
              buttonVariants({ variant: 'default', size: 'sm' }),
              // The solid brand weight (the `brandSolid` badge's tokens), so
              // the button is a filled colour rather than a tint, and the
              // same colour again as a halo. Holds up on both themes: the
              // fill carries light mode, the halo carries dark.
              'border-transparent bg-ds-brand-fill font-semibold text-ds-brand-on-fill hover:bg-ds-brand-subtler active:bg-ds-brand-subtler',
              'shadow-[0_0_0_1px_var(--primitive-brand-green-500),0_3px_16px_-2px_var(--primitive-brand-green-400)] transition-shadow hover:shadow-[0_0_0_1px_var(--primitive-brand-green-400),0_5px_24px_-2px_var(--primitive-brand-green-400)]'
            )}
          >
            {ctaLabel}
            <ArrowSquareOut size={12} aria-hidden />
          </a>
          <button
            type="button"
            onClick={onToggle}
            className={cn(buttonVariants({ variant: 'ghost', size: 'sm' }))}
          >
            {shown ? 'Hide sample' : 'See sample'}
          </button>
          {exit}
        </div>
      </div>
    </section>
  );
}
