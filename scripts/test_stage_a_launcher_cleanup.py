#!/usr/bin/env python3
import subprocess
import unittest
from pathlib import Path

HERE=Path(__file__).resolve().parent
SCRIPT=HERE/"start-stage-a-run.sh"


class StageALauncherCleanupTests(unittest.TestCase):
    def test_shell_syntax(self):
        subprocess.run(["bash","-n",str(SCRIPT)],check=True)

    def test_launcher_owns_runtime_pids_and_has_exit_cleanup(self):
        text=SCRIPT.read_text()
        self.assertIn("SERVER_WRAPPER_PID=$!",text)
        self.assertIn('SUPERVISOR_PID="$("$ROOT/scripts/start-a2-v11831-supervisor.sh"',text)
        self.assertIn("trap cleanup_stage_a_runtime EXIT",text)
        self.assertIn("matches_project_supervisor",text)
        self.assertIn("matches_server_wrapper",text)

    def test_driver_is_waited_not_exec_replaced(self):
        text=SCRIPT.read_text()
        self.assertNotIn('exec python3 "$ROOT/scripts/drive-stage-a-run.py"',text)
        self.assertIn('DRIVER_PID=$!',text)
        self.assertIn('wait "$DRIVER_PID"',text)
        self.assertIn('exit "$DRIVER_RC"',text)

    def test_signal_forwarding_is_bounded_to_driver(self):
        text=SCRIPT.read_text()
        self.assertIn("trap 'forward_driver_signal TERM 143' TERM",text)
        self.assertIn("trap 'forward_driver_signal INT 130' INT",text)
        self.assertIn('kill "-$signal" "$DRIVER_PID"',text)


if __name__=="__main__":
    unittest.main()
