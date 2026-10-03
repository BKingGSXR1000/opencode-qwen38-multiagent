#!/usr/bin/env python3
"""One-shot fault proxy protocol, isolation and forward-on-retry tests."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SPEC=importlib.util.spec_from_file_location(
    "adaptive_stall_proxy",Path(__file__).with_name("adaptive-enforce-stall-proxy.py")
)
PROXY=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROXY)


class StallProxyTests(unittest.TestCase):
    def test_synthetic_frames_are_reasoning_only_and_unique(self):
        def decode(chunk):
            line=next(x for x in chunk.decode().splitlines() if x.startswith("data: "))
            return json.loads(line[6:])
        first=decode(PROXY.stall_frame(0,"qwen3.8-27b"))
        second=decode(PROXY.stall_frame(1,"qwen3.8-27b"))
        choice=first["choices"][0]
        self.assertEqual(choice["finish_reason"],None)
        self.assertEqual(set(choice["delta"]),{"reasoning_content"})
        self.assertNotIn("tool_calls",choice["delta"])
        self.assertNotEqual(
            choice["delta"]["reasoning_content"],
            second["choices"][0]["delta"]["reasoning_content"],
        )
        self.assertTrue(PROXY.final_frame("qwen3.8-27b").endswith(b"data: [DONE]\n\n"))

    def test_fault_consumed_once_then_forwards_only_to_local_stub(self):
        calls=[]
        class Upstream(BaseHTTPRequestHandler):
            protocol_version="HTTP/1.1"
            def log_message(self,*args):pass
            def do_POST(self):
                count=int(self.headers.get("Content-Length") or 0)
                raw=self.rfile.read(count)
                calls.append(json.loads(raw))
                data=b'{"choices":[]}'
                self.send_response(200)
                self.send_header("Content-Type","application/json")
                self.send_header("Content-Length",str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        upstream=ThreadingHTTPServer(("127.0.0.1",0),Upstream)
        upstream.daemon_threads=True
        host=threading.Thread(target=upstream.serve_forever,daemon=True)
        host.start()
        with tempfile.TemporaryDirectory() as td:
            proxy=PROXY.StallServer(
                ("127.0.0.1",0),PROXY.Handler,
                target="http://127.0.0.1:"+str(upstream.server_port),
                interval=1.0,cap=0.0,
                events=Path(td)/"events.jsonl",
            )
            t=threading.Thread(target=proxy.serve_forever,daemon=True)
            t.start()
            url="http://127.0.0.1:"+str(proxy.server_port)+"/v1/chat/completions"
            body=json.dumps({"model":"qwen3.8-27b","stream":True}).encode()
            try:
                def post():
                    r=urllib.request.Request(
                        url,data=body,
                        headers={"Content-Type":"application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(r,timeout=3) as response:
                        return response.read()
                first=post()
                second=post()
                self.assertIn(b"chatcmpl-adaptive-stall-one-shot",first)
                self.assertEqual(json.loads(second),{"choices":[]})
                self.assertEqual(calls,[{"model":"qwen3.8-27b","stream":True}])
                events=[json.loads(x) for x in (Path(td)/"events.jsonl").read_text().splitlines()]
                self.assertEqual(events[0]["event"],"STALL_STARTED")
                self.assertEqual(events[1]["event"],"STALL_SAFETY_CAP_REACHED")
                self.assertEqual(proxy.faults_left,0)
            finally:
                proxy.shutdown();proxy.server_close()
        upstream.shutdown();upstream.server_close()

    def test_requires_private_bounded_listen_parameters(self):
        script=Path(__file__).with_name("adaptive-enforce-stall-proxy.py")
        with tempfile.TemporaryDirectory() as td:
            for args in [
                ["--listen","0"],
                ["--listen","18035","--interval","0.01"],
                ["--listen","18035","--cap","300"],
            ]:
                run=subprocess.run([
                    sys.executable,str(script),*args,
                    "--events",str(Path(td)/"events.jsonl"),
                ],capture_output=True,text=True,timeout=4)
                self.assertNotEqual(run.returncode,0)
                self.assertIn("invalid bounded canary parameters",run.stderr)


if __name__=="__main__":
    unittest.main()
