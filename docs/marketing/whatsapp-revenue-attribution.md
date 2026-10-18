# WhatsApp Revenue Attribution — OpenExecutive / Control Tower

## Status

**Phase 1 implementation:** signed event ingestion, SQLite audit ledger, idempotent retries, aggregated reporting, and a human-verification gate for revenue. This does **not** yet send events to Meta or Google Ads and must not be described as a live end-to-end advertising integration until those provider adapters and live test diagnostics have passed.

## What the Tintim screenshots imply

The feature set breaks into five separate capabilities:

1. **Attribution capture:** preserve Google click IDs (gclid, gbraid, wbraid), Meta click IDs (fbclid, fbc, fbp, ctwa_clid), UTMs, and campaign/ad identifiers where available.
2. **Conversation linkage:** associate a WhatsApp conversation with the earlier website/ad touchpoint using a pseudonymous lead_key; the source system must provide the authorized WhatsApp/CRM webhook event.
3. **Sales-stage ledger:** record lead created, conversation started, evaluation booked/attended, sale candidate, confirmed sale, and rejected candidate as separate immutable events.
4. **Sales inspector:** AI may create a sale_candidate, but that event remains pending. Only a human-verified sale_confirmed with a unique transaction ID and value counts as revenue or becomes eligible for an ad-platform conversion.
5. **Ad-platform feedback:** deliver only validated conversion events to Meta Conversions API and Google Data Manager API, store every provider response, deduplicate retries, and reconcile provider diagnostics against the internal ledger.

## Routes added

- POST /marketing-attribution/webhook — accepts a signed, schema-validated event.
- GET /marketing-attribution/summary — returns aggregate counts and verified revenue by source; it never returns lead identifiers or conversation text.

In production, the OpenExecutive BACKEND_SHARED_SECRET gate must remain enabled. The webhook additionally requires ATTRIBUTION_WEBHOOK_SECRET and X-Attribution-Signature: sha256=<HMAC-SHA256 of the exact request body>. The HMAC secret must never be committed to GitHub.

## Event rules

- Use an opaque pseudonymous lead_key, not a raw telephone number.
- Send only allowlisted attribution fields. Do not send names, raw phone numbers, dental conditions, treatment details, or WhatsApp message transcripts to this ledger.
- Include a timezone in occurred_at.
- Use a stable event_id for retries. Reusing it with different content returns HTTP 409.
- Confirmed sales require verification_status=verified, an internal verified_by actor ID, unique transaction_id, and sale_value_brl.
- The same transaction_id cannot count as a second sale, even if a new event ID is used.
- AI classifications are suggestions only; sale_candidate is not a sale.

### Example: conversation created from a Google Ads click

    {
      "schema_version": "1",
      "event_id": "evt_conversation_000001",
      "event_type": "conversation_started",
      "occurred_at": "2026-10-18T12:00:00-03:00",
      "lead_key": "lead_hmac_7d83a93e8b14",
      "source": "google_ads",
      "channel": "whatsapp",
      "gclid": "example-click-id",
      "utm_source": "google",
      "utm_medium": "cpc",
      "utm_campaign": "dentista_parnamirim"
    }

This is illustrative data only. Production requests must be sent by the authorized site/WhatsApp/CRM integration over HTTPS and signed over the exact JSON body. Never use the example identifiers for real conversions.

## Proposed integration topology

    Site / Cloudflare Worker
      capture allowlisted click IDs + UTMs at first touch
        signed event → OpenExecutive attribution ledger

    Authorized WhatsApp Business Platform / approved inbox webhook
      conversation + referral metadata (when Meta provides it)
        signed event → same pseudonymous lead key

    Sales inspector (AI suggests) → human confirms/rejects sale
      immutable sales-stage event + unique transaction ID + amount
        verified conversion outbox (next phase)
          Meta Conversions API (test events + deduplication)
          Google Data Manager API (offline / enhanced lead conversions)

    Provider delivery attempts + diagnostics → audit trail / reconciliation report

Attribution is probabilistic when identifiers are missing. Direct/organic conversations, private browsing, changed prefilled messages, lost redirect parameters, and conversations begun before tracking was connected must be reported as unknown or unattributed, not guessed. Do not claim 100% coverage.

## Provider implementation gate — next phase

- **Meta:** implement a server-side adapter using the official Conversions API, an explicit event mapping, a stable event_id, test-event diagnostics, response logging, retry policy, and opt-out/legal-basis controls. Never assume a successful HTTP response means that Meta attributed the event.
- **Google Ads:** use Google Data Manager API for offline/enhanced conversions for leads. Starting 15 June 2026, legacy UploadClickConversion is restricted for projects without eligible prior upload history; new work must not depend on old tutorials or developer-token-only examples.
- Build an outbox with pending / delivered / retryable_error / permanent_error, capped retries, and a provider request ID. Never mark a conversion sent before the platform confirms acceptance.
- Validate in sandbox/test events first. Only after platform-side diagnostics confirm receipt, matching, deduplication, and the correct conversion action should production delivery be enabled.

## Recommended building blocks

- **Existing OpenExecutive backend:** event ledger, human review gate, summary, agent orchestration, and audit flow.
- **Existing Cloudflare site/Worker:** browser-side allowlisted attribution capture and signed server-to-server ingress; do not put provider access tokens in browser code.
- **Authorized WhatsApp Business Platform / approved inbox webhook:** conversation/referral metadata. Use official API/coexistence or the currently approved provider path; avoid making unofficial WhatsApp Web automation the system of record.
- **Chatwoot (optional, self-hostable):** inbox and workflow surface if the clinic wants an open-source interface for human review; not the attribution ledger itself.

## Current acceptance criteria

- [x] Event schema rejects free-form/extra fields and raw phone numbers as the lead key.
- [x] Inbound HMAC authentication with a fail-closed disabled state.
- [x] Idempotent duplicate delivery and 409 on event ID/content collision.
- [x] Unique transaction ID for each confirmed sale.
- [x] Revenue is counted only for explicitly verified sales.
- [x] Aggregate report shows events, leads, pending candidates, verified sales, and verified revenue by source.
- [ ] Live provider webhook tested against the clinic's real WhatsApp/inbox integration.
- [ ] Meta CAPI adapter implemented and validated with Test Events.
- [ ] Google Data Manager API adapter implemented and validated with diagnostics.
- [ ] Conversion outbox, retry policy, monitoring, and reconciliation completed.
- [ ] Only after all items above: enable production ad-platform delivery.
