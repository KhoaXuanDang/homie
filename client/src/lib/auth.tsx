"use client"

import { useQueryClient } from "@tanstack/react-query"
import { WebStorageStateStore } from "oidc-client-ts"
import { useRouter } from "next/navigation"
import { createContext, use, useMemo } from "react"
import {
  AuthProvider as OidcProvider,
  useAuth as useOidc,
} from "react-oidc-context"

export type User = { sub: string; name: string; email: string }
type Session = { user: User; accessToken: string }

type Auth = {
  /** True until the stored session is read or a sign-in redirect finishes. */
  loading: boolean
  session: Session | null
  /** Plain-language message when the last sign-in attempt failed. */
  error: string | null
  /** Sends the user to Cognito's Hosted UI (sign-in and sign-up). */
  signIn: () => Promise<void>
  signOut: () => Promise<void>
}

// Cognito Hosted UI with authorization code + PKCE (plan Task 9). NEXT_PUBLIC_*
// must be referenced literally so Next inlines them at build time.
const AUTHORITY = process.env.NEXT_PUBLIC_COGNITO_AUTHORITY ?? ""
const CLIENT_ID = process.env.NEXT_PUBLIC_COGNITO_CLIENT_ID ?? ""
const DOMAIN = process.env.NEXT_PUBLIC_COGNITO_DOMAIN ?? ""
const SCOPE =
  process.env.NEXT_PUBLIC_COGNITO_SCOPE ??
  "openid email profile homie/mcp:tools"

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
      onSigninCallback={() => router.replace("/memories")}
    >
      <SessionBridge>{children}</SessionBridge>
    </OidcProvider>
  )
}

function SessionBridge({ children }: { children: React.ReactNode }) {
  const oidc = useOidc()
  const queryClient = useQueryClient()
  const user = oidc.user && !oidc.user.expired ? oidc.user : null

  const value = useMemo<Auth>(
    () => ({
      loading: oidc.isLoading,
      session: user && {
        accessToken: user.access_token,
        user: {
          sub: user.profile.sub,
          email: user.profile.email ?? "",
          name:
            user.profile.name ??
            user.profile.email?.split("@")[0] ??
            "Homie user",
        },
      },
      error: oidc.error
        ? "Couldn't sign you in. Check your connection and try again."
        : null,
      signIn: async () => {
        await oidc.signinRedirect()
      },
      signOut: async () => {
        await queryClient.cancelQueries()
        queryClient.clear()
        await oidc.removeUser()
        // Cognito's discovery has no end_session_endpoint, so end its session by hand.
        const logout = new URL("/logout", DOMAIN)
        logout.searchParams.set("client_id", CLIENT_ID)
        logout.searchParams.set("logout_uri", window.location.origin)
        window.location.assign(logout)
      },
    }),
    [oidc, user, queryClient],
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
