"""Optional local HTTP adapter for a separately managed coordinator such as n8n.

No server starts on import. Deploy behind the user's chosen private network or
reverse proxy; this small adapter intentionally has no public-internet defaults.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from urllib.parse import urlparse

from tracking.store import TrackingStore
from .jobs import JobService


def make_handler(service: JobService, token: str):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def _authorized(self) -> bool:
            return self.headers.get("Authorization") == f"Bearer {token}"

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized(): return self._json(401, {"error": "Unauthorized"})
            path = urlparse(self.path).path.strip("/").split("/")
            try:
                if path == ["jobs"]: return self._json(200, {"jobs": list(service.store.state["jobs"].values())})
                if len(path) == 2 and path[0] == "jobs": return self._json(200, service.get(path[1]))
                if len(path) == 2 and path[0] == "leads": return self._json(200, service.store.state["leads"][path[1]])
                if len(path) == 3 and path[0] == "projects" and path[2] == "snapshot": return self._json(200, service.store.snapshot(path[1]))
                return self._json(404, {"error": "Not found"})
            except (KeyError, ValueError) as error:
                return self._json(404, {"error": str(error)})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized(): return self._json(401, {"error": "Unauthorized"})
            path = urlparse(self.path).path.strip("/").split("/")
            try:
                payload = self._body()
                if path == ["jobs"]:
                    job = service.create(payload.pop("job_type"), **payload)
                    return self._json(201, job)
                # Running needs a registered application worker. The HTTP adapter has none by design.
                if len(path) == 3 and path[0] == "jobs" and path[2] == "run":
                    return self._json(409, {"error": "No worker is registered by the HTTP adapter. Run an approved worker process."})
                return self._json(404, {"error": "Not found"})
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                return self._json(400, {"error": str(error)})

        def log_message(self, _format: str, *_args) -> None:
            """Avoid logging headers or request bodies, which could contain credentials."""

    return Handler


def serve(*, state_path: str = "data/tracking/state.json", host: str = "127.0.0.1", port: int = 8787) -> None:
    token = os.environ.get("AI_WEBSITE_AGENT_API_TOKEN")
    if not token:
        raise RuntimeError("AI_WEBSITE_AGENT_API_TOKEN is required; server not started.")
    service = JobService(TrackingStore(state_path))
    ThreadingHTTPServer((host, port), make_handler(service, token)).serve_forever()
