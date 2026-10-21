"use client"

import createClient from "openapi-fetch"
import { useMemo } from "react"

import { useAuth } from "@/lib/auth"
import { env } from "@/lib/env"

import type { paths } from "./schema"

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
    this.name = "ApiError"
  }
}

export function useApi() {
  const auth = useAuth()
  const token = auth.session?.accessToken

  return useMemo(() => {
    const client = createClient<paths>({
      baseUrl: env.apiUrl,
      async fetch(request) {
        try {
          return await fetch(request)
        } catch (error) {
          if (error instanceof TypeError) {
            throw new ApiError(
              0,
              "You're offline. Check your connection and try again.",
            )
          }
          throw error
        }
      },
    })

    client.use({
      onRequest({ request }) {
        if (!token || !auth.isSessionTokenActive(token)) {
          window.dispatchEvent(new Event("homie:session-expired"))
          throw new ApiError(401, "Sign in again to continue.")
        }
        request.headers.set("Authorization", `Bearer ${token}`)
        return request
      },
      onResponse({ response }) {
        if (response.status === 401) {
          if (token) auth.invalidateSession(token)
          window.dispatchEvent(new Event("homie:session-expired"))
        }
        return response
      },
    })

    return client
  }, [auth, token])
}
