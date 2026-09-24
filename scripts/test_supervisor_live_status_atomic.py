#!/usr/bin/env python3
"""Regression for concurrent supervisor live-status publication."""

import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))

import supervisor


class LiveStatusAtomicTests(unittest.TestCase):
    def test_concurrent_writers_use_collision_safe_atomic_publish(self):
        errors=[]
        with tempfile.TemporaryDirectory(prefix="v2-live-status-test-") as td:
            path=Path(td)/"supervisor-live.json"
            barrier=threading.Barrier(12)

            def writer(worker):
                try:
                    barrier.wait(timeout=5)
                    for seq in range(40):
                        supervisor.persist_live_status({
                            "worker":worker,
                            "seq":seq,
                            "sessions":[],
                        })
                except Exception as exc:
                    errors.append(exc)

            with mock.patch.object(supervisor,"LIVE_STATUS",path):
                threads=[
                    threading.Thread(target=writer,args=(worker,))
                    for worker in range(12)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)

            self.assertFalse(
                any(thread.is_alive() for thread in threads),
                "concurrent live-status writers did not finish",
            )
            self.assertEqual(errors,[])
            payload=json.loads(path.read_text())
            self.assertIn("worker",payload)
            self.assertIn("seq",payload)
            self.assertEqual(payload.get("sessions"),[])
            self.assertEqual(
                list(path.parent.glob(f".{path.name}.*.tmp")),
                [],
            )


if __name__=="__main__":
    unittest.main(verbosity=2)
