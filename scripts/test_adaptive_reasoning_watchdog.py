#!/usr/bin/env python3
"""Adaptive reasoning watchdog: deterministic policy, isolation and SSE tests."""
import unittest
from unittest import mock

import adaptive_reasoning_watchdog as policy
import supervisor


class AdaptiveReasoningWatchdogTests(unittest.TestCase):
    def test_default_disabled_and_explicit_modes(self):
        self.assertEqual(policy.mode(None), "off")
        self.assertEqual(policy.mode(" OBSERVE "), "observe")
        self.assertEqual(policy.mode("enforce"), "enforce")
        with self.assertRaisesRegex(ValueError, "must be"):
            policy.mode("force")

    def test_task_complexity_is_from_guarded_leaf(self):
        self.assertEqual(policy.profile("core-builder", {
            "complexity": "S", "repeated_operations": 0
        }), "TRIVIAL")
        self.assertEqual(policy.profile("feature-builder", {
            "complexity": "S", "repeated_operations": 1
        }), "NORMAL")
        self.assertEqual(policy.profile("test-builder", {
            "complexity": "M", "repeated_operations": 2
        }), "NORMAL")
        self.assertEqual(policy.profile("core-builder", {
            "complexity": "M", "repeated_operations": 7
        }), "COMPLEX")
        self.assertEqual(policy.profile("implementer", {
            "complexity": "S", "deep_reasoning": True
        }), "COMPLEX")
        self.assertEqual(policy.profile("reasoning-builder", {
            "complexity": "S", "deep_reasoning": False
        }), "COMPLEX")
        self.assertEqual(policy.profile("probe-builder", {}), "RESEARCH")
        self.assertEqual(policy.profile("implementation-planner", {}), "")
        self.assertEqual(policy.profile("orchestrator", {}), "")

    def test_reasoning_progress_does_not_reset_action_clock(self):
        st = {}
        key = ("m1", "", 0)
        age, used = policy.action_clock(
            st, key, 100, 120, observable=True, tool_running=False
        )
        self.assertEqual((age, used), (0, 0))
        age, used = policy.action_clock(
            st, key, 180, 7120, observable=True, tool_running=False
        )
        self.assertEqual((age, used), (80, 7000))
        # Legacy liveness timer deliberately resets on changed reasoning;
        # the new action timer must not.
        supervisor.watch.clear()
        supervisor.watchdog_age("s1", ("m1", ""), True, ("m1", "", 1), now=100)
        legacy_age, _ = supervisor.watchdog_age(
            "s1", ("m1", ""), True, ("m1", "", 2), now=180
        )
        self.assertEqual(legacy_age, 0)
        self.assertEqual(age, 80)

    def test_completed_tool_resets_clock_even_if_message_id_unchanged(self):
        st = {}
        policy.action_clock(st, ("m1", "t1", 0), 100, 10, observable=True, tool_running=False)
        self.assertEqual(policy.action_clock(
            st, ("m1", "t1", 0), 190, 7010, observable=True, tool_running=False
        ), (90, 7000))
        self.assertEqual(policy.action_clock(
            st, ("m1", "t1", 1), 191, 7010, observable=True, tool_running=False
        ), (0, 0))
        self.assertEqual(policy.action_clock(
            st, ("m1", "t2", 1), 200, 500, observable=True, tool_running=False
        ), (0, 0))

    def test_running_tool_and_nonobservable_stream_reset_clock(self):
        st = {}
        policy.action_clock(st, ("m1", "", 0), 100, 0, observable=True, tool_running=False)
        self.assertEqual(policy.action_clock(
            st, ("m1", "", 0), 1000, 9000, observable=True, tool_running=True
        ), (0, 0))
        self.assertEqual(st, {})
        self.assertEqual(policy.action_clock(
            st, ("", "", 0), 1005, 8000, observable=True, tool_running=False
        ), (0, 0))
        self.assertEqual(st, {})
        self.assertEqual(policy.action_clock(
            st, ("m1", "", 0), 1010, 8000, observable=False, tool_running=False
        ), (0, 0))

    def test_sse_counter_reset_fails_open_after_reconnect(self):
        st = {}
        policy.action_clock(st, ("m1", "", 0), 100, 500, observable=True, tool_running=False)
        self.assertEqual(policy.action_clock(
            st, ("m1", "", 0), 130, 200, observable=True, tool_running=False
        ), (0, 0))
        self.assertEqual(policy.action_clock(
            st, ("m1", "", 0), 175, 200, observable=True, tool_running=False
        ), (45, 0))

    def test_budget_requires_reasoning_AND_duration(self):
        leaf = {"complexity": "S", "repeated_operations": 1}
        def evaluate(age, chars):
            return policy.decision(
                agent="implementer", leaf=leaf, action_age=age,
                reasoning_chars=chars, loop_detected=False,
                observable=True, tool_running=False,
                compaction_active=False,
            )
        self.assertFalse(evaluate(5, 9000)["abort"])
        self.assertFalse(evaluate(200, 1000)["abort"])
        self.assertTrue(evaluate(76, 7000)["abort"])
        self.assertEqual(evaluate(76, 7000)["reason"], "reasoning-budget-without-tool")
        self.assertTrue(evaluate(181, 3500)["abort"])
        self.assertEqual(evaluate(181, 3500)["reason"], "action-timeout-with-reasoning")

    def test_complex_budget_exceeds_normal_and_trivial(self):
        leaf = {"complexity": "M", "deep_reasoning": True}
        kwargs = dict(
            agent="core-builder", leaf=leaf, action_age=90,
            reasoning_chars=8000, loop_detected=False, observable=True,
            tool_running=False, compaction_active=False,
        )
        self.assertFalse(policy.decision(**kwargs)["abort"])
        kwargs.update(action_age=130, reasoning_chars=15000)
        d = policy.decision(**kwargs)
        self.assertTrue(d["abort"])
        self.assertEqual(d["profile"], "COMPLEX")
        self.assertEqual(d["budget"]["reasoning_chars"], 14000)

    def test_loop_detection_requires_three_long_identical_blocks(self):
        # Enough different characters to avoid whitespace/punctuation repeats.
        pattern = ("reason with concrete details and unique identifiers " * 3)[:120]
        self.assertEqual(len(pattern), 120)
        self.assertTrue(policy.repeated_reasoning_tail(pattern * 3))
        self.assertFalse(policy.repeated_reasoning_tail(pattern * 2))
        self.assertFalse(policy.repeated_reasoning_tail("x" * 400))
        self.assertFalse(policy.repeated_reasoning_tail(pattern * 2 + "different" * 15))

    def test_streamed_reasoning_tail_is_bounded_and_clears_after_tool(self):
        state = {"reasoning": 0, "text": 0, "progress_seq": 0}
        pattern = ("loop details with concrete repeated plan " * 4)[:120]
        for part in (pattern[:60], pattern[60:] * 1, pattern, pattern):
            supervisor.reduce_live_event(
                state, {"type": "session.next.reasoning.delta",
                        "data": {"delta": part}}, now=100.0
            )
        self.assertTrue(state["reasoning_loop_detected"])
        self.assertLessEqual(len(state["reasoning_tail"]), 1440)
        supervisor.reduce_live_event(
            state, {"type": "session.next.tool.success", "data": {}}, now=101.0
        )
        self.assertNotIn("reasoning_loop_detected", state)
        self.assertNotIn("reasoning_tail", state)
        self.assertEqual(state["reasoning"], 0)

    def test_loop_intervention_needs_age_and_reasoning(self):
        base = dict(
            agent="implementer",
            leaf={"complexity": "S", "repeated_operations": 1},
            reasoning_chars=3500, loop_detected=True,
            observable=True, tool_running=False, compaction_active=False,
        )
        self.assertFalse(policy.decision(action_age=20, **base)["abort"])
        d = policy.decision(action_age=35, **base)
        self.assertTrue(d["abort"])
        self.assertEqual(d["gate"], "loop")

    def test_queued_backend_never_causes_spurious_interrupt(self):
        d = policy.decision(
            agent="implementer", leaf={"complexity": "S"},
            action_age=1000, reasoning_chars=0, loop_detected=False,
            observable=True, tool_running=False, compaction_active=False,
            backend={"metrics_available": True, "running": 0, "waiting": 4},
        )
        self.assertFalse(d["abort"])
        self.assertEqual(d["gate"], "no-reasoning")

    def test_running_tool_compaction_and_missing_parts_suppress_interrupt(self):
        base = dict(
            agent="implementer", leaf={"complexity": "S"},
            action_age=500, reasoning_chars=18000, loop_detected=True,
            observable=True, tool_running=False, compaction_active=False,
        )
        for changed in ({"tool_running": True}, {"compaction_active": True},
                        {"observable": False}):
            d = policy.decision(**(base | changed))
            self.assertFalse(d["abort"])
            self.assertEqual(d["gate"], "ineligible")

    def test_backend_global_token_progress_does_not_override_exclusive_budget(self):
        base = dict(
            agent="implementer", leaf={"complexity": "S"},
            action_age=90, reasoning_chars=7000, loop_detected=False,
            observable=True, tool_running=False, compaction_active=False,
        )
        shared = policy.decision(**base, backend={
            "metrics_available": True, "running": 3,
            "generation_progress_age": 0.1,
        })
        exclusive = policy.decision(**base, backend={
            "metrics_available": True, "running": 1,
            "generation_progress_age": 0.1,
        })
        self.assertTrue(shared["abort"])
        self.assertTrue(exclusive["abort"])
        self.assertEqual(shared["backend_running"], 3)

    def test_supervisor_observe_deduplicates_without_interrupt(self):
        sid="ses-test-observe"
        supervisor.adaptive_watch.pop(sid,None)
        shape={"message_id":"msg","last_tool_id":""}
        live={"tool_successes":0}
        watch={"aborted_key":None}
        manifest={"leaves":{"D001":{
            "complexity":"S","repeated_operations":1,
        }}}
        with mock.patch.object(supervisor,"ADAPTIVE_REASONING_MODE","observe"), \
             mock.patch.object(supervisor,"load_manifest",return_value=manifest), \
             mock.patch.object(supervisor,"abort_session") as interrupt, \
             mock.patch.object(supervisor,"log") as log, \
             mock.patch.object(supervisor,"csv"), \
             mock.patch.object(supervisor,"emit_watchdog_telemetry") as telemetry:
            first=supervisor.adaptive_reasoning_step(
                sid,"implementer","D001",("msg",""),shape,live,0,
                can_watch=True,tool_running=False,compaction_active=False,
                backend_snapshot={},watch_state=watch,now_mono=100,
            )
            second=supervisor.adaptive_reasoning_step(
                sid,"implementer","D001",("msg",""),shape,live,7500,
                can_watch=True,tool_running=False,compaction_active=False,
                backend_snapshot={},watch_state=watch,now_mono=180,
            )
            again=supervisor.adaptive_reasoning_step(
                sid,"implementer","D001",("msg",""),shape,live,7600,
                can_watch=True,tool_running=False,compaction_active=False,
                backend_snapshot={},watch_state=watch,now_mono=182,
            )
            self.assertFalse(first["abort"])
            self.assertTrue(second["abort"])
            self.assertTrue(again["abort"])
            interrupt.assert_not_called()
            telemetry.assert_called_once()
            self.assertTrue(telemetry.call_args.kwargs["force"])
            self.assertEqual(
                telemetry.call_args.args[1]["adaptive_event"],
                "would-interrupt",
            )
            log.assert_called_once()
            self.assertIn("WOULD_INTERRUPT",log.call_args.args[0])
            self.assertIsNone(watch["aborted_key"])
        supervisor.adaptive_watch.pop(sid,None)

    def test_supervisor_enforce_interrupts_once_and_fails_open_on_http_failure(self):
        sid="ses-test-enforce"
        supervisor.adaptive_watch.pop(sid,None)
        shape={"message_id":"msg","last_tool_id":""}
        watch={"aborted_key":None}
        manifest={"leaves":{"D001":{
            "complexity":"S","repeated_operations":1,
        }}}
        with mock.patch.object(supervisor,"ADAPTIVE_REASONING_MODE","enforce"), \
             mock.patch.object(supervisor,"load_manifest",return_value=manifest), \
             mock.patch.object(supervisor,"abort_session",side_effect=[False,True]) as interrupt, \
             mock.patch.object(supervisor,"log"), \
             mock.patch.object(supervisor,"csv"), \
             mock.patch.object(supervisor,"emit_watchdog_telemetry") as telemetry:
            kwargs=dict(
                sid=sid,agent="implementer",did="D001",key=("msg",""),
                shape=shape,live={"tool_successes":0},
                can_watch=True,tool_running=False,compaction_active=False,
                backend_snapshot={},watch_state=watch,
            )
            supervisor.adaptive_reasoning_step(reasoning=0,now_mono=100,**kwargs)
            supervisor.adaptive_reasoning_step(reasoning=7500,now_mono=180,**kwargs)
            self.assertEqual(interrupt.call_count,1)
            self.assertIsNone(watch["aborted_key"])
            supervisor.adaptive_reasoning_step(reasoning=7510,now_mono=181,**kwargs)
            self.assertEqual(interrupt.call_count,2)
            telemetry.assert_called_once()
            self.assertEqual(
                telemetry.call_args.args[1]["adaptive_event"],
                "interrupt-confirmed",
            )
            self.assertEqual(watch["aborted_key"],("msg",""))
            supervisor.adaptive_reasoning_step(reasoning=7520,now_mono=182,**kwargs)
            self.assertEqual(interrupt.call_count,2)
        supervisor.adaptive_watch.pop(sid,None)

    def test_supervisor_default_off_makes_no_observation_state(self):
        sid="ses-test-off"
        supervisor.adaptive_watch.pop(sid,None)
        with mock.patch.object(supervisor,"ADAPTIVE_REASONING_MODE","off"), \
             mock.patch.object(supervisor,"abort_session") as interrupt:
            result=supervisor.adaptive_reasoning_step(
                sid,"implementer","D001",("msg",""),
                {"message_id":"msg","last_tool_id":""},{},10000,
                can_watch=True,tool_running=False,compaction_active=False,
                backend_snapshot={},watch_state={"aborted_key":None},now_mono=100,
            )
            self.assertEqual(result,{})
            self.assertNotIn(sid,supervisor.adaptive_watch)
            interrupt.assert_not_called()

    def test_stream_counter_persists_across_reasoning_only_model_steps(self):
        state={"reasoning":0,"text":0,"progress_seq":0,"connected":True,
               "action_reasoning_chars":0,"action_seq":0,"tool_successes":0}
        supervisor.reduce_live_event(
            state,{"type":"session.next.reasoning.delta",
                   "data":{"delta":"first "*600}},now=100,
        )
        self.assertEqual(state["action_reasoning_chars"],3600)
        supervisor.reduce_live_event(
            state,{"type":"session.next.step.started","data":{}},now=101,
        )
        self.assertEqual(state["reasoning"],0)
        self.assertEqual(state["action_reasoning_chars"],3600)
        supervisor.reduce_live_event(
            state,{"type":"session.next.reasoning.delta",
                   "data":{"delta":"second "*600}},now=102,
        )
        self.assertEqual(state["action_reasoning_chars"],7800)
        supervisor.reduce_live_event(
            state,{"type":"session.next.tool.called","data":{}},now=103,
        )
        self.assertEqual(state["action_reasoning_chars"],0)
        self.assertEqual(state["action_seq"],1)
        self.assertNotIn("reasoning_tail",state)
        supervisor.reduce_live_event(
            state,{"type":"session.next.tool.success","data":{}},now=104,
        )
        self.assertEqual(state["action_reasoning_chars"],0)

    def test_supervisor_sse_action_clock_ignores_message_id_only_rollover(self):
        sid="ses-test-action-stability"
        supervisor.adaptive_watch.pop(sid,None)
        leaf={"leaves":{"D001":{
            "complexity":"S","repeated_operations":1,
        }}}
        live={"connected":True,"tool_successes":0,"action_seq":0,
              "action_reasoning_chars":0}
        shape={"message_id":"step-one","last_tool_id":""}
        with mock.patch.object(supervisor,"ADAPTIVE_REASONING_MODE","observe"), \
             mock.patch.object(supervisor,"load_manifest",return_value=leaf), \
             mock.patch.object(supervisor,"log"), \
             mock.patch.object(supervisor,"csv"):
            kwargs=dict(sid=sid,agent="implementer",did="D001",key=("", ""),
                        live=live,can_watch=True,tool_running=False,
                        compaction_active=False,backend_snapshot={},
                        watch_state={"aborted_key":None})
            first=supervisor.adaptive_reasoning_step(
                shape=shape,reasoning=0,now_mono=100,**kwargs)
            shape["message_id"]="step-two"
            live["action_reasoning_chars"]=7000
            second=supervisor.adaptive_reasoning_step(
                shape=shape,reasoning=4000,now_mono=180,**kwargs)
            self.assertFalse(first["abort"])
            self.assertTrue(second["abort"])
            self.assertGreaterEqual(second["reasoning_chars_since_action"],7000)
            live["action_seq"]+=1
            live["action_reasoning_chars"]=0
            third=supervisor.adaptive_reasoning_step(
                shape=shape,reasoning=0,now_mono=181,**kwargs)
            self.assertFalse(third["abort"])
            self.assertEqual(third["action_age"],0)
        supervisor.adaptive_watch.pop(sid,None)

    def test_immediate_candidate_telemetry_persists_even_between_polling_ticks(self):
        import json
        import tempfile
        from pathlib import Path
        verdict={"abort":True,"profile":"NORMAL","action_age":75.3,
                 "reasoning_chars_since_action":11778,
                 "reason":"reasoning-budget-without-tool"}
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"events.jsonl"
            with mock.patch.object(supervisor,"WATCHDOG_TELEMETRY",path), \
                 mock.patch.object(supervisor,"watchdog_telemetry_last",{}), \
                 mock.patch.object(supervisor,"PROJECT",td), \
                 mock.patch.object(supervisor,"ADAPTIVE_REASONING_MODE","enforce"):
                supervisor.record_adaptive_watchdog_event(
                    "ses-native","implementer","D001",verdict,
                    "interrupt-confirmed",{"running":1},
                )
            rows=[json.loads(x) for x in path.read_text().splitlines()]
        self.assertEqual(len(rows),1)
        row=rows[0]
        self.assertEqual(row["adaptive_event"],"interrupt-confirmed")
        self.assertEqual(row["adaptive_reasoning"]["reasoning_chars_since_action"],11778)
        self.assertEqual(row["session"],"ses-native")
        self.assertNotIn("reasoning_text",row)

    def test_telemetry_filesystem_failure_cannot_unconfirm_worker_abort(self):
        verdict={"abort":True,"profile":"NORMAL","reason":"budget"}
        with mock.patch.object(supervisor,"emit_watchdog_telemetry",
                               side_effect=OSError("disk full")), \
             mock.patch.object(supervisor,"log") as log:
            supervisor.record_adaptive_watchdog_event(
                "ses-native","implementer","D001",verdict,
                "interrupt-confirmed",{},
            )
            log.assert_called_once()
            self.assertIn("ADAPTIVE_REASONING_TELEMETRY_FAILED",
                          log.call_args.args[0])

    def test_runtime_reload_fingerprint_tracks_new_module(self):
        from control_policy import reexec_source_paths
        names = {p.name for p in reexec_source_paths()}
        self.assertIn("adaptive_reasoning_watchdog.py", names)
        self.assertIn("supervisor.py", names)


if __name__ == "__main__":
    unittest.main()
