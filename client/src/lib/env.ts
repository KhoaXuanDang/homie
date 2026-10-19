// NEXT_PUBLIC_* must be referenced literally so Next can inline them at build time.
export const env = {
  apiUrl: process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000",
  cognitoAuthority: process.env.NEXT_PUBLIC_COGNITO_AUTHORITY ?? "",
  cognitoClientId: process.env.NEXT_PUBLIC_COGNITO_CLIENT_ID ?? "",
  cognitoDomain: process.env.NEXT_PUBLIC_COGNITO_DOMAIN ?? "",
  cognitoScope:
    process.env.NEXT_PUBLIC_COGNITO_SCOPE ??
    "openid email profile homie/mcp:tools",
}
