import * as React from 'react';
import { Dialog as DialogPrimitive } from '@base-ui/react/dialog';

import { cn } from '@/lib/utils';

const Dialog = DialogPrimitive.Root;

const DialogTrigger = DialogPrimitive.Trigger;

const DialogClose = DialogPrimitive.Close;

const DialogTitle = DialogPrimitive.Title;

const DialogDescription = DialogPrimitive.Description;

const DialogPortal = DialogPrimitive.Portal;

const DialogViewport = DialogPrimitive.Viewport;

const DialogPopup = DialogPrimitive.Popup;

const DialogContent = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Popup>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Popup>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Portal>
    <DialogPrimitive.Backdrop className="fixed inset-0 z-[1000] bg-black/40 transition-opacity data-ending-style:opacity-0 data-starting-style:opacity-0" />
    <DialogPrimitive.Popup
      ref={ref}
      className={cn(
        'fixed left-1/2 top-1/2 z-[1001] flex max-h-[min(40rem,calc(100vh-3rem))] w-[min(30rem,calc(100vw-2rem))] -translate-x-1/2 -translate-y-1/2 flex-col gap-3 overflow-y-auto rounded-xl border border-ds-border-subtler bg-ds-surface-white p-4 text-ds-text-default shadow-lg outline-none transition-[opacity,transform] data-ending-style:scale-95 data-ending-style:opacity-0 data-starting-style:scale-95 data-starting-style:opacity-0',
        className
      )}
      {...props}
    />
  </DialogPrimitive.Portal>
));
DialogContent.displayName = DialogPrimitive.Popup.displayName;

export {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogPopup,
  DialogPortal,
  DialogTitle,
  DialogTrigger,
  DialogViewport,
};
