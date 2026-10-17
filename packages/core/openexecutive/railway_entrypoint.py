from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
import threading
import time
from collections import OrderedDict

import uvicorn
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

print("OE_BOOT: entrypoint started", flush=True)

# The upstream lifespan seeds Chroma synchronously before it begins serving.
# Defer only those three knowledge-index jobs to a worker thread so /health
# remains responsive during the first boot. All database and app migrations
# in the upstream lifespan continue to run normally.
from openexecutive.knowledge import loader, skills_index

_seed_builtin = loader.seed_builtin_knowledge
_seed_skills = skills_index.seed_builtin_skills
_seed_failures = loader.seed_failures
_seed_task = None

async def _defer_seed(store=None, **kwargs):
    global _seed_task
    if _seed_task is None:
        async def _seed_all():
            await _seed_builtin(store=store)
            await _seed_skills(store=store)
            await _seed_failures(store=store)
        async def _warm_index():
            try:
                await asyncio.to_thread(lambda: asyncio.run(_seed_all()))
                logging.getLogger("openexecutive").info("Built-in knowledge index ready")
            except Exception:
                logging.getLogger("openexecutive").exception("Background knowledge indexing failed")
        _seed_task = asyncio.create_task(_warm_index())
    return 0

loader.seed_builtin_knowledge = _defer_seed
loader.seed_failures = _defer_seed
skills_index.seed_builtin_skills = _defer_seed

def _b64url_decode(value: str) -> bytes:
    normalized = value.strip()
    return base64.urlsafe_b64decode(normalized + "=" * ((4 - len(normalized) % 4) % 4))

class SignedOpenExecutiveMcpGate:
    """Permit public health only; require a signed Control Tower request for MCP."""

    MAX_BODY = 512 * 1024
    MAX_NONCES = 10_000

    def __init__(self, app):
        self.app = app
        self.shared_secret = os.environ.get("BACKEND_SHARED_SECRET", "")
        encoded_key = os.environ.get("OPENEXECUTIVE_CALLER_PUBLIC_KEY", "")
        self.public_key = None
        if encoded_key:
            raw_key = _b64url_decode(encoded_key)
            if len(raw_key) != 32:
                raise RuntimeError("OPENEXECUTIVE_CALLER_PUBLIC_KEY must encode 32 Ed25519 public-key bytes")
            self.public_key = Ed25519PublicKey.from_public_bytes(raw_key)
        if not self.shared_secret:
            raise RuntimeError("BACKEND_SHARED_SECRET is required for the signed public MCP gateway")
        if self.public_key is None:
            raise RuntimeError("OPENEXECUTIVE_CALLER_PUBLIC_KEY is required for the signed public MCP gateway")
        self.nonces = OrderedDict()
        self.nonce_lock = threading.Lock()

    async def respond(self, send, status, body, content_type=b"application/json"):
        headers = [
            (b"content-type", content_type),
            (b"cache-control", b"no-store"),
            (b"x-content-type-options", b"nosniff"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def fail(self, send, status, message):
        await self.respond(send, status, ('{"ok":false,"error":"' + message + '"}').encode("utf-8"))

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        if path == "/health" and method in ("GET", "HEAD"):
            await self.respond(send, 200, b"" if method == "HEAD" else b'{"status":"ok"}')
            return

        if path not in ("/mcp", "/mcp/"):
            await self.fail(send, 404, "not_found")
            return
        if method != "POST" or scope.get("query_string"):
            await self.fail(send, 405, "method_not_allowed")
            return

        request_headers = {}
        for key, value in scope.get("headers", []):
            request_headers.setdefault(key.lower(), []).append(value.decode("latin-1"))

        required = (
            b"x-openexecutive-timestamp",
            b"x-openexecutive-nonce",
            b"x-openexecutive-signature",
        )
        if any(len(request_headers.get(header, [])) != 1 for header in required):
            await self.fail(send, 401, "unauthorized")
            return

        timestamp_text = request_headers[b"x-openexecutive-timestamp"][0]
        nonce = request_headers[b"x-openexecutive-nonce"][0]
        signature_text = request_headers[b"x-openexecutive-signature"][0]
        if not timestamp_text.isdigit() or not re.fullmatch(r"[0-9a-fA-F-]{36}", nonce):
            await self.fail(send, 401, "unauthorized")
            return
        now = int(time.time())
        timestamp = int(timestamp_text)
        if abs(now - timestamp) > 60:
            await self.fail(send, 401, "expired_signature")
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                await self.fail(send, 400, "incomplete_request")
                return
            if message["type"] != "http.request":
                continue
            body.extend(message.get("body", b""))
            if len(body) > self.MAX_BODY:
                await self.fail(send, 413, "payload_too_large")
                return
            if not message.get("more_body", False):
                break

        body_digest = hashlib.sha256(bytes(body)).hexdigest()
        canonical = chr(10).join([method, path, timestamp_text, nonce, body_digest]).encode("utf-8")
        try:
            signature = _b64url_decode(signature_text)
            if len(signature) != 64:
                raise InvalidSignature
            self.public_key.verify(signature, canonical)
        except Exception:
            await self.fail(send, 401, "unauthorized")
            return

        with self.nonce_lock:
            for old_nonce, old_timestamp in list(self.nonces.items()):
                if old_timestamp < now - 120:
                    self.nonces.pop(old_nonce, None)
            if nonce in self.nonces:
                await self.fail(send, 409, "replayed_request")
                return
            self.nonces[nonce] = timestamp
            while len(self.nonces) > self.MAX_NONCES:
                self.nonces.popitem(last=False)

        # The service-side shared key is injected only after signature validation.
        scope = dict(scope)
        blocked = {
            b"x-api-key", b"authorization", b"x-openexecutive-timestamp",
            b"x-openexecutive-nonce", b"x-openexecutive-signature",
        }
        clean_headers = [(key, value) for key, value in scope.get("headers", []) if key.lower() not in blocked]
        clean_headers.append((b"x-api-key", self.shared_secret.encode("utf-8")))
        scope["headers"] = clean_headers
        body_sent = False

        async def replay_receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)

print("OE_BOOT: importing upstream FastAPI app", flush=True)
from openexecutive.api.main import app as openexecutive_app
print("OE_BOOT: upstream FastAPI app imported", flush=True)

if __name__ == "__main__":
    uvicorn.run(
        SignedOpenExecutiveMcpGate(openexecutive_app),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        log_level="info",
    )
