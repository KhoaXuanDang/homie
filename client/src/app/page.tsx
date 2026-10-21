"use client"

import { useRouter } from "next/navigation"
import { useEffect } from "react"

import { Button } from "@/components/ui/button"
import { useAuth } from "@/lib/auth"

export default function Home() {
  const auth = useAuth()
  const router = useRouter()

  useEffect(() => {
    if (auth.session) router.replace("/memories")
  }, [auth.session, router])

  return (
    <main className="bg-welcome flex min-h-dvh flex-col items-center justify-center gap-6 p-6 text-center">
      <div className="bg-primary/10 text-primary grid size-16 place-items-center rounded-2xl text-3xl font-bold">
        h
      </div>
      <div className="space-y-3">
        <h1 className="font-heading text-4xl font-bold tracking-tight">
          homie
        </h1>
        <p className="text-muted-foreground max-w-md text-balance">
          Remember the little things. Homie keeps track of where things are,
          what you like, and what is coming up.
        </p>
      </div>
      {auth.error && (
        <p role="alert" className="text-destructive text-sm">
          {auth.error}
        </p>
      )}
      <Button
        size="lg"
        className="min-h-11 min-w-40"
        disabled={auth.loading}
        onClick={() => void auth.signIn()}
      >
        {auth.loading ? "Loading…" : "Sign in"}
      </Button>
    </main>
  )
}
