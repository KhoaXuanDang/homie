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
    const client = createClient<paths>({ baseUrl: env.apiUrl })

    client.use({
      onRequest({ request }) {
        if (!token) {
          window.dispatchEvent(new Event("homie:session-expired"))
          throw new ApiError(401, "Sign in again to continue.")
        }
        request.headers.set("Authorization", `Bearer ${token}`)
        return request
      },
      onResponse({ response }) {
        if (response.status === 401) {
          window.dispatchEvent(new Event("homie:session-expired"))
        }
        return response
      },
    })

    return client
  }, [token])
}
