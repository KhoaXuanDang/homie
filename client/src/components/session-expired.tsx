"use client"

import { useEffect, useState } from "react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { useAuth } from "@/lib/auth"

export function SessionExpiredNotice() {
  const auth = useAuth()
  const [open, setOpen] = useState(false)

  useEffect(() => {
    const handleExpired = () => setOpen(true)
    const handleRestored = () => setOpen(false)

    window.addEventListener("homie:session-expired", handleExpired)
    window.addEventListener("homie:session-restored", handleRestored)
    return () => {
      window.removeEventListener("homie:session-expired", handleExpired)
      window.removeEventListener("homie:session-restored", handleRestored)
    }
  }, [])

  const recoverSession = async () => {
    if (
      !window.confirm(
        "Copy or discard unsaved work before signing in again. Continue?",
      )
    ) {
      return
    }
    try {
      await auth.signIn()
    } catch {
      toast.error(
        "Couldn't start sign-in. Check your connection and try again.",
      )
    }
  }

  if (!open) return null

  return (
    <div className="fixed inset-x-4 top-4 z-50 mx-auto max-w-md rounded-lg border border-amber-200 bg-white p-4 shadow-lg">
      <p className="text-foreground text-sm font-medium">
        Your session expired. Copy or discard any unsaved work, then sign in
        again.
      </p>
      <div className="mt-3 flex justify-end gap-2">
        <Button variant="outline" size="sm" onClick={() => setOpen(false)}>
          Close
        </Button>
        <Button size="sm" onClick={() => void recoverSession()}>
          Sign in
        </Button>
      </div>
    </div>
  )
}
