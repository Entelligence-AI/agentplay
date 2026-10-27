/**
 * The images pasted into one task's prompts: a thumbnail grid, and a lightbox
 * to read one at full size. The bytes live on the machine that recorded the
 * session, so every way they can be missing (a source that never has them, a
 * refused request, an entry the server marks unavailable, an image that fails
 * to load) lands on the same honest "unavailable" state instead of an error.
 */
import { useMemo, useState } from 'react';
import { ImageSquare, WarningCircle, X } from '@phosphor-icons/react';
import { buttonVariants } from '@/components/ui/button';
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogTitle,
} from '@/components/ui/dialog';
import { Skeleton } from '@/components/ui/skeleton';
import {
  imageLabel,
  pasteTag,
  type PromptImageTurn,
} from '@/core/promptImages';
import { usePromptImages } from '@/data/hooks';
import { useSessionMapSource } from '@/data/SourceContext';
import { cn } from '@/lib/utils';

const TILE =
  'relative aspect-[4/3] overflow-hidden rounded-lg border border-ds-border-subtlest bg-ds-gray-subtlest';

interface Slot {
  key: string;
  turn: number;
  /** index within its turn, what the image endpoint takes */
  i: number;
  pasteId: number | null;
  mediaType: string;
  available: boolean;
}

const imagesLabel = (n: number) => `${n} ${n === 1 ? 'image' : 'images'}`;

function Unavailable({ count }: { count: number }) {
  return (
    <div
      role="note"
      className="flex items-start gap-2 rounded-lg border border-dashed border-ds-border-subtler p-3 ds-text-body-xs-medium text-ds-text-subtle"
    >
      <WarningCircle size={14} aria-hidden className="mt-px shrink-0" />
      <span>
        {imagesLabel(count)} attached. Images stay on the machine that recorded
        the session.
      </span>
    </div>
  );
}

export function PromptImages({
  sessionId,
  turns,
}: {
  sessionId: string;
  /** this task's image turns, from `promptImageTurns` over the full map */
  turns: PromptImageTurn[];
}) {
  const source = useSessionMapSource();
  const total = turns.reduce((s, t) => s + t.count, 0);
  const canServe = !!source.promptImages && !!source.promptImageUrl;
  const throughTurn = turns.reduce((m, t) => Math.max(m, t.turn), 0);
  const query = usePromptImages(sessionId, throughTurn, canServe && total > 0);
  const [failed, setFailed] = useState<ReadonlySet<string>>(new Set());
  const [openKey, setOpenKey] = useState<string | null>(null);

  // Join the map's turns with what the server listed; an image the server
  // did not list is unavailable, so a short answer pads rather than hides.
  const slots = useMemo<Slot[]>(() => {
    const byTurn = new Map(query.data?.turns.map((t) => [t.turn, t.images]));
    return turns.flatMap(({ turn, count }) =>
      Array.from({ length: count }, (_, i) => {
        const img = byTurn.get(turn)?.find((x) => x.i === i);
        return {
          key: `${turn}:${i}`,
          turn,
          i,
          pasteId: img?.pasteId ?? null,
          mediaType: img?.mediaType ?? '',
          available: !!img?.available,
        };
      })
    );
  }, [turns, query.data]);

  if (total === 0) return null;
  if (!canServe || query.isError) return <Unavailable count={total} />;
  if (query.isPending) {
    return (
      <div
        className="grid grid-cols-2 gap-2"
        role="status"
        aria-label="Loading images"
      >
        {Array.from({ length: total }, (_, i) => (
          <Skeleton key={i} className="aspect-[4/3] rounded-lg" />
        ))}
      </div>
    );
  }

  const urlOf = (s: Slot) => source.promptImageUrl!(sessionId, s.turn, s.i);
  const markFailed = (key: string) =>
    setFailed((prev) => new Set(prev).add(key));
  const openable = slots.filter((s) => s.available && !failed.has(s.key));
  const current = openable.findIndex((s) => s.key === openKey);
  const step = (delta: number) => {
    const next = openable[current + delta];
    if (next) setOpenKey(next.key);
  };
  const position = (s: Slot) => slots.indexOf(s) + 1;

  return (
    <>
      <ul className="grid grid-cols-2 gap-2" aria-label="Pasted images">
        {slots.map((s) => (
          <li key={s.key}>
            {s.available && !failed.has(s.key) ? (
              <button
                type="button"
                aria-label={`Open image ${position(s)} of ${total}`}
                onClick={() => setOpenKey(s.key)}
                className={cn(
                  TILE,
                  'block w-full cursor-pointer outline-none transition-colors hover:border-ds-border-subtler focus-visible:ring-2 focus-visible:ring-ds-brand-subtle'
                )}
              >
                <img
                  src={urlOf(s)}
                  alt=""
                  loading="lazy"
                  decoding="async"
                  onError={() => markFailed(s.key)}
                  className="size-full object-contain"
                />
                <span className="absolute bottom-1 left-1 rounded bg-ds-surface-white/80 px-1 ds-text-body-xs-medium text-ds-text-darker">
                  {imageLabel(s.pasteId, position(s) - 1, s.mediaType)}
                </span>
              </button>
            ) : (
              <div
                className={cn(
                  TILE,
                  'flex items-center justify-center gap-1 border-dashed text-ds-text-subtler'
                )}
                title="Images stay on the machine that recorded the session."
              >
                <ImageSquare size={14} aria-hidden />
                <span className="ds-text-body-xs-medium">Unavailable</span>
              </div>
            )}
          </li>
        ))}
      </ul>
      <Lightbox
        // a lightbox with no openable image has nothing to show
        slot={current >= 0 ? openable[current] : null}
        url={current >= 0 ? urlOf(openable[current]) : ''}
        position={current >= 0 ? position(openable[current]) : 0}
        total={total}
        hasPrev={current > 0}
        hasNext={current >= 0 && current < openable.length - 1}
        onStep={step}
        onClose={() => setOpenKey(null)}
        onError={markFailed}
      />
    </>
  );
}

