import * as React from "react"
import * as PopoverPrimitive from "@radix-ui/react-popover"

export const Popover = PopoverPrimitive.Root
export const PopoverTrigger = PopoverPrimitive.Trigger

export const PopoverContent = React.forwardRef<
  React.ElementRef<typeof PopoverPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof PopoverPrimitive.Content>
>(({ className = "", align = "center", sideOffset = 8, ...props }, ref) => (
  <PopoverPrimitive.Content
    ref={ref}
    align={align}
    sideOffset={sideOffset}
    className={
      "z-50 w-auto rounded-md border bg-background p-0 shadow-md outline-none " +
      className
    }
    {...props}
  />
))
PopoverContent.displayName = "PopoverContent"


