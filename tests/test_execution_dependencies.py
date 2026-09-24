# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Dependency outcomes must control actual dispatch, including keep-going."""
import json
import copy
import io
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gluster_heal_tool.executor import execute_apply_results, validate_execute_results
from gluster_heal_tool.models import ApplyActionResult, ApplyStep
from gluster_heal_tool import cli
from gluster_heal_tool.status import merge_write_history
from tests.test_reversibility_gate import _bind_test_payload, _BOUND_VOLUME_INFO, _load_manager_module


def action(name, wave=0, dependencies=(), status="planned"):
    return ApplyActionResult(
        action_id=name, logical_path=f"/{name}", action_type="cleanup_orphaned_symlink",
        execution_mode="dry-run", backup_root="", backup_mode="none", batch=True,
        status=status, execution_wave=wave, depends_on=list(dependencies), parallel_safe=True,
        notes=["repair strategy: delete_orphaned_symlink_residue"],
        steps=[ApplyStep(step_id=name, step_type="remove_stale_backend", command_preview=["fixture-only"])],
    )


class ExecutionDependencyTests(unittest.TestCase):
    def run_chain(self, workers, parent_outcome, *, keep_going=True, stage_only=False):
        results = [action("parent"), action("peer"), action("child", 1, ["parent"]),
                   action("independent", 1), action("grandchild", 2, ["child"])]
        self.assertEqual((True, []), validate_execute_results(results))
        calls = []

        def step(item):
            calls.append(item.step_id)
            if item.step_id == "parent":
                if isinstance(parent_outcome, BaseException):
                    raise parent_outcome
                return parent_outcome, 0 if parent_outcome == "ok" else 1, "injected parent outcome"
            return "ok", 0, ""

        with tempfile.TemporaryDirectory() as directory:
            with (patch("gluster_heal_tool.executor._execute_step", side_effect=step),
                  patch("gluster_heal_tool.executor.os.cpu_count", return_value=8)):
                if isinstance(parent_outcome, KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        execute_apply_results(results, keep_going=keep_going, parallel_actions=workers,
                                              parallel_nice=0, run_dir=directory)
                    report = json.loads((Path(directory) / "execute.json").read_text())
                else:
                    report = execute_apply_results(results, keep_going=keep_going, parallel_actions=workers,
                                                   parallel_nice=0, stage_only=stage_only, run_dir=directory)
            records = [json.loads(path.read_text())["action"] for path in (Path(directory) / "actions").glob("*.json")]
            self.assertEqual(5, len(records))
        return report, calls

    def test_failed_parent_blocks_transitive_dependents_but_independent_work_continues(self):
        for workers in (1, 3):
            with self.subTest(workers=workers):
                report, calls = self.run_chain(workers, "failed")
                self.assertEqual({"parent", "peer", "independent"}, set(calls))
                rows = {row["action_id"]: row for row in report["actions"]}
                for child, parent in (("child", "parent"), ("grandchild", "child")):
                    self.assertEqual("blocked", rows[child]["status"])
                    self.assertIn(parent, " ".join(rows[child]["notes"]))
                    self.assertEqual("not-run", rows[child]["steps"][0]["status"])
                self.assertEqual(2, report["summary"]["blocked_actions"])
                self.assertEqual(3, report["summary"]["executed_actions"])

    def test_skipped_and_unknown_prerequisites_are_not_success(self):
        for workers in (1, 3):
            for outcome in ("skipped", "unknown", OSError("lost worker result")):
                with self.subTest(workers=workers, outcome=str(outcome)):
                    report, calls = self.run_chain(workers, outcome)
                    self.assertNotIn("child", calls)
                    self.assertNotIn("grandchild", calls)
                    self.assertIn("independent", calls)
                    self.assertEqual(2, report["summary"]["blocked_actions"])

    def test_completed_prerequisite_allows_chain_in_both_execution_modes(self):
        for workers in (1, 3):
            with self.subTest(workers=workers):
                report, calls = self.run_chain(workers, "ok")
                self.assertEqual(5, len(calls))
                self.assertLess(calls.index("parent"), calls.index("child"))
                self.assertLess(calls.index("child"), calls.index("grandchild"))
                self.assertEqual(5, report["summary"]["completed_actions"])

    def test_stop_on_failure_still_records_blocked_descendants(self):
        for workers in (1, 3):
            with self.subTest(workers=workers):
                report, calls = self.run_chain(workers, "failed", keep_going=False)
                self.assertNotIn("independent", calls)
                self.assertNotIn("child", calls)
                self.assertEqual(2, report["summary"]["blocked_actions"])

    def test_interruption_blocks_descendants_and_stops_new_waves(self):
        for workers in (1, 3):
            with self.subTest(workers=workers):
                report, calls = self.run_chain(workers, KeyboardInterrupt())
                self.assertNotIn("child", calls)
                self.assertNotIn("independent", calls)
                self.assertEqual(2, report["summary"]["blocked_actions"])
                self.assertEqual(1, report["summary"]["unknown_actions"])

    def test_stage_only_does_not_satisfy_repair_dependencies(self):
        report, calls = self.run_chain(3, "ok", stage_only=True)
        self.assertEqual([], calls)
        self.assertEqual(2, report["summary"]["blocked_actions"])

    def test_scheduler_interrupt_joins_dispatched_workers_without_starting_next_wave(self):
        results = [action("parent"), action("peer"), action("child", 1, ["parent"])]
        with tempfile.TemporaryDirectory() as directory:
            with (patch("gluster_heal_tool.executor._execute_step", return_value=("ok", 0, "")) as step,
                  patch("gluster_heal_tool.executor.os.cpu_count", return_value=8),
                  patch("gluster_heal_tool.executor.as_completed", side_effect=KeyboardInterrupt())):
                with self.assertRaises(KeyboardInterrupt):
                    execute_apply_results(results, keep_going=True, parallel_actions=2,
                                          parallel_nice=0, run_dir=directory)
            self.assertNotIn("child", [call.args[0].step_id for call in step.call_args_list])
            report = json.loads((Path(directory) / "execute.json").read_text())
            self.assertTrue(report["interrupted"])
            self.assertNotEqual("completed", report["actions"][2]["status"])

    def test_invalid_graph_and_old_outcomes_cannot_authorize_execution(self):
        cases = [
            [action("child", 1, ["missing"])],
            [action("parent"), action("child", 0, ["parent"])],
            [action("child", 0, ["parent"]), action("parent", 1)],
            [action("parent"), action("parent"), action("child", 1, ["parent"])],
            [action("child", 0, ["child"])],
        ]
        cases.extend([action("parent", status=status), action("child", 1, ["parent"])]
                     for status in ("unknown", "running", "completed", "failed", "skipped"))
        for fixture in cases:
            for workers in (1, 3):
                results = copy.deepcopy(fixture)
                with self.subTest(ids=[r.action_id for r in results], states=[r.status for r in results], workers=workers):
                    valid, errors = validate_execute_results(results)
                    self.assertFalse(valid)
                    self.assertTrue(errors)
                    with patch("gluster_heal_tool.executor._execute_step", return_value=("ok", 0, "")) as step:
                        execute_apply_results(results, keep_going=True, parallel_actions=workers, parallel_nice=0)
                    self.assertNotIn("child", [call.args[0].step_id for call in step.call_args_list])

    def test_every_prerequisite_must_succeed(self):
        results = [action("left"), action("right"), action("child", 1, ["left", "right"])]
        with patch("gluster_heal_tool.executor._execute_step", side_effect=lambda step: (
            ("failed", 1, "injected") if step.step_id == "right" else ("ok", 0, "")
        )) as execute:
            report = execute_apply_results(results, keep_going=True, parallel_nice=0)
        self.assertEqual(["left", "right"], [call.args[0].step_id for call in execute.call_args_list])
        self.assertEqual("blocked", report["actions"][2]["status"])

    def test_public_commands_do_not_report_success_or_launch_heal_after_blocked_execution(self):
        for entry in (cli, _load_manager_module()):
            for outcome in ("failed", "skipped", "unknown"):
                with self.subTest(entry=entry.__name__, outcome=outcome), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    payload = {"actions": [item.to_dict() for item in (
                        action("parent"), action("child", 1, ["parent"]), action("independent", 1),
                    )]}
                    _bind_test_payload(root, payload)
                    apply_path, status_path = root / "apply.json", root / "status.json"
                    apply_path.write_text(json.dumps(payload))
                    status_path.write_text(json.dumps({"volume": "gtest"}))
                    with (
                        # This test isolates dependency outcomes after admission;
                        # freshness refusal is exercised by the gate tests.
                        patch('gluster_heal_tool.execution_freshness.validate_live_evidence'),
                        patch.object(cli, "get_volume_info", return_value=_BOUND_VOLUME_INFO),
                        patch.object(cli, "get_heal_settings", return_value={}),
                        patch.object(cli, "_refresh_post_execute_heal_preserving_settings") as heal,
                        patch.object(cli, "manage_backup_artifacts") as backups,
                        patch("gluster_heal_tool.executor._execute_step", side_effect=lambda step: (
                            (outcome, 1, "injected") if step.step_id == "parent" else ("ok", 0, "")
                        )) as execute,
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as stderr,
                    ):
                        code = entry.main([
                            "apply-run", "--execute", "--batch", "--keep-going", "--skip-health-check",
                            "--allow-heal-on", "--cleanup-backups", "--apply-in", str(apply_path),
                            "--status-file", str(status_path), "--run-dir", str(root / "run"),
                        ])
                    self.assertEqual(2, code)
                    self.assertTrue(execute.called, stderr.getvalue())
                    self.assertNotIn("child", [call.args[0].step_id for call in execute.call_args_list])
                    self.assertTrue(json.loads(status_path.read_text())["completion_blocked"])
                    heal.assert_not_called()
                    backups.assert_not_called()

    def test_unknown_summary_is_not_downgraded_to_completed_write_history(self):
        history = merge_write_history({"summary": {"executed_actions": 2, "completed_actions": 1, "unknown_actions": 1}})
        self.assertEqual("unknown", history["execution_write_state"])
        self.assertTrue(history["write_outcome_unknown"])


if __name__ == "__main__":
    unittest.main()
