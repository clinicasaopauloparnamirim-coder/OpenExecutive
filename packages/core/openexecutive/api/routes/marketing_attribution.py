"""Authenticated ingress and summary endpoints for the marketing attribution ledger."""
from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ValidationError

from openexecutive.attribution import store
from openexecutive.attribution.models import MarketingAttributionEvent

router = APIRouter(prefix="/marketing-attribution", tags=["marketing-attribution"])
_MAX_BODY_BYTES = 64 * 1024


class IngestResponse(BaseModel):
    status: str = "accepted"
    event_id: str
    duplicate: bool
    sale_recorded: bool
    delivery_status: str = "not_configured"


def _valid_signature(raw_body: bytes, secret: str, provided: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided.strip())


def _assert_principal_operator(request: Request) -> None:
    """Require the app's existing proof-of-principal gate for revenue confirmation."""
    from openexecutive.api.routes.decisions import _provably_principal

    if not _provably_principal(request):
        raise HTTPException(
            status_code=403,
            detail="Only the authenticated clinic principal may confirm attributed revenue",
        )


@router.post("/webhook", response_model=IngestResponse)
async def ingest_event(
    request: Request,
    x_attribution_signature: str | None = Header(default=None, alias="X-Attribution-Signature"),
) -> IngestResponse:
    """Receive an event signed with HMAC-SHA256; unsigned payloads are not trusted."""
    secret = os.environ.get("ATTRIBUTION_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="Marketing attribution webhook is disabled until ATTRIBUTION_WEBHOOK_SECRET is configured")

    raw_body = await request.body()
    if len(raw_body) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")
    if not x_attribution_signature or not _valid_signature(raw_body, secret, x_attribution_signature):
        raise HTTPException(status_code=401, detail="Invalid attribution signature")

    try:
        event = MarketingAttributionEvent.model_validate_json(raw_body)
    except ValidationError as exc:
        # Do not echo submitted values back into logs or HTTP responses.
        raise HTTPException(status_code=422, detail="Invalid marketing attribution event schema") from exc

    if event.event_type == "sale_confirmed":
        raise HTTPException(
            status_code=403,
            detail="External webhooks cannot confirm revenue; use the authenticated sales confirmation route",
        )

    try:
        write = store.record_event(event)
    except store.EventConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Unable to persist attribution event") from exc

    return IngestResponse(
        event_id=write["event_id"],
        duplicate=write["duplicate"],
        sale_recorded=False,
    )


@router.get("/summary")
async def attribution_summary() -> dict[str, Any]:
    """Aggregate counts only; no contact identifiers or conversation text are exposed."""
    try:
        return store.get_summary()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Unable to read attribution summary") from exc


@router.post("/sales/confirm", response_model=IngestResponse)
async def confirm_sale(event: MarketingAttributionEvent, request: Request) -> IngestResponse:
    """Record a sale only from the app's authenticated principal/operator workflow."""
    _assert_principal_operator(request)
    if event.event_type != "sale_confirmed":
        raise HTTPException(status_code=422, detail="This route accepts only sale_confirmed events")

    try:
        write = store.record_event(event)
    except store.EventConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Unable to persist confirmed sale") from exc

    return IngestResponse(
        event_id=write["event_id"],
        duplicate=write["duplicate"],
        sale_recorded=True,
    )
