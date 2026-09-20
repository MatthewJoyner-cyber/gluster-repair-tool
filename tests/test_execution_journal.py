# SPDX-License-Identifier: GPL-2.0-only
"""Durable execution evidence at command, process and report boundaries."""
import json
import io
from contextlib import redirect_stdout, redirect_stderr
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gluster_heal_tool.executor import execute_apply_results
from gluster_heal_tool.models import ApplyStep
from tests.test_execution_dependencies import action
from tests.test_reversibility_gate import _bind_test_payload, _BOUND_VOLUME_INFO
from gluster_heal_tool import cli


def records(root):
    return [json.loads(p.read_text()) for p in sorted(Path(root).glob("attempts/*/*.json"))]


class ExecutionJournalTests(unittest.TestCase):
    def test_step_intent_precedes_write_and_previous_result_survives_exception(self):
        for failure in (OSError("lost result"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as root:
                item = action("parent")
                item.steps.append(ApplyStep(step_id="second", step_type="remove_stale_backend",
                                           command_preview=["fixture-second"]))
                def execute(step):
                    events = records(root)
                    self.assertTrue(any(e.get("kind") == "step-start" and e["step"]["step_id"] == step.step_id for e in events))
                    if step.step_id == "second":
                        self.assertTrue(any(e.get("kind") == "step-finish" and e["step"]["status"] == "ok" for e in events))
                        raise failure
                    return "ok", 0, "first write completed"
                with patch("gluster_heal_tool.executor._execute_step", side_effect=execute):
                    if isinstance(failure, KeyboardInterrupt):
                        with self.assertRaises(KeyboardInterrupt):
                            execute_apply_results([item], run_dir=root, parallel_nice=0)
                    else:
                        execute_apply_results([item], run_dir=root, parallel_nice=0)
                events = records(root)
                self.assertTrue(any(e.get("kind") == "step-finish" and e["step"]["status"] == "unknown" for e in events))
                with patch("gluster_heal_tool.executor._execute_step") as retry:
                    with self.assertRaises(RuntimeError):
                        execute_apply_results([action("parent")], run_dir=root, parallel_nice=0)
                    retry.assert_not_called()

    def test_each_actual_command_is_recorded_including_backup_parent_creation(self):
        with tempfile.TemporaryDirectory() as root:
            item = action("parent")
            item.steps = [ApplyStep(step_id="backup", step_type="backup_stale_backend", host="host-a",
                                   target_path="/fixture/backup", command_preview=["fixture-copy"])]
            def run(command, **kwargs):
                self.assertTrue(any(e.get("kind") == "command-start" and e["command"] == command for e in records(root)))
                return subprocess.CompletedProcess(command, 0, "copied", "diagnostic")
            with patch("gluster_heal_tool.executor.subprocess.run", side_effect=run):
                execute_apply_results([item], run_dir=root, parallel_nice=0)
            finished = [e for e in records(root) if e.get("kind") == "command-finish"]
            self.assertEqual(2, len(finished))
            self.assertEqual("diagnostic", finished[-1]["stderr"])
            self.assertEqual("copied", finished[-1]["stdout"])

    def test_report_failure_keeps_completed_commands_and_blocks_replay(self):
        with tempfile.TemporaryDirectory() as root:
            with (patch("gluster_heal_tool.executor._execute_step", return_value=("ok", 0, "done")),
                  patch("gluster_heal_tool.executor.write_json_shared", side_effect=OSError("report disk full"))):
                with self.assertRaises(RuntimeError):
                    execute_apply_results([action("parent")], run_dir=root, parallel_nice=0)
            self.assertTrue(any(e.get("kind") == "step-finish" and e["step"]["status"] == "ok" for e in records(root)))
            with patch("gluster_heal_tool.executor._execute_step") as step:
                with self.assertRaises(RuntimeError):
                    execute_apply_results([action("parent")], run_dir=root, parallel_nice=0)
                step.assert_not_called()

    def test_process_death_leaves_intent_and_cannot_replay(self):
        with tempfile.TemporaryDirectory() as root:
            script = """import os,sys
from unittest.mock import patch
from gluster_heal_tool.executor import execute_apply_results
from tests.test_execution_dependencies import action
with patch('gluster_heal_tool.executor._execute_step', side_effect=lambda step: os._exit(73)):
    execute_apply_results([action('parent')], run_dir=sys.argv[1], parallel_nice=0)
"""
            result = subprocess.run([sys.executable, "-B", "-c", script, root], capture_output=True, text=True)
            self.assertEqual(73, result.returncode, result.stderr)
            self.assertTrue(any(e.get("kind") == "step-start" for e in records(root)))
            with patch("gluster_heal_tool.executor._execute_step") as step:
                with self.assertRaises(RuntimeError):
                    execute_apply_results([action("parent")], run_dir=root, parallel_nice=0)
                step.assert_not_called()

    def test_new_successful_attempt_preserves_previous_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            with patch("gluster_heal_tool.executor._execute_step", return_value=("ok", 0, "done")):
                first = execute_apply_results([action("parent")], run_dir=root, parallel_nice=0)
                previous = {str(p):p.read_bytes() for p in Path(root).glob("attempts/*/*.json")}
                second = execute_apply_results([action("parent")], run_dir=root, parallel_nice=0)
            self.assertNotEqual(first["attempt_id"], second["attempt_id"])
            self.assertTrue(all(Path(p).read_bytes() == content for p,content in previous.items()))

    def test_journal_intent_failure_prevents_command_dispatch(self):
        with tempfile.TemporaryDirectory() as root:
            from gluster_heal_tool.execution_journal import ExecutionJournal
            original = ExecutionJournal.append
            def append(journal, kind, **payload):
                if kind == "step-start":
                    raise OSError("intent storage failed")
                return original(journal, kind, **payload)
            with (patch.object(ExecutionJournal, "append", append),
                  patch("gluster_heal_tool.executor._execute_step") as step):
                execute_apply_results([action("parent")], run_dir=root, parallel_nice=0)
            step.assert_not_called()

    def test_parallel_command_results_survive_worker_failure(self):
        with tempfile.TemporaryDirectory() as root:
            def execute(step):
                if step.step_id == "parent":
                    raise OSError("worker result lost")
                return "ok", 0, "peer completed"
            with (patch("gluster_heal_tool.executor._execute_step", side_effect=execute),
                  patch("gluster_heal_tool.executor.os.cpu_count", return_value=8)):
                execute_apply_results([action("parent"), action("peer")], keep_going=True,
                                      parallel_actions=2, parallel_nice=0, run_dir=root)
            finishes = [e for e in records(root) if e.get("kind") == "step-finish"]
            self.assertEqual({"ok", "unknown"}, {e["step"]["status"] for e in finishes})

    def test_cli_report_failure_retains_journal_and_refuses_same_artifact_in_new_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = {"actions": [action("parent").to_dict()]}
            _bind_test_payload(root, payload)
            apply_path, status_path = root / "apply.json", root / "status.json"
            apply_path.write_text(json.dumps(payload))
            status_path.write_text(json.dumps({"volume": "gtest"}))
            args = ["apply-run", "--execute", "--batch", "--skip-health-check", "--allow-heal-on",
                    "--apply-in", str(apply_path), "--status-file", str(status_path)]
            with (patch.object(cli, "get_volume_info", return_value=_BOUND_VOLUME_INFO),
                  patch.object(cli, "get_heal_settings", return_value={}),
                  patch.object(cli, "_refresh_post_execute_heal_preserving_settings") as heal,
                  patch.object(cli, "manage_backup_artifacts") as backups,
                  patch.object(cli, "write_execute_report", side_effect=OSError("report unavailable")),
                  patch("gluster_heal_tool.executor._execute_step", return_value=("ok", 0, "done")) as execute,
                  redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                self.assertEqual(2, cli.main([*args, "--run-dir", str(root / "run1")]))
                self.assertTrue(any(e.get("kind") == "step-finish" for e in records(root / "run1")))
                self.assertEqual(1, execute.call_count)
                self.assertEqual(2, cli.main([*args, "--run-dir", str(root / "run2")]))
                self.assertEqual(1, execute.call_count)
                heal.assert_not_called()
                backups.assert_not_called()

    def test_result_journal_failure_stops_following_commands(self):
        from gluster_heal_tool.execution_journal import ExecutionJournal
        with tempfile.TemporaryDirectory() as root:
            item = action("parent")
            item.steps.append(ApplyStep(step_id="later", step_type="remove_stale_backend",
                                       command_preview=["fixture-later"]))
            original = ExecutionJournal.append
            def append(journal, kind, **payload):
                if kind == "step-finish":
                    raise OSError("result storage failed")
                return original(journal, kind, **payload)
            with (patch.object(ExecutionJournal, "append", append),
                  patch("gluster_heal_tool.executor._execute_step", return_value=("ok", 0, "done")) as execute):
                execute_apply_results([item], run_dir=root, parallel_nice=0)
            execute.assert_called_once()
            self.assertTrue(any(e.get("kind") == "step-start" for e in records(root)))


if __name__ == "__main__":
    unittest.main()
