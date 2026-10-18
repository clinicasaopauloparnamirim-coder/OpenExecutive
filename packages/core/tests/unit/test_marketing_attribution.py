"""Security, idempotency and verified-revenue tests for marketing attribution."""
from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from openexecutive.api.routes import marketing_attribution as route
from openexecutive.attribution import store


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "attribution.db")
    monkeypatch.setenv("ATTRIBUTION_WEBHOOK_SECRET", "unit-test-secret-not-for-production")
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


def _payload(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": "1",
        "event_id": "evt_lead_000001",
        "event_type": "conversation_started",
        "occurred_at": "2026-10-18T12:00:00-03:00",
        "lead_key": "lead_hmac_7d83a93e8b14",
        "source": "google_ads",
        "channel": "whatsapp",
        "gclid": "test-gclid",
        "utm_campaign": "dentista_parnamirim",
    }
    body.update(overrides)
    return body


def _signed_headers(
    raw: bytes, secret: str = "unit-test-secret-not-for-production"
) -> dict[str, str]:
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return {"X-Attribution-Signature": f"sha256={digest}"}


def _confirmed_sale(**overrides: Any) -> dict[str, Any]:
    return _payload(
        event_id="evt_sale_000001",
        event_type="sale_confirmed",
        transaction_id="sale_000001",
        sale_value_brl="349.90",
        verification_status="verified",
        verified_by="operator_01",
        evidence_ref="conversation_ref_01",
        **overrides,
    )


def test_webhook_records_lead_and_summary_is_aggregated_only(client: TestClient) -> None:
    raw = json.dumps(_payload(), separators=(",", ":")).encode()
    response = client.post(
        "/marketing-attribution/webhook", content=raw, headers=_signed_headers(raw)
    )
    assert response.status_code == 200, response.text
    assert response.json()["sale_recorded"] is False

    summary = client.get("/marketing-attribution/summary").json()
    assert summary["event_count"] == 1
    assert summary["unique_leads"] == 1
    assert summary["verified_sales"] == 0
    assert summary["verified_revenue_brl"] == 0.0
    assert summary["by_source"][0]["source"] == "google_ads"
    assert "lead_hmac_7d83a93e8b14" not in response.text


def test_exact_retry_is_idempotent(client: TestClient) -> None:
    raw = json.dumps(_payload(), separators=(",", ":")).encode()
    headers = _signed_headers(raw)
    assert client.post(
        "/marketing-attribution/webhook", content=raw, headers=headers
    ).status_code == 200
    second = client.post("/marketing-attribution/webhook", content=raw, headers=headers)
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert client.get("/marketing-attribution/summary").json()["event_count"] == 1


def test_reused_event_id_with_different_payload_is_conflict(client: TestClient) -> None:
    first = json.dumps(_payload(), separators=(",", ":")).encode()
    changed = json.dumps(_payload(source="meta_ads"), separators=(",", ":")).encode()
    assert client.post(
        "/marketing-attribution/webhook",
        content=first,
        headers=_signed_headers(first),
    ).status_code == 200
    response = client.post(
        "/marketing-attribution/webhook",
        content=changed,
        headers=_signed_headers(changed),
    )
    assert response.status_code == 409


def test_unverified_sale_cannot_be_counted(client: TestClient) -> None:
    raw = json.dumps(
        _payload(
            event_type="sale_confirmed",
            transaction_id="sale_000001",
            sale_value_brl="349.90",
        ),
        separators=(",", ":"),
    ).encode()
    response = client.post(
        "/marketing-attribution/webhook", content=raw, headers=_signed_headers(raw)
    )
    assert response.status_code == 422
    assert client.get("/marketing-attribution/summary").json()["verified_revenue_brl"] == 0.0


def test_external_webhook_cannot_confirm_even_a_verified_sale(client: TestClient) -> None:
    raw = json.dumps(_confirmed_sale(), separators=(",", ":")).encode()
    response = client.post(
        "/marketing-attribution/webhook", content=raw, headers=_signed_headers(raw)
    )
    assert response.status_code == 403
    assert client.get("/marketing-attribution/summary").json()["verified_revenue_brl"] == 0.0


def test_verified_sale_counts_once_and_deduplicates_transaction(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The app's real principal-proof helper is covered by the application's
    # authentication tests. Here we test this route's ledger workflow in isolation.
    monkeypatch.setattr(route, "_assert_principal_operator", lambda _request: None)
    event = _confirmed_sale()
    response = client.post("/marketing-attribution/sales/confirm", json=event)
    assert response.status_code == 200, response.text
    assert response.json()["sale_recorded"] is True
    summary = client.get("/marketing-attribution/summary").json()
    assert summary["verified_sales"] == 1
    assert summary["verified_revenue_brl"] == 349.90

    second = dict(event, event_id="evt_sale_000002")
    response_second = client.post("/marketing-attribution/sales/confirm", json=second)
    assert response_second.status_code == 409
    assert client.get("/marketing-attribution/summary").json()["verified_revenue_brl"] == 349.90


def test_rejects_missing_signature_and_disabled_webhook(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = json.dumps(_payload(), separators=(",", ":")).encode()
    assert client.post("/marketing-attribution/webhook", content=raw).status_code == 401
    monkeypatch.delenv("ATTRIBUTION_WEBHOOK_SECRET")
    assert client.post(
        "/marketing-attribution/webhook", content=raw, headers=_signed_headers(raw)
    ).status_code == 503


def test_rejects_raw_phone_as_lead_key_and_extra_conversation_text(client: TestClient) -> None:
    for payload in (
        _payload(lead_key="+5511991234567"),
        _payload(conversation_text="Olá, quero marcar uma consulta"),
    ):
        raw = json.dumps(payload, separators=(",", ":")).encode()
        response = client.post(
            "/marketing-attribution/webhook",
            content=raw,
            headers=_signed_headers(raw),
        )
        assert response.status_code == 422


def test_requires_timezone_for_event_time() -> None:
    from openexecutive.attribution.models import MarketingAttributionEvent

    with pytest.raises(ValidationError):
        MarketingAttributionEvent.model_validate(
            _payload(occurred_at="2026-10-18T12:00:00")
        )


def test_decimal_is_currency_precise() -> None:
    from openexecutive.attribution.models import MarketingAttributionEvent

    event = MarketingAttributionEvent.model_validate(
        _confirmed_sale(
            event_id="evt_sale_000003",
            transaction_id="sale_000003",
            sale_value_brl="123.45",
        )
    )
    assert event.sale_value_minor == 12345
    assert event.sale_value_brl == Decimal("123.45")


def test_confirmed_sale_requires_evidence_reference() -> None:
    from openexecutive.attribution.models import MarketingAttributionEvent

    with pytest.raises(ValidationError):
        MarketingAttributionEvent.model_validate(
            _confirmed_sale(evidence_ref=None)
        )
