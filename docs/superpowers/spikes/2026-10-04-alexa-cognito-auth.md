# Spike: Alexa & Cognito Account Linking and Scopes

## Overview
This spike investigates and validates the account-linking integration between Amazon Alexa and our AWS Cognito user pool (`homie-dev`), confirming support for custom scopes, resource server identifiers, and token exchange flows.

## Key Findings & Validation

### 1. Cognito Support for the `resource` Parameter
* **Tested via:** AWS Cognito OAuth2 Token Endpoint (`/oauth2/token`) using the `client_credentials` grant type.
* **Result:** **Pass.** Cognito successfully accepted the `resource` parameter and returned a fully signed JWT access token containing the correct scopes.

### 2. Custom Scopes & Resource Server Configuration
* **Resource Server Identifier:** `homie`
* **Configured Scopes:**
  * `mcp:service` *(Service-level access for Alexa)*
  * `mcp:tools` *(Use homie tools on behalf of the user)*
* **Cognito Custom Scope Format:** `<identifier>/<scope-name>`
  * `MCP_SERVICE_SCOPE`: `homie/mcp:service`
  * `MCP_TOOLS_SCOPE`: `homie/mcp:tools`

### 3. Alexa Account-Linking Redirect URIs
The following redirect URIs were provisioned via the Alexa Developer Console for Skill `MV93T6QN8NJTU`:
* `https://pitangui.amazon.com/api/skill/link/MV93T6QN8NJTU`
* `https://layla.amazon.com/api/skill/link/MV93T6QN8NJTU`
* `https://alexa.amazon.co.jp/api/skill/link/MV93T6QN8NJTU`

---

## Decision: Go / No-Go
* **Decision:** **GO (Cognito-Only)**
* **Rationale:** Because Cognito natively supports custom resource server prefixes, full custom scopes, and correctly processes the `resource` parameter during token generation, **no fallback token endpoint is required**. We can proceed directly with Cognito-backed authentication for subsequent issues.

## Next Steps
* Unblock and proceed with [#13 (Verify Cognito JWTs)](https://github.com/dntrAnh/homie/issues/48) and [#14 (OAuth authorization-server metadata endpoint)](https://github.com/dntrAnh/homie/issues/49).