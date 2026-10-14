"use client"

import { useQueryClient } from "@tanstack/react-query"
import { createContext, use, useMemo, useSyncExternalStore } from "react"

export type User = { sub: string; name: string; email: string }
type Session = { user: User; accessToken: string | null }

type Auth = {
  /** True until the client has read the stored session (SSR/first paint). */
  loading: boolean
  session: Session | null
  signIn: (input: { email: string; password: string }) => Promise<void>
  signUp: (input: {
    name: string
    email: string
    password: string
  }) => Promise<void>
  signOut: () => void
}

const AuthContext = createContext<Auth | null>(null)

// dev-note: stub auth. Sign-in/sign-up aren't wired to Cognito yet (needs the
// dashboard client to allow USER_PASSWORD_AUTH + SignUp). In `next dev` a
// token-less demo session lets you click through the UI; every API call still
// needs a real token, so the server rejects it. Production builds throw.
const KEY = "homie:dev-session"
const EVENT = "homie:auth"
const isDev = process.env.NODE_ENV === "development"

function subscribe(onChange: () => void) {
  window.addEventListener(EVENT, onChange)
  return () => window.removeEventListener(EVENT, onChange)
}

function write(session: Session | null) {
  if (session) sessionStorage.setItem(KEY, JSON.stringify(session))
  else sessionStorage.removeItem(KEY)
  window.dispatchEvent(new Event(EVENT))
}

async function startDevSession(name: string, email: string) {
  if (!isDev) throw new Error("Email sign-in isn't available yet.")
  write({ user: { sub: `dev:${email}`, name, email }, accessToken: null })
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient()
  const raw = useSyncExternalStore(
    subscribe,
    () => (isDev ? (sessionStorage.getItem(KEY) ?? "") : ""),
    () => null,
  )

  const value = useMemo<Auth>(
    () => ({
      loading: raw === null,
      session: raw ? (JSON.parse(raw) as Session) : null,
      signIn: ({ email }) => startDevSession(email.split("@")[0], email),
      signUp: ({ name, email }) => startDevSession(name, email),
      signOut: () => {
        void queryClient.cancelQueries()
        queryClient.clear()
        write(null)
      },
    }),
    [raw, queryClient],
  )

  return <AuthContext value={value}>{children}</AuthContext>
}

export function useAuth() {
  const auth = use(AuthContext)
  if (!auth) throw new Error("useAuth must be used inside <AuthProvider>")
  return auth
}

export function initials(name: string) {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .map((part) => part[0])
    .join("")
    .slice(0, 2)
    .toUpperCase()
}
