import { Inbox, TriangleAlert, WifiOff } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { ApiError } from "@/lib/api"

export function LoadingRows({
  count = 3,
  className = "h-23",
}: {
  count?: number
  className?: string
}) {
  return (
    <div className="space-y-2.5" aria-busy="true">
      <span className="sr-only">Loading…</span>
      {Array.from({ length: count }, (_, i) => (
        <Skeleton key={i} className={`w-full rounded-[10px] ${className}`} />
      ))}
    </div>
  )
}

export function ErrorState({
  error,
  onRetry,
}: {
  error: unknown
  onRetry?: () => void
}) {
  const offline = error instanceof ApiError && error.status === 0
  const Icon = offline ? WifiOff : TriangleAlert
  return (
    <div
      role="alert"
      className="bg-card flex flex-col items-start gap-3 rounded-[10px] border p-5"
    >
      <div className="flex items-center gap-2.5 font-medium">
        <Icon className="text-destructive size-5" aria-hidden />
        {error instanceof Error ? error.message : "Something went wrong."}
      </div>
      {onRetry && (
        <Button variant="outline" size="lg" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  )
}

export function EmptyState({
  icon: Icon = Inbox,
  title,
  children,
}: {
  icon?: React.ComponentType<{ className?: string }>
  title: string
  children?: React.ReactNode
}) {
  return (
    <div className="flex flex-col items-center gap-2 rounded-[10px] border border-dashed px-6 py-10 text-center">
      <Icon className="text-muted-foreground size-6" />
      <p className="font-medium">{title}</p>
      {children && (
        <div className="text-muted-foreground max-w-sm text-sm">{children}</div>
      )}
    </div>
  )
}
