#!/usr/bin/env python3
"""One-shot OpenAI-compatible SSE fault injector for splitter canaries."""
from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ap=argparse.ArgumentParser(description=__doc__)
ap.add_argument("--listen",type=int,default=18034)
ap.add_argument("--target",default="http://127.0.0.1:18033")
ap.add_argument(
    "--greedy-after-fault",action="store_true",
    help="make forwarded chat completions deterministic after the injected fault",
)
ns=ap.parse_args()
lock=threading.Lock()
faults_left=1


def sse_fault():
    created=int(time.time())
    first={
        "id":"chatcmpl-canary-fault","object":"chat.completion.chunk",
        "created":created,"model":"qwen3.8-27b",
        "choices":[{"index":0,"delta":{
            "role":"assistant","content":"CANARY_FIRST_RESPONSE_INVALID",
        },"logprobs":None,"finish_reason":None}],
    }
    final={
        "id":"chatcmpl-canary-fault","object":"chat.completion.chunk",
        "created":created,"model":"qwen3.8-27b",
        "choices":[{"index":0,"delta":{},"logprobs":None,
                    "finish_reason":"stop"}],
    }
    return (
        f"data: {json.dumps(first)}\n\n"
        f"data: {json.dumps(final)}\n\n"
        "data: [DONE]\n\n"
    ).encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version="HTTP/1.1"

    def log_message(self,fmt,*args):
        print("FAULT_PROXY",self.command,self.path,fmt % args,flush=True)

    def _read_body(self):
        size=int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(size) if size else b""

    def _forward(self,body=b""):
        headers={
            key:value for key,value in self.headers.items()
            if key.lower() not in {
                "host","content-length","transfer-encoding","connection"
            }
        }
        request=urllib.request.Request(
            ns.target+self.path,data=body or None,
            headers=headers,method=self.command,
        )
        try:
            with urllib.request.urlopen(request,timeout=300) as response:
                payload=response.read()
                self.send_response(response.status)
                self.send_header(
                    "Content-Type",
                    response.headers.get("Content-Type","application/octet-stream"),
                )
        except urllib.error.HTTPError as exc:
            payload=exc.read()
            self.send_response(exc.code)
            self.send_header(
                "Content-Type",exc.headers.get("Content-Type","text/plain")
            )
        self.send_header("Content-Length",str(len(payload)))
        self.send_header("Connection","close")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._forward()

    def do_POST(self):
        global faults_left
        body=self._read_body()
        is_chat=self.path.split("?",1)[0].endswith("/chat/completions")
        inject=False
        if is_chat:
            with lock:
                if faults_left:
                    faults_left-=1
                    inject=True
        if not inject:
            if is_chat and ns.greedy_after_fault:
                request=json.loads(body)
                request["temperature"]=0
                request["top_p"]=1
                request["seed"]=0
                body=json.dumps(request,separators=(",",":")).encode()
                print("CANARY_FORWARD_GREEDY",flush=True)
            self._forward(body)
            return
        payload=sse_fault()
        print("CANARY_FAULT_INJECTED",flush=True)
        self.send_response(200)
        self.send_header("Content-Type","text/event-stream")
        self.send_header("Cache-Control","no-cache")
        self.send_header("Content-Length",str(len(payload)))
        self.send_header("Connection","close")
        self.end_headers()
        self.wfile.write(payload)


if __name__=="__main__":
    server=ThreadingHTTPServer(("127.0.0.1",ns.listen),Handler)
    print(
        f"CANARY_FAULT_PROXY_READY {ns.listen} -> {ns.target}",
        flush=True,
    )
    server.serve_forever()
