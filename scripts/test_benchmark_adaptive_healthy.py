#!/usr/bin/env python3
"""Isolated benchmark measurement and process-scope safety regressions."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

source=Path(__file__).with_name("benchmark-adaptive-healthy.py")
spec=importlib.util.spec_from_file_location("adaptive_healthy_benchmark",source)
bench=importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


class HealthyBenchmarkTests(unittest.TestCase):
    def test_modes_have_unique_private_ports_and_do_not_touch_shared_servers(self):
        self.assertEqual(set(bench.PORTS),set(bench.MODES))
        self.assertEqual(len(set(bench.PORTS.values())),3)
        self.assertTrue(all(p not in {18030,18033,57182,58508,58518,58528}
                            for p in bench.PORTS.values()))

    def test_existing_mode_dir_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);mode=root/"off";mode.mkdir()
            marker=mode/"KEEP-ME";marker.write_text("prior result")
            with mock.patch.object(bench,"RUN_ROOT",root):
                with self.assertRaisesRegex(RuntimeError,"never overwrite"):
                    bench.run("off",10)
            self.assertEqual(marker.read_text(),"prior result")

    def test_measurement_counts_actual_native_part_and_message_tokens(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);db=root/"xdg/data-v11831-a2/opencode"
            db.mkdir(parents=True)
            con=sqlite3.connect(db/"opencode.db")
            con.executescript("""
                CREATE TABLE session(id TEXT,directory TEXT);
                CREATE TABLE part(session_id TEXT,data TEXT);
                CREATE TABLE message(session_id TEXT,data TEXT);
            """)
            project=root/"project";project.mkdir()
            con.executemany("INSERT INTO session VALUES (?,?)",
                [("s1",str(project)),("s2",str(project)),
                 ("foreign","/somewhere-else")])
            parts=[
                ("s1",{"type":"tool","state":{"status":"completed"}}),
                ("s1",{"type":"tool","state":{"status":"error"}}),
                ("s2",{"type":"tool","state":{"status":"completed"}}),
                ("foreign",{"type":"tool","state":{"status":"completed"}}),
            ]
            messages=[
                ("s1",{"role":"assistant","tokens":{
                    "input":110,"output":25,"reasoning":5}}),
                ("s2",{"role":"assistant","tokens":{
                    "input":60,"output":10,"reasoning":4}}),
                ("s2",{"role":"user","tokens":{
                    "input":999,"output":999,"reasoning":999}}),
                ("foreign",{"role":"assistant","tokens":{
                    "input":999,"output":999,"reasoning":999}}),
            ]
            con.executemany("INSERT INTO part VALUES (?,?)",
                            [(sid,json.dumps(row)) for sid,row in parts])
            con.executemany("INSERT INTO message VALUES (?,?)",
                            [(sid,json.dumps(row)) for sid,row in messages])
            con.commit();con.close()
            with mock.patch.object(bench,"ROOT",root):
                value=bench.metric(project,0,1)
            self.assertEqual(value,{
                "sessions":2,"tool_calls":3,"tool_successes":2,
                "input_tokens":170,"output_tokens":35,"reasoning_tokens":9,
            })

    def test_process_shutdown_rejects_unrelated_supervisor_and_server(self):
        project=Path("/tmp/exact-project-only")
        with mock.patch.object(bench,"proc_env",
                return_value=b"V2_PROJECT=/tmp/other\0"), \
             mock.patch.object(Path,"read_bytes",
                return_value=b"python3\0scripts/supervisor.py\0"), \
             mock.patch.object(bench.os,"kill") as kill:
            with self.assertRaisesRegex(RuntimeError,"unrelated supervisor"):
                bench.safe_kill(99,project)
            kill.assert_not_called()

        with mock.patch.object(bench,"proc_env",
                return_value=(
                    b"V2_OPENCODE_BASE_URL=http://127.0.0.1:58542\0"
                )), \
             mock.patch.object(Path,"read_bytes",
                return_value=b"opencode"+bytes((0,))+b"serve"+bytes((0,))+b"--port"+bytes((0,))+b"58542"+bytes((0,))), \
             mock.patch.object(bench.os,"kill") as kill:
            with self.assertRaisesRegex(RuntimeError,"unrelated server"):
                bench.safe_kill(98,project,port=58543,server=True)
            kill.assert_not_called()


if __name__=="__main__":
    unittest.main()
