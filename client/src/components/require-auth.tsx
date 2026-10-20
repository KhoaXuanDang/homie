"use client"

import { useRouter } from "next/navigation"
import { useEffect, useState } from "react"
import { useQueryClient } from "@tanstack/react-query"

import { Skeleton } from "@/components/ui/skeleton"
import { useAuth } from "@/lib/auth"

export function RequireAuth({ children }: { children: React.ReactNode }) {
  const { loading, session, signingOut } = useAuth()
  const queryClient = useQueryClient()
  const router = useRouter()
  const [lastSub, setLastSub] = useState<string | null>(
    session?.user.sub ?? null,
  )
  const currentSub = session?.user.sub ?? null
  const accountChanged = currentSub !== null && currentSub !== lastSub

  useEffect(() => {
    if (!accountChanged || !currentSub) return
    let cancelled = false
    const clearPreviousAccount = async () => {
      try {
        await queryClient.cancelQueries()
      } catch {
        // Clearing the cache still matters if a query cancellation rejects.
      } finally {
        queryClient.clear()
        if (!cancelled) setLastSub(currentSub)
      }
    }
    void clearPreviousAccount()
    return () => {
      cancelled = true
    }
  }, [accountChanged, currentSub, queryClient])

  useEffect(() => {
    if (signingOut || (!loading && !session && !lastSub)) router.replace("/")
  }, [loading, session, lastSub, router, signingOut])

  if (signingOut || accountChanged || (!session && !lastSub)) {
    return (
      <div className="flex min-h-dvh" aria-busy="true">
        <span className="sr-only">Loading…</span>
        <Skeleton className="bg-sidebar hidden w-60 rounded-none md:block" />
        <div className="flex-1 space-y-4 p-6 md:p-11">
          <Skeleton className="h-10 w-56" />
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
        </div>
      </div>
    )
  }

  // Keep the protected subtree mounted through token recovery; its API hooks
  // still see the current (possibly null) session and must gate requests.
  return <div key={session?.user.sub ?? lastSub}>{children}</div>
}
