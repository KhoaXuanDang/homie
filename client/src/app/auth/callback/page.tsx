"use client"

import { Button } from "@/components/ui/button"
import { useAuth } from "@/lib/auth"

// The OIDC provider finishes the code exchange here, then replaces the route with /memories.
export default function AuthCallback() {
  const { error, signIn } = useAuth()

  return (
    <main className="flex min-h-dvh flex-col items-center justify-center gap-4 p-6 text-center">
      {error ? (
        <>
          <p role="alert">{error}</p>
          <Button size="lg" onClick={signIn}>
            Try again
          </Button>
        </>
      ) : (
        <p aria-busy="true">Signing you in…</p>
      )}
    </main>
  )
}
