#!/usr/bin/env python3
"""The same contract, with no framework at all — tier 0.

`service.py` is the worked example. This is here to prove there is no magic in it: a
CIRCULess service at tier 0 is an HTTP server that checks its own API key and reads a
body. If you are integrating from Java, Go or Node, this is the whole specification.

    REFERENCE_SERVICE_KEY=sk-… python3 minimal.py

Standard library only, about twenty lines of actual logic.
"""

from __future__ import annotations

import csv
import io
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

KEY = os.environ["REFERENCE_SERVICE_KEY"]


class Service(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's interface
        # 1. Check your own API key. That is the entire integration: the node holds the
        #    same value, encrypted, and sends it on every call.
        if self.headers.get("X-API-Key") != KEY:
            return self.reply(401, {"error": "bad or missing X-API-Key"})

        # 2. Work on the bytes you were handed. There is nothing to fetch — this
        #    service holds no credentials for the node, and the caller's token never
        #    reaches it. The caller downloads from the node, where that download is
        #    decided and logged against them, and posts the bytes here.
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if not body.strip():
            return self.reply(422, {"error": "the body is empty; send a CSV"})
        header = next(csv.reader(io.StringIO(body.decode("utf-8", "replace"))), None)
        if not header:
            return self.reply(422, {"error": "no header row"})

        self.reply(200, {"columns": header, "count": len(header)})

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            return self.reply(200, {"ok": True})
        self.reply(404, {"error": "not found"})

    def reply(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:
        # Optional, and the cheapest item on the tier-2 list: log the node's request id.
        # It is the one string that joins your logs to the node's access log, and an
        # incident is where you find out whether you kept it.
        print(f"request_id={self.headers.get('X-CIRCULess-Request-Id', '-')} " + fmt % args)


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8080), Service).serve_forever()  # nosec B104 — private network
