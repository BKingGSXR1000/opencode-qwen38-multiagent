#!/usr/bin/env python3
"""Actual OpenCode v1 part-stream normalization and watchdog clock."""
import unittest
import sqlite3
import tempfile
import json
from pathlib import Path
from unittest import mock
import supervisor
import adaptive_reasoning_watchdog as policy


class NativeV1SSEWatchdogTests(unittest.TestCase):
    SID="ses-native-watch"

    def state(self):
        return {"reasoning":0,"text":0,"progress_seq":0,
                "action_reasoning_chars":0,"action_seq":0,
                "tool_successes":0}

    def event(self,kind,**props):
        return {"type":kind,"data":{"sessionID":self.SID,**props}}

    def test_readonly_part_lookup_is_bound_to_exact_session(self):
        with tempfile.TemporaryDirectory() as td:
            db=Path(td)/"parts.db"
            con=sqlite3.connect(db)
            con.execute("CREATE TABLE part (id TEXT, session_id TEXT, data TEXT)")
            con.execute("INSERT INTO part VALUES (?,?,?)",
                        ("prt-a",self.SID,json.dumps({"type":"reasoning"})))
            con.commit();con.close()
            with mock.patch.object(supervisor,"v1_runtime_enabled",return_value=True), \
                 mock.patch.object(supervisor,"db_connect",
                                   side_effect=lambda:sqlite3.connect(db)):
                self.assertEqual(supervisor.native_v1_part_type(self.SID,"prt-a"),
                                 "reasoning")
                self.assertEqual(supervisor.native_v1_part_type("ses-other","prt-a"),
                                 "")
                self.assertEqual(supervisor.native_v1_part_type(self.SID,"prt-other"),
                                 "")

    def test_late_subscriber_recovers_reasoning_part_from_same_session(self):
        state=self.state();calls=[]
        def lookup(sid,pid):
            calls.append((sid,pid))
            return "reasoning" if sid==self.SID and pid=="prt-reason" else ""
        for i in range(30):
            raw=self.event("message.part.delta",partID="prt-reason",
                field="text",delta=("frame "+str(i)+"x"*300))
            e=supervisor.normalize_native_v1_stream_event(
                state,raw,self.SID,lookup=lookup)
            self.assertEqual(e["type"],"session.next.reasoning.delta")
            supervisor.reduce_live_event(state,e,now=100+i*2)
        self.assertEqual(calls,[(self.SID,"prt-reason")])
        self.assertGreater(state["action_reasoning_chars"],9000)
        self.assertEqual(state["text"],0)

    def test_normal_text_is_not_reasoning(self):
        state=self.state()
        supervisor.normalize_native_v1_stream_event(state,
            self.event("message.part.updated",
                part={"id":"prt-text","type":"text"}),self.SID)
        e=supervisor.normalize_native_v1_stream_event(state,
            self.event("message.part.delta",partID="prt-text",
                field="text",delta="hello user"),self.SID,
                lookup=lambda *_:self.fail("unexpected DB query"))
        self.assertEqual(e["type"],"session.next.text.delta")
        supervisor.reduce_live_event(state,e,now=100)
        self.assertEqual(state["text"],10)
        self.assertEqual(state["action_reasoning_chars"],0)

    def test_cross_session_event_never_resolves_foreign_part(self):
        state=self.state()
        e=supervisor.normalize_native_v1_stream_event(state,{
            "type":"message.part.delta","data":{
                "sessionID":"ses-foreign","partID":"prt-foreign",
                "field":"text","delta":"not yours",
            },
        },self.SID,lookup=lambda *_:self.fail("foreign lookup"))
        self.assertEqual(e["type"],"ignored")
        self.assertNotIn("part_types",state)

    def test_native_tool_called_and_completed_once(self):
        state=self.state()
        def feed(status):
            raw=self.event("message.part.updated",part={
                "id":"prt-tool","type":"tool","state":{"status":status}})
            e=supervisor.normalize_native_v1_stream_event(state,raw,self.SID)
            supervisor.reduce_live_event(state,e,now=100)
            if e["type"]=="session.next.tool.success":
                state["tool_successes"]+=1
            return e["type"]
        self.assertEqual(feed("running"),"session.next.tool.called")
        self.assertEqual(state["action_seq"],1)
        self.assertEqual(feed("running"),"ignored")
        self.assertEqual(feed("completed"),"session.next.tool.success")
        self.assertEqual(feed("completed"),"ignored")
        self.assertEqual(state["tool_successes"],1)
        self.assertEqual(state["action_seq"],1)

    def test_native_reasoning_only_step_preserves_budget(self):
        state=self.state();clock={}
        def feed(e,t):
            normalized=supervisor.normalize_native_v1_stream_event(
                state,e,self.SID,lookup=lambda *_:"reasoning")
            supervisor.reduce_live_event(state,normalized,now=t)
        feed(self.event("message.part.delta",partID="prt-reason",
            field="text",delta="think "*400),100)
        key=("sse:"+self.SID,"0",0)
        self.assertEqual(policy.action_clock(clock,key,100,
            state["action_reasoning_chars"],
            observable=True,tool_running=False),(0,0))
        feed(self.event("message.part.updated",part={
            "id":"prt-step","type":"step-start"}),105)
        feed(self.event("message.part.delta",partID="prt-reason",
            field="text",delta="think "*400),181)
        age,used=policy.action_clock(clock,key,181,
            state["action_reasoning_chars"],
            observable=True,tool_running=False)
        self.assertEqual(age,81)
        self.assertEqual(used,2400)
        self.assertEqual(state["action_reasoning_chars"],4800)


if __name__=="__main__":
    unittest.main()
