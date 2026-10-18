"use client"

import { useRouter } from "next/navigation"
import { useEffect } from "react"

import { Skeleton } from "@/components/ui/skeleton"
import { useAuth } from "@/lib/auth"

export function RequireAuth({ children }: { children: React.ReactNode }) {
  const { loading, session } = useAuth()
  const router = useRouter()

  useEffect(() => {
    if (!loading && !session) router.replace("/")
  }, [loading, session, router])

  if (!session) {
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
  return children
}
