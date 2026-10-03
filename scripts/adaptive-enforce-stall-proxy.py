#!/usr/bin/env python3
"""One-shot OpenAI SSE reasoning-only stall for a private Stage-A canary.

The first chat completion receives a synthetically stalled reasoning stream.
Every later request is forwarded unchanged to the normal local Qwen proxy.
Never bind beyond 127.0.0.1. It logs metadata only, not prompts or secrets.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def stall_frame(index: int, model: str) -> bytes:
    seeds = [
        hashlib.sha256(f"watchdog-stall-frame-{index}-{part}".encode()).hexdigest()
        for part in range(3)
    ]
    reasoning = (
        f"Canary synthetic no-tool reasoning step {index:06d}, "
        f"no state mutation and no function call. Distinct markers: "
        + " ".join(seeds) + ". "
    )
    chunk = {
        "id": "chatcmpl-adaptive-stall-one-shot",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"reasoning_content": reasoning},
            "finish_reason": None,
            "logprobs": None,
        }],
    }
    return ("data: " + json.dumps(chunk, separators=(",", ":")) + "\n\n").encode()


def final_frame(model: str) -> bytes:
    chunk = {
        "id": "chatcmpl-adaptive-stall-one-shot",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0, "delta": {"content": "Synthetic stall exceeded safety cap."},
            "finish_reason": "stop", "logprobs": None,
        }],
    }
    return (
        "data: " + json.dumps(chunk, separators=(",", ":"))
        + "\n\ndata: [DONE]\n\n"
    ).encode()


class StallServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, *, target, interval, cap, events):
        super().__init__(address, handler)
        self.target = target.rstrip("/")
        self.interval = interval
        self.cap = cap
        self.events_path = Path(events)
        self.faults_left = 1
        self.lock = threading.Lock()

    def event(self, kind: str, **detail):
        row = {"at": time.time(), "event": kind, **detail}
        with self.lock:
            with self.events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, sort_keys=True) + "\n")
        print("CANARY_PROXY", kind, detail, flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return

    def forward(self, body=None):
        headers = {
            key: value for key, value in self.headers.items()
            if key.lower() not in {
                "host", "content-length", "transfer-encoding",
                "connection", "accept-encoding",
            }
        }
        request = urllib.request.Request(
            self.server.target + self.path,
            data=body,
            headers=headers,
            method=self.command,
        )
        try:
            upstream = urllib.request.urlopen(request, timeout=180)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            self.send_response(exc.code)
            self.send_header("Content-Type", exc.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True
            return
        with upstream:
            content_type = upstream.headers.get("Content-Type", "application/json")
            self.send_response(upstream.status)
            self.send_header("Content-Type", content_type)
            self.send_header("Connection", "close")
            if "text/event-stream" not in content_type:
                payload = upstream.read()
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                while chunk := upstream.read1(4096):
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
        self.close_connection = True

    def do_GET(self):
        self.forward()

    def do_POST(self):
        size = int(self.headers.get("Content-Length") or 0)
        if size < 0 or size > 20 * 1024 * 1024:
            self.send_error(413)
            return
        body = self.rfile.read(size)
        if self.path.split("?", 1)[0] != "/v1/chat/completions":
            self.forward(body)
            return
        with self.server.lock:
            inject = bool(self.server.faults_left)
            if inject:
                self.server.faults_left -= 1
        if not inject:
            self.forward(body)
            return
        try:
            request = json.loads(body)
            model = str(request.get("model") or "qwen3.8-27b")
        except (TypeError, ValueError):
            model = "qwen3.8-27b"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.server.event("STALL_STARTED", model=model)
        start = time.monotonic()
        index = 0
        try:
            while time.monotonic() - start < self.server.cap:
                self.wfile.write(stall_frame(index, model))
                self.wfile.flush()
                index += 1
                time.sleep(self.server.interval)
            self.wfile.write(final_frame(model))
            self.wfile.flush()
            self.server.event("STALL_SAFETY_CAP_REACHED", frames=index)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.server.event("STALL_STREAM_DISCONNECTED", frames=index)
        finally:
            self.close_connection = True


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--listen", type=int, required=True)
    ap.add_argument("--target", default="http://127.0.0.1:18033")
    ap.add_argument("--interval", type=float, default=1.9)
    ap.add_argument("--cap", type=float, default=145)
    ap.add_argument("--events", type=Path, required=True)
    args = ap.parse_args()
    if not (1 <= args.listen <= 65535 and args.interval >= 1.0 and args.cap <= 160):
        ap.error("invalid bounded canary parameters")
    args.events.parent.mkdir(parents=True, exist_ok=True)
    server = StallServer(
        ("127.0.0.1", args.listen), Handler, target=args.target,
        interval=args.interval, cap=args.cap, events=args.events,
    )
    server.event("PROXY_READY", listen=args.listen, target=args.target)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
