"use client"

import { useQueryClient } from "@tanstack/react-query"
import { WebStorageStateStore } from "oidc-client-ts"
import { useRouter } from "next/navigation"
import { createContext, use, useMemo, useRef, useState } from "react"
import {
  AuthProvider as OidcProvider,
  useAuth as useOidc,
} from "react-oidc-context"

import { env } from "@/lib/env"

export type User = { sub: string; name: string; email: string }
type Session = { user: User; accessToken: string }

type Auth = {
  /** True until the stored session is read or a sign-in redirect finishes. */
  loading: boolean
  session: Session | null
  /** Plain-language message when the last sign-in attempt failed. */
  error: string | null
  /** True while explicit sign-out is clearing the current account. */
  signingOut: boolean
  /** Sends the user to Cognito's Hosted UI (sign-in and sign-up). */
  signIn: () => Promise<void>
  signOut: () => Promise<void>
  /** Blocks a rejected access token and removes it from persistent auth storage. */
  invalidateSession: (accessToken: string) => void
  isSessionTokenActive: (accessToken: string) => boolean
}

// Cognito Hosted UI with authorization code + PKCE (plan Task 9). NEXT_PUBLIC_*
// must be referenced literally so Next inlines them at build time.
const AUTHORITY = env.cognitoAuthority
const CLIENT_ID = env.cognitoClientId
const DOMAIN = env.cognitoDomain
const SCOPE = env.cognitoScope

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter()
  // react-oidc-context skips the UserManager during SSR; only touch window in the browser.
  const isBrowser = typeof window !== "undefined"

  return (
    <OidcProvider
      authority={AUTHORITY}
      client_id={CLIENT_ID}
      redirect_uri={isBrowser ? `${window.location.origin}/auth/callback` : ""}
      scope={SCOPE}
      // localStorage so an installed PWA survives cold launches.
      userStore={
        isBrowser
          ? new WebStorageStateStore({ store: window.localStorage })
          : undefined
      }
      onSigninCallback={() => {
        router.replace("/memories")
        window.dispatchEvent(new Event("homie:session-restored"))
      }}
    >
      <SessionBridge>{children}</SessionBridge>
    </OidcProvider>
  )
}

function SessionBridge({ children }: { children: React.ReactNode }) {
  const oidc = useOidc()
  const queryClient = useQueryClient()
  const [signingOut, setSigningOut] = useState(false)
  const [invalidatedTokens, setInvalidatedTokens] = useState<Set<string>>(
    () => new Set(),
  )
  const invalidatedTokensRef = useRef(new Set<string>())
  const user = oidc.user && !oidc.user.expired ? oidc.user : null

  const value = useMemo<Auth>(
    () => ({
      loading: oidc.isLoading,
      session:
        user && !invalidatedTokens.has(user.access_token)
          ? {
              accessToken: user.access_token,
              user: {
                sub: user.profile.sub,
                email: user.profile.email ?? "",
                name:
                  user.profile.name ??
                  user.profile.email?.split("@")[0] ??
                  "Homie user",
              },
            }
          : null,
      error: oidc.error
        ? "Couldn't sign you in. Check your connection and try again."
        : null,
      signingOut,
      signIn: async () => {
        await oidc.signinRedirect()
      },
      invalidateSession: (accessToken) => {
        if (invalidatedTokensRef.current.has(accessToken)) return
        invalidatedTokensRef.current.add(accessToken)
        setInvalidatedTokens((current) => new Set(current).add(accessToken))
        if (oidc.user?.access_token === accessToken) {
          void oidc.removeUser().catch(() => undefined)
        }
      },
      isSessionTokenActive: (accessToken) =>
        !invalidatedTokensRef.current.has(accessToken),
      signOut: async () => {
        if (
          !window.confirm("Sign out of Homie? Any unsaved work will be lost.")
        )
          return
        setSigningOut(true)
        try {
          await queryClient.cancelQueries()
          await oidc.removeUser()
          queryClient.clear()
          // Cognito's discovery has no end_session_endpoint, so end its session by hand.
          const logout = new URL("/logout", DOMAIN)
          logout.searchParams.set("client_id", CLIENT_ID)
          logout.searchParams.set("logout_uri", window.location.origin)
          window.location.assign(logout)
        } catch (error) {
          setSigningOut(false)
          throw error
        }
      },
    }),
    [oidc, user, queryClient, signingOut, invalidatedTokens],
  )

  return <AuthContext value={value}>{children}</AuthContext>
}

const AuthContext = createContext<Auth | null>(null)

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
