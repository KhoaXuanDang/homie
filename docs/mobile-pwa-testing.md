# Mobile and PWA testing

## Deployed test environments

- Frontend (Next.js HTTPS URL): **Not recorded yet — fill in when deployment is assigned.**
- API (AWS HTTPS URL): **Not recorded yet — fill in when deployment is assigned.**
- Deployment owner: **Not assigned.**

For final hosted acceptance, use the deployed Next.js frontend and AWS HTTPS API. A named Cloudflare Tunnel can also expose the local Docker stack over a stable HTTPS origin for phone testing before deployment; it terminates at local Caddy, which routes the frontend and API paths. Do not use a quick tunnel or commit a personal tunnel URL or token.

### Local phone-test tunnel setup

1. Copy `infra/.env.example` to `infra/.env`. Set the named tunnel token, stable HTTPS origin in `NEXT_PUBLIC_API_URL`, and Cognito values from Terraform. The hostname must match the public hostname configured for the named tunnel.
2. Configure the named tunnel's public hostname to forward to `http://caddy:80` on the Docker network. Caddy routes `/api/*`, `/mcp`, and `/health` to FastAPI, and other requests to Next.js.
3. Add the exact HTTPS origin (without a trailing slash) to Terraform `dashboard_urls` using `infra/terraform/phone-test.tfvars.example` as a template, then apply Terraform. This enables Cognito callback/logout URLs and S3 CORS. Add it to server `CORS_ORIGINS` as well.
4. Start the stack with the `phone-test` Compose profile and `infra/.env`. Keep secrets in the local env file only. For local desktop development, keep the default API URL at `http://localhost:8000`.

The phone-test origin is deliberately a placeholder until a domain and tunnel are assigned. Do not apply the example tfvars unchanged. For hosted testing, set `NEXT_PUBLIC_API_URL` to the AWS API's HTTPS base URL instead, and allow the deployed frontend origin in Terraform `dashboard_urls`, server `CORS_ORIGINS`, and S3 CORS where browser uploads are enabled. Set server `PUBLIC_BASE_URL` to the AWS API origin when Google OAuth is configured.

## Device checks

Use Chrome as the primary browser on the team's available phones. Exact device, OS, or browser version inventory is not required. If an iPhone-specific install or sign-in issue occurs in Chrome, reproduce it in Safari.

1. Open the selected named-tunnel or deployed frontend URL over HTTPS and confirm it loads without certificate warnings.
2. Sign in through Cognito Hosted UI and verify the callback returns to `/memories`.
3. Refresh and verify the signed-in session remains available.
4. Install from the browser and launch from the home screen; verify the standalone shell and start URL.
5. Verify mobile tabs, safe-area spacing, keyboard access, and no page-level horizontal scrolling.
6. Exercise API-backed UI flows and confirm requests reach the local API through Caddy (tunnel run) or the AWS API (hosted run).
7. Trigger or simulate an API 401 and confirm the session-expired notice offers reauthentication.
8. Sign out and verify Cognito returns the browser to the allowed frontend origin.

## Results

Record the actual test origin, API target (local or AWS), deployment/tunnel owner, test date, pass/fail results, and blockers after device checks. Do not record credentials, access tokens, or private user data. No device acceptance results are claimed until these checks have actually been run.
