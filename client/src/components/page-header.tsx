import { cn } from "@/lib/utils"

/**
 * Mobile: dark app bar with a square icon action. Desktop: large title with a
 * full button. Pass both so each breakpoint gets the design's control.
 */
export function PageHeader({
  title,
  subtitle,
  mobileSubtitle,
  action,
  mobileAction,
  className,
}: {
  title: React.ReactNode
  subtitle?: React.ReactNode
  mobileSubtitle?: React.ReactNode
  action?: React.ReactNode
  mobileAction?: React.ReactNode
  className?: string
}) {
  return (
    <>
      <header className="bg-sidebar sticky top-0 z-10 flex items-center gap-3 px-4.5 pt-[max(1.375rem,env(safe-area-inset-top))] pb-2.5 md:hidden">
        <div className="min-w-0 flex-1">
          <h1 className="font-heading truncate text-2xl font-medium text-white">
            {title}
          </h1>
          {(mobileSubtitle ?? subtitle) && (
            <p className="truncate text-[11px] text-[#bfd0c6]">
              {mobileSubtitle ?? subtitle}
            </p>
          )}
        </div>
        {mobileAction}
      </header>
      <header
        className={cn(
          "hidden items-start justify-between gap-4 md:flex md:px-11 md:pt-9",
          className,
        )}
      >
        <div className="min-w-0 space-y-1">
          <h1 className="font-heading text-[34px] leading-tight font-medium">
            {title}
          </h1>
          {subtitle && (
            <p className="text-muted-foreground text-sm">{subtitle}</p>
          )}
        </div>
        {action}
      </header>
    </>
  )
}

/** Square translucent button used in the mobile app bar. */
export const mobileActionClass =
  "grid size-11 shrink-0 place-items-center rounded-[10px] bg-white/10 text-white outline-none hover:bg-white/15 focus-visible:ring-2 focus-visible:ring-sidebar-ring disabled:opacity-50 [&_svg]:size-5"