function Lightbox({
  slot,
  url,
  position,
  total,
  hasPrev,
  hasNext,
  onStep,
  onClose,
  onError,
}: {
  slot: Slot | null;
  url: string;
  position: number;
  total: number;
  hasPrev: boolean;
  hasNext: boolean;
  onStep: (delta: number) => void;
  onClose: () => void;
  onError: (key: string) => void;
}) {
  // Natural size is only known once the bytes decode; keyed by slot so a
  // neighbour never shows the previous image's dimensions.
  const [size, setSize] = useState<{ key: string; w: number; h: number } | null>(
    null
  );
  const dims = slot && size?.key === slot.key ? ` · ${size.w}×${size.h}` : '';
  return (
    <Dialog open={!!slot} onOpenChange={(open) => !open && onClose()}>
      <DialogContent
        aria-describedby={undefined}
        className="w-[min(64rem,calc(100vw-2rem))] max-h-[calc(100vh-2rem)]"
        onKeyDown={(e) => {
          if (e.key === 'ArrowLeft') onStep(-1);
          else if (e.key === 'ArrowRight') onStep(1);
          else return;
          e.preventDefault();
        }}
      >
        {slot ? (
          <>
            <div className="flex items-center justify-between gap-2">
              <DialogTitle className="ds-text-body-sm-semibold text-ds-text-darker">
                Image {position} of {total} · {pasteTag(slot.pasteId, position - 1)}
                {dims}
              </DialogTitle>
              <DialogClose
                render={
                  <button
                    type="button"
                    aria-label="Close"
                    className={cn(
                      buttonVariants({ variant: 'ghost', size: 'sm' }),
                      'cursor-pointer'
                    )}
                  >
                    <X size={14} aria-hidden />
                  </button>
                }
              />
            </div>
            <div className="flex items-center justify-center rounded-lg bg-ds-gray-subtlest">
              <img
                src={url}
                alt={`Pasted ${position} of ${total}`}
                decoding="async"
                onLoad={(e) =>
                  setSize({
                    key: slot.key,
                    w: e.currentTarget.naturalWidth,
                    h: e.currentTarget.naturalHeight,
                  })
                }
                onError={() => onError(slot.key)}
                className="max-h-[80vh] max-w-full object-contain"
              />
            </div>
            <div className="flex justify-between gap-2">
              <button
                type="button"
                aria-label="Previous image"
                disabled={!hasPrev}
                onClick={() => onStep(-1)}
                className={cn(
                  buttonVariants({ variant: 'outline', size: 'sm' }),
                  'cursor-pointer'
                )}
              >
                Previous
              </button>
              <button
                type="button"
                aria-label="Next image"
                disabled={!hasNext}
                onClick={() => onStep(1)}
                className={cn(
                  buttonVariants({ variant: 'outline', size: 'sm' }),
                  'cursor-pointer'
                )}
              >
                Next
              </button>
            </div>
          </>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
