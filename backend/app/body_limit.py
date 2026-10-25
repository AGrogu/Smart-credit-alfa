"""Bound request bytes before multipart parsing, including chunked transfers."""

from uuid import uuid4
from starlette.responses import JSONResponse


class BoundedBodyMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        upload = scope["path"].endswith("/documents")
        upload_megabytes = 10 if scope["path"].startswith(("/api/v2/", "/api/v1/")) else 5
        limit = upload_megabytes * 1024 * 1024 + 65536 if upload else 128 * 1024
        detail = f"Максимальный размер файла — {upload_megabytes} МиБ" if upload else "Тело запроса слишком велико"
        headers = dict(scope.get("headers", []))
        request_id = str(uuid4())

        async def reject(code, message):
            await JSONResponse({"detail": message}, status_code=code, headers={"X-Request-ID": request_id, "Cache-Control": "no-store"})(
                scope, receive, send
            )

        try:
            if int(headers.get(b"content-length", b"0")) > limit:
                return await reject(413, detail)
        except ValueError:
            return await reject(400, "Некорректный Content-Length")
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > limit:
                return await reject(413, detail)
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        pending = True

        async def buffered_receive():
            nonlocal pending
            if pending:
                pending = False
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        return await self.app(scope, buffered_receive, send)
