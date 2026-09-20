#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=57182)
    ns = ap.parse_args()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, fmt, *args): pass

        def send_json(self, status, obj):
            raw = json.dumps(obj, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path in ("/health", "/v1/health"):
                return self.send_json(200, {"ok": True, "model": "root-noop"})
            if self.path == "/v1/models":
                return self.send_json(200, {
                    "object": "list",
                    "data": [{"id": "root-noop", "object": "model",
                              "created": 0, "owned_by": "local"}],
                })
            self.send_json(404, {"error": {"message": "not found"}})

        def do_POST(self):
            n = int(self.headers.get("content-length", "0") or "0")
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                body = {}
            if self.path not in ("/v1/chat/completions", "/chat/completions"):
                return self.send_json(404, {"error": {"message": "unsupported"}})

            model = str(body.get("model") or "root-noop")
            stream = bool(body.get("stream"))
            created = int(time.time())
            rid = f"chatcmpl-a2-noop-{time.time_ns()}"

            if not stream:
                return self.send_json(200, {
                    "id": rid, "object": "chat.completion", "created": created,
                    "model": model,
                    "choices": [{"index": 0, "message": {
                        "role": "assistant", "content": "ROOT_NOOP"
                    }, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                              "total_tokens": 0},
                })

            chunks = [
                {
                    "id": rid, "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {
                        "role": "assistant", "content": "ROOT_NOOP"
                    }, "finish_reason": None}],
                },
                {
                    "id": rid, "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                              "total_tokens": 0},
                },
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(
                    b"data: " +
                    json.dumps(chunk, separators=(",", ":")).encode() +
                    b"\n\n"
                )
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    print("A2 v1.18.31 deterministic root no-op provider")
    print(f"listening: http://{ns.host}:{ns.port}/v1")
    ThreadingHTTPServer((ns.host, ns.port), Handler).serve_forever()

if __name__ == "__main__":
    main()
