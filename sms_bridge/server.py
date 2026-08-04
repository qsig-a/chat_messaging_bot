"""SignalWire webhook endpoints.

Handlers return within milliseconds - SignalWire retries on slow responses - so
inbound messages are enqueued and the slow chat work happens in the worker.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, Request, Response

from .config import Config
from .delivery import Delivery, InboundSms
from .media import MediaTokens
from .signalwire import SignalWire
from .store import Store

log = logging.getLogger("bridge.server")


def create_app(
    config: Config,
    signalwire: SignalWire,
    store: Store,
    delivery: Delivery,
    queue: asyncio.Queue,
    adapter,
    media: MediaTokens,
) -> FastAPI:
    api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    async def check(request: Request, path: str) -> dict[str, str] | None:
        form = await request.form()
        params = {k: str(v) for k, v in form.items()}
        if config.verify_signature:
            signature = request.headers.get("X-Twilio-Signature", "")
            # SignalWire.check reads the header itself; `signature` is pulled out
            # separately here only so explain_bad_signature can report on it.
            if not signalwire.check(request, path, params):
                log.warning(
                    "rejected request with bad signature on %s: %s",
                    path,
                    signalwire.explain_bad_signature(request, path, params, signature),
                )
                return None
        return params

    @api.post("/sms/inbound")
    async def inbound(request: Request) -> Response:
        params = await check(request, "/sms/inbound")
        if params is None:
            return Response(status_code=403)

        sid = params.get("MessageSid", "")
        if store.already_seen(sid):
            log.info("duplicate webhook for %s, ignoring", sid)
            return Response(content="<Response/>", media_type="application/xml")

        media = []
        for i in range(int(params.get("NumMedia", "0") or 0)):
            url = params.get(f"MediaUrl{i}")
            if url:
                media.append(url)

        await queue.put(
            InboundSms(
                sender=params.get("From", ""),
                body=(params.get("Body") or "").strip(),
                media_urls=tuple(media),
                sid=sid,
            )
        )
        return Response(content="<Response/>", media_type="application/xml")

    @api.post("/sms/status")
    async def status(request: Request) -> Response:
        params = await check(request, "/sms/status")
        if params is None:
            return Response(status_code=403)
        asyncio.create_task(
            delivery.update_status(
                params.get("MessageSid", ""),
                (params.get("MessageStatus") or "").lower(),
                params.get("ErrorCode", ""),
            )
        )
        return Response(status_code=204)

    @api.get("/healthz")
    async def healthz() -> dict:
        return {
            "ok": adapter.is_ready(),
            "platform": adapter.name,
            "queued": queue.qsize(),
            "latency_ms": adapter.latency_ms(),
        }

    @api.get("/media/{token}")
    async def outbound_media(token: str) -> Response:
        """Serve a chat attachment to SignalWire for MMS.

        Guarded solely by the token's HMAC and expiry. Every failure returns 404
        so the route never discloses which file ids exist.
        """
        file_id = media.verify(token)
        if file_id is None:
            log.warning("rejected media request with an invalid or expired token")
            return Response(status_code=404)
        try:
            data, content_type = await adapter.fetch_attachment(file_id)
        except Exception:  # noqa: BLE001
            log.exception("could not fetch attachment %s", file_id)
            return Response(status_code=404)
        return Response(
            content=data,
            media_type=content_type,
            headers={"Cache-Control": "no-store"},
        )

    return api
