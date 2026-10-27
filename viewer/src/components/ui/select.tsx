import * as React from 'react';
import { Select as SelectPrimitive } from '@base-ui/react/select';
import { CaretDown, CaretUp, Check } from '@phosphor-icons/react';

import { cn } from '@/lib/utils';

const Select = SelectPrimitive.Root;

const SelectGroup = SelectPrimitive.Group;

const SelectValue = SelectPrimitive.Value;

const SelectTrigger = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Trigger>
>(({ className, children, ...props }, ref) => (
  <SelectPrimitive.Trigger
    ref={ref}
    className={cn(
      'flex h-9 w-full items-center justify-between whitespace-nowrap overflow-hidden rounded-lg border border-ds-border-subtlest bg-ds-surface-white px-3 py-2 text-sm text-ds-text-default data-[placeholder]:text-ds-text-subtle shadow-sm focus:outline-none focus-visible:ring-0 disabled:cursor-not-allowed disabled:opacity-50 [&>span]:line-clamp-1 [&>span]:truncate',
      className
    )}
    {...props}
  >
    {children}
    <SelectPrimitive.Icon
      render={<CaretDown size={16} aria-hidden className="text-ds-text-subtle" />}
    />
  </SelectPrimitive.Trigger>
));
SelectTrigger.displayName = SelectPrimitive.Trigger.displayName;

const SelectScrollUpButton = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.ScrollUpArrow>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.ScrollUpArrow>
>(({ className, ...props }, ref) => (
  <SelectPrimitive.ScrollUpArrow
    ref={ref}
    className={cn(
      'top-0 w-full flex cursor-default items-center justify-center py-1',
      className
    )}
    {...props}
  >
    <CaretUp size={16} aria-hidden />
  </SelectPrimitive.ScrollUpArrow>
));
SelectScrollUpButton.displayName = SelectPrimitive.ScrollUpArrow.displayName;

const SelectScrollDownButton = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.ScrollDownArrow>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.ScrollDownArrow>
>(({ className, ...props }, ref) => (
  <SelectPrimitive.ScrollDownArrow
    ref={ref}
    className={cn(
      'bottom-0 w-full flex cursor-default items-center justify-center py-1',
      className
    )}
    {...props}
  >
    <CaretDown size={16} aria-hidden />
  </SelectPrimitive.ScrollDownArrow>
));
SelectScrollDownButton.displayName =
  SelectPrimitive.ScrollDownArrow.displayName;

const SelectContent = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Popup>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Popup> &
    Pick<
      SelectPrimitive.Positioner.Props,
      'alignItemWithTrigger' | 'sideOffset' | 'align' | 'alignOffset' | 'side'
    > & {
      container?: HTMLElement | null;
    }
>(
  (
    {
      className,
      children,
      container,
      alignItemWithTrigger = false,
      sideOffset = 4,
      align,
      alignOffset,
      side,
      ...props
    },
    ref
  ) => (
    <SelectPrimitive.Portal container={container ?? undefined}>
      <SelectPrimitive.Positioner
        className="isolate z-[1000]"
        alignItemWithTrigger={alignItemWithTrigger}
        sideOffset={sideOffset}
        align={align}
        alignOffset={alignOffset}
        side={side}
      >
        <SelectPrimitive.Popup
          ref={ref}
          className={cn(
            'relative isolate z-50 max-h-[var(--available-height,20rem)] w-[var(--anchor-width)] min-w-[8rem] overflow-y-auto overflow-x-hidden rounded-md border border-ds-border-subtlest bg-ds-surface-white p-1 text-ds-text-default shadow-md origin-[var(--transform-origin)] transition-[opacity,transform] data-[starting-style]:opacity-0 data-[ending-style]:opacity-0 data-[starting-style]:scale-95 data-[ending-style]:scale-95 data-[side=bottom]:data-[starting-style]:-translate-y-2 data-[side=top]:data-[starting-style]:translate-y-2 data-[side=left]:data-[starting-style]:translate-x-2 data-[side=right]:data-[starting-style]:-translate-x-2',
            className
          )}
          {...props}
        >
          <SelectScrollUpButton />
          <SelectPrimitive.List>{children}</SelectPrimitive.List>
          <SelectScrollDownButton />
        </SelectPrimitive.Popup>
      </SelectPrimitive.Positioner>
    </SelectPrimitive.Portal>
  )
);
SelectContent.displayName = SelectPrimitive.Popup.displayName;

const SelectLabel = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.GroupLabel>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.GroupLabel>
>(({ className, ...props }, ref) => (
  <SelectPrimitive.GroupLabel
    ref={ref}
    className={cn(
      'px-2 py-1.5 text-sm font-semibold text-ds-text-default',
      className
    )}
    {...props}
  />
));
SelectLabel.displayName = SelectPrimitive.GroupLabel.displayName;

const SelectItem = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Item>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Item>
>(({ className, children, ...props }, ref) => (
  <SelectPrimitive.Item
    ref={ref}
    className={cn(
      'relative flex w-full cursor-default select-none items-center rounded-sm py-1.5 pl-2 pr-8 text-sm outline-none text-ds-text-default data-[highlighted]:bg-ds-gray-subtlest data-[highlighted]:text-ds-text-default data-[disabled]:pointer-events-none data-[disabled]:opacity-50',
      className
    )}
    {...props}
  >
    <SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText>
    <SelectPrimitive.ItemIndicator
      render={
        <span className="absolute right-2 flex h-3.5 w-3.5 items-center justify-center" />
      }
    >
      <Check size={16} aria-hidden className="text-ds-brand-subtle" />
    </SelectPrimitive.ItemIndicator>
  </SelectPrimitive.Item>
));
SelectItem.displayName = SelectPrimitive.Item.displayName;

const SelectSeparator = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Separator>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Separator>
>(({ className, ...props }, ref) => (
  <SelectPrimitive.Separator
    ref={ref}
    className={cn('-mx-1 my-1 h-px bg-border', className)}
    {...props}
  />
));
SelectSeparator.displayName = SelectPrimitive.Separator.displayName;

export {
  Select,
  SelectGroup,
  SelectValue,
  SelectTrigger,
  SelectContent,
  SelectLabel,
  SelectItem,
  SelectSeparator,
};
