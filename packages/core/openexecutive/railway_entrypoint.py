from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict

import uvicorn
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def _b64url_decode(value: str) -> bytes:
    normalized = value.strip()
    return base64.urlsafe_b64decode(normalized + "=" * ((4 - len(normalized) % 4) % 4))


class SignedOpenExecutiveMcpGate:
    """Keep liveness responsive while the upstream API initializes; gate MCP until ready."""

    MAX_BODY = 512 * 1024
    MAX_NONCES = 10_000

    def __init__(self):
        self.app = None
        self.shared_secret = os.environ.get("BACKEND_SHARED_SECRET", "")
        encoded_key = os.environ.get("OPENEXECUTIVE_CALLER_PUBLIC_KEY", "")
        self.public_key = None
        if encoded_key:
            raw_key = _b64url_decode(encoded_key)
            if len(raw_key) != 32:
                raise RuntimeError("OPENEXECUTIVE_CALLER_PUBLIC_KEY must encode 32 Ed25519 public-key bytes")
            self.public_key = Ed25519PublicKey.from_public_bytes(raw_key)

        if not self.shared_secret or self.public_key is None:
            logging.getLogger("openexecutive").error(
                "Signed MCP gateway is not configured; /mcp will return 503 until "
                "BACKEND_SHARED_SECRET and OPENEXECUTIVE_CALLER_PUBLIC_KEY are set"
            )

        self.nonces = OrderedDict()
        self.nonce_lock = threading.Lock()
        self.api_ready = asyncio.Event()
        self.startup_failed = False
        self.lifespan_task = None

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
        body = json.dumps({"ok": False, "error": message}, separators=(",", ":")).encode("utf-8")
        await self.respond(send, status, body)

    def _prepare_upstream_app(self):
        # Import the heavy app only after the outer ASGI listener is already live.
        # Defer the three expensive knowledge-index seed operations off the startup path.
        from openexecutive.knowledge import loader, skills_index

        seed_builtin = loader.seed_builtin_knowledge
        seed_skills = skills_index.seed_builtin_skills
        seed_failures = loader.seed_failures
        seed_state = {"started": False}

        async def defer_seed(store=None, **kwargs):
            if not seed_state["started"]:
                seed_state["started"] = True

                async def seed_all():
                    await seed_builtin(store=store)
                    await seed_skills(store=store)
                    await seed_failures(store=store)

                async def warm_index():
                    try:
                        await asyncio.to_thread(lambda: asyncio.run(seed_all()))
                        logging.getLogger("openexecutive").info("Built-in knowledge index ready")
                    except Exception:
                        logging.getLogger("openexecutive").exception("Background knowledge indexing failed")

                asyncio.create_task(warm_index())
            return 0

        loader.seed_builtin_knowledge = defer_seed
        loader.seed_failures = defer_seed
        skills_index.seed_builtin_skills = defer_seed

        from openexecutive.api.main import app as upstream_app
        return upstream_app

    async def _start_upstream(self):
        try:
            self.app = await asyncio.to_thread(self._prepare_upstream_app)
            logging.getLogger("openexecutive").info("Upstream FastAPI module imported; starting app lifespan")
            async with self.app.router.lifespan_context(self.app):
                self.api_ready.set()
                logging.getLogger("openexecutive").info("OpenExecutive upstream API is ready")
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise
        except BaseException:
            self.startup_failed = True
            logging.getLogger("openexecutive").exception("OpenExecutive upstream API failed during startup")

    async def _handle_lifespan(self, receive, send):
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                if self.lifespan_task is None:
                    self.lifespan_task = asyncio.create_task(
                        self._start_upstream(), name="openexecutive-upstream-lifespan"
                    )
                # Start listening immediately; /mcp remains closed until api_ready is set.
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                if self.lifespan_task is not None and not self.lifespan_task.done():
                    self.lifespan_task.cancel()
                    try:
                        await self.lifespan_task
                    except asyncio.CancelledError:
                        pass
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def __call__(self, scope, receive, send):
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            await self._handle_lifespan(receive, send)
            return
        if scope_type != "http":
            if self.app is not None and self.api_ready.is_set():
                await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()

        if path == "/health" and method in ("GET", "HEAD"):
            state = "ready" if self.api_ready.is_set() else ("failed" if self.startup_failed else "starting")
            body = json.dumps({"status": state}, separators=(",", ":")).encode("utf-8")
            await self.respond(send, 200, b"" if method == "HEAD" else body)
            return

        if path not in ("/mcp", "/mcp/"):
            await self.fail(send, 404, "not_found")
            return
        if method != "POST" or scope.get("query_string"):
            await self.fail(send, 405, "method_not_allowed")
            return
        if self.startup_failed:
            await self.fail(send, 503, "service_startup_failed")
            return
        if not self.api_ready.is_set() or self.app is None:
            await self.fail(send, 503, "service_not_ready")
            return
        if not self.shared_secret or self.public_key is None:
            await self.fail(send, 503, "signed_gateway_not_configured")
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
        canonical = "\n".join([method, path, timestamp_text, nonce, body_digest]).encode("utf-8")
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

        # Inject the backend shared key only after signature verification.
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


if __name__ == "__main__":
    uvicorn.run(
        SignedOpenExecutiveMcpGate(),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        log_level="info",
    )
