"""A small fault-injecting reverse proxy, for live failure tests (Phase 7). It sits between the worker and a real provider
and, on command, answers with a failure instead of (or after) passing the request through.

    python scripts/fault_proxy.py --upstream https://api.vachana.ai --port 9101 --rewrite-downloads
    python scripts/fault_proxy.py --upstream https://api.groq.com --port 9102

Point the worker at it with GNANI_BASE_URL=http://127.0.0.1:9101 and LLM_BASE_URL=http://127.0.0.1:9102/openai/v1.
Everything else is passed through untouched (headers included, so the provider key still reaches the provider; it is
never logged). Faults are rules, replaced as a whole with PUT /__rules and consumed in order of arrival:

    [{"match": "/files", "method": "GET", "action": "status", "status": 503, "times": 2}]

match    substring of the request path (and query); "" matches everything
exact    true: the path (without its query) must equal `match` exactly
method   GET / POST / ...; omitted matches any
times    how many requests the rule applies to (default 1; -1 means forever)
action   status             answer at once with `status` (default 503), optional `headers` and `body`
         hang               wait `seconds`, then close the connection without answering (a read timeout)
         drop               close the connection at once (a reset)
         garbage            answer 200 with a body that is not JSON
         synthetic          answer 200 with `body` (JSON), never contacting the provider
         forward_then_fail  pass the request through (so the provider acts on it), throw its answer away and answer
                            with `status`: a success whose reply was lost

GET /__log shows what happened to each request, DELETE /__log clears it. --rewrite-downloads turns the transcript links in
Gnani's file lists into links back through this proxy (path /__dl), so those downloads can be broken too.
"""

from __future__ import annotations

import argparse
import json
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

NOT_FORWARDED = {"host", "connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding"}


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rules: list[dict[str, Any]] = []
        self.log: list[dict[str, Any]] = []

    def take_rule(self, method: str, target: str) -> dict[str, Any] | None:
        with self.lock:
            for rule in self.rules:
                if rule.get("times", 1) == 0:
                    continue
                if rule.get("method") not in (None, method):
                    continue
                needle = rule.get("match", "")
                if (target.split("?")[0] != needle) if rule.get("exact") else (needle not in target):
                    continue
                if rule.get("times", 1) > 0:
                    rule["times"] = rule.get("times", 1) - 1
                return dict(rule)
        return None

    def begin(self, method: str, target: str, action: str) -> dict[str, Any]:
        """Log the request the moment it arrives: a request that hangs for half a minute must already be counted when
        the scenario that caused it settles, not show up in the next scenario's log."""
        entry: dict[str, Any] = {
            "t": time.strftime("%H:%M:%S"),
            "method": method,
            "path": target.split("?")[0],
            "action": action,
            "status": "-",
        }
        with self.lock:
            self.log.append(entry)
        return entry

    @staticmethod
    def finish(entry: dict[str, Any], status: int | None) -> None:
        entry["status"] = status if status is not None else "-"
        print(f"{entry['t']} {entry['method']} {entry['path']} -> {entry['action']} {entry['status']}", flush=True)


def make_handler(upstream: str, rewrite_downloads: bool, state: State) -> type[BaseHTTPRequestHandler]:
    client = httpx.Client(base_url=upstream, timeout=60, follow_redirects=True)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: object) -> None:  # our own log line says more
            pass

        # --- helpers ---------------------------------------------------------------------------------------------

        def send_body(self, status: int, body: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status: int, data: Any, headers: dict[str, str] | None = None) -> None:
            self.send_body(status, json.dumps(data).encode(), "application/json", headers)

        def hang_up(self) -> None:
            try:
                self.request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.close_connection = True

        def forward(self, method: str, target: str, body: bytes) -> httpx.Response:
            headers = {k: v for k, v in self.headers.items() if k.lower() not in NOT_FORWARDED}
            return client.request(method, target, headers=headers, content=body or None)

        def relay(self, resp: httpx.Response, rewrite: bool) -> None:
            content = resp.content
            content_type = resp.headers.get("content-type", "application/json")
            if rewrite and rewrite_downloads and "json" in content_type:
                content = self.rewrite_links(content)
            self.send_body(resp.status_code, content, content_type)

        def rewrite_links(self, content: bytes) -> bytes:
            """Gnani's file list carries signed S3 links; send them through /__dl so downloads can be broken too."""
            try:
                data = json.loads(content)
                for entry in data.get("data", []):
                    link = entry.get("transcript_url")
                    if link:
                        host = self.headers.get("Host", "127.0.0.1")
                        entry["transcript_url"] = f"http://{host}/__dl?u={urllib.parse.quote(link, safe='')}"
                return json.dumps(data).encode()
            except (ValueError, AttributeError):
                return content

        # --- the request -----------------------------------------------------------------------------------------

        def handle_any(self) -> None:
            method = self.command
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            path = self.path

            if path.startswith(("/__rules", "/__log", "/__health")):
                return self.control(method, path, body)

            rule = state.take_rule(method, path)
            action = rule.get("action", "status") if rule else "pass"
            entry = state.begin(method, path, action)
            status: int | None = None
            try:
                if rule is None:
                    resp = (
                        client.get(urllib.parse.unquote(path.split("u=", 1)[1]))
                        if path.startswith("/__dl")
                        else self.forward(method, path, body)
                    )
                    status = resp.status_code
                    return self.relay(resp, rewrite=path.rstrip("/").endswith("/files"))
                if action == "drop":
                    return self.hang_up()
                if action == "hang":
                    time.sleep(float(rule.get("seconds", 40)))
                    return self.hang_up()
                if action == "garbage":
                    status = 200
                    return self.send_body(200, b"<html><body>upstream hiccup</body></html>", "text/html")
                if action == "synthetic":
                    status = 200
                    return self.send_json(200, rule.get("body", {}))
                if action == "forward_then_fail":
                    self.forward(method, path, body)  # the provider acts on it; we pretend it did not answer
                status = int(rule.get("status", 503))
                default = {"error": "injected", "message": "injected by fault_proxy"}
                self.send_json(status, rule.get("body", default), rule.get("headers"))
            finally:
                state.finish(entry, status)

        def control(self, method: str, path: str, body: bytes) -> None:
            if path == "/__rules" and method == "PUT":
                with state.lock:
                    state.rules = json.loads(body or b"[]")
                return self.send_json(200, {"rules": len(state.rules)})
            if path == "/__rules" and method == "DELETE":
                with state.lock:
                    state.rules = []
                return self.send_json(200, {"rules": 0})
            if path == "/__log" and method == "GET":
                with state.lock:
                    return self.send_json(200, state.log)
            if path == "/__log" and method == "DELETE":
                with state.lock:
                    state.log = []
                return self.send_json(200, {"log": 0})
            if path == "/__health":
                return self.send_json(200, {"ok": True})
            self.send_json(404, {"error": "unknown control path"})

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = handle_any  # noqa: N815

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--upstream", required=True, help="the real provider, e.g. https://api.vachana.ai")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--rewrite-downloads", action="store_true")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.upstream, args.rewrite_downloads, State()))
    print(f"fault proxy on 127.0.0.1:{args.port} -> {args.upstream}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
