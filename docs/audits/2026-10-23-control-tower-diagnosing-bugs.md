# Control Tower diagnostic — 2026-10-23

## Scope and safety

- Method: Matt Pocock `diagnosing-bugs` + GAUNTLET LOOP + Karpathy-style minimal changes + ASD-STE100.
- Inspection mode: read-only against Cloudflare Worker metadata, settings, deployment records, and source.
- No Worker, secret, OAuth credential, DNS, or Google Ads campaign was changed.
- No secret value or authentication header was read or recorded.

## Confirmed facts

1. Cloudflare API authentication succeeded for the connected account.
2. Three Worker scripts are present: `clinica-sao-paulo-site`, `jev-control-tower`, and `weathered-tree-2839`.
3. `jev-control-tower` source is the default Cloudflare “Hello World” Worker. It returns `Hello World!`; it does not implement the Control Tower MCP handler. Its latest listed deployment was 2026-10-03.
4. `clinica-sao-paulo-site` source contains `/health`, `/control/health`, and `/mcp/tower` handlers. The MCP handler requires authorization. Its latest listed deployment was 2026-10-23T01:32:49Z.
5. `weathered-tree-2839` is the Worker attached to the custom domains `clinicasaopauloparnamirim.com.br` and `www.clinicasaopauloparnamirim.com.br`. Its source also contains MCP and Google OAuth logic.
6. The relevant Workers expose bindings for `MCP_AUTH_TOKEN`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `GOOGLE_OAUTH_STORE` where expected. Secret values are not readable through the inspected settings and were not requested.
7. The `jev-control-tower` script settings show empty plain-text bindings named `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `GOOGLE_REFRESH_TOKEN`. Because its source is only the Hello World template, these bindings do not make it a working Control Tower.

## Ranked findings

### P1 — Worker name/endpoint mismatch risk
**Evidence:** `jev-control-tower` is a Hello World placeholder, not an MCP server.

**Impact:** Any client configured to use this Worker as the Control Tower will not receive MCP tools.

**Safe action:** Keep this Worker on STANDBY. Do not overwrite it until every client reference has been identified and the canonical endpoint has passed an authenticated end-to-end test.

### P1 — Live MCP endpoint not yet proven end-to-end
**Evidence:** Source contains an authenticated `/mcp/tower` route and a `tools/list` response path. Configuration includes an `MCP_AUTH_TOKEN` secret binding.

**Impact:** Source and binding metadata do not prove that the public endpoint accepts the configured client token or that a tool call succeeds.

**Test status:** BLOCKED. The available web fetch could not access the Workers endpoint, and the connected Firecrawl account had no credits. No authenticated HTTP request was executed. Do not mark MCP as verified.

### P1 — Google OAuth refresh remains unverified
**Evidence:** The current Worker code obtains access tokens through the `GOOGLE_OAUTH_STORE` Durable Object. Historical notes recorded a failed/revoked/missing Google Ads refresh token. Current settings confirm the store binding exists, but do not expose its stored OAuth state.

**Impact:** Google Ads access may still fail even when the Worker itself is deployed.

**Test status:** BLOCKED pending a safe authenticated OAuth health check or redacted error response. Do not replace credentials or change campaigns as part of this diagnostic.

## Required next test loop

1. From the authorized runtime, call `GET /health`; expect HTTP 200 and `{"ok":true}`.
2. Call `GET /mcp/tower` without credentials; expect HTTP 401.
3. Call `GET /mcp/tower` with the already-configured token supplied through a secret environment variable; expect HTTP 200 and the Control Tower tool list. Never print the token.
4. Send JSON-RPC `initialize`, then `tools/list`; expect valid JSON-RPC responses.
5. Run one harmless read-only tool call and verify the returned data against its source.
6. Run a read-only Google Ads OAuth health check. Record only status code and a redacted error category.
7. If a check fails, create the smallest reproducible test, rank 3–5 falsifiable hypotheses, change one variable at a time, and repeat the exact test after any correction.

## Decision

- Keep `jev-control-tower` on STANDBY.
- Do not change production Worker code, DNS, secrets, OAuth state, or Google Ads campaigns until the canonical endpoint and failure are proven.
- Continue with independent read-only checks. Record any blocked dependency and do not infer success from configuration alone.
