"use client"

import { useCallback } from "react"

import { useAuth } from "@/lib/auth"

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
  }
}

function messageFor(status: number) {
  if (status === 401) return "Your session expired. Sign in again."
  if (status === 404) return "That item no longer exists."
  if (status === 429) return "You're going a bit fast. Try again in a minute."
  if (status >= 500) return "Homie is having trouble right now. Try again."
  return "Something went wrong. Check your input and try again."
}

/**
 * Authenticated JSON fetch scoped to the signed-in user. Query keys should
 * start with `sub` so cached data never leaks across accounts.
 */
export function useApi() {
  const { session } = useAuth()
  const token = session?.accessToken
  const sub = session?.user.sub

  const request = useCallback(
    async <T>(path: string, init: RequestInit & { json?: unknown } = {}) => {
      const { json, headers, ...rest } = init
      let res: Response
      try {
        res = await fetch(`${API_URL}${path}`, {
          ...rest,
          headers: {
            ...(json !== undefined && { "Content-Type": "application/json" }),
            ...(token && { Authorization: `Bearer ${token}` }),
            ...headers,
          },
          body: json !== undefined ? JSON.stringify(json) : rest.body,
        })
      } catch {
        throw new ApiError(
          0,
          navigator.onLine
            ? "Couldn't reach Homie. Try again."
            : "You're offline. Reconnect and try again.",
        )
      }
      if (!res.ok) throw new ApiError(res.status, messageFor(res.status))
      return (res.status === 204 ? undefined : await res.json()) as T
    },
    [token],
  )

  return { request, sub, enabled: Boolean(sub) }
}
