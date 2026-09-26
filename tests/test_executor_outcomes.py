# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Command failures must never grant authority for subsequent destructive work."""
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from gluster_heal_tool.executor import _execute_step, execute_apply_results
from gluster_heal_tool.models import ApplyStep
from tests.test_execution_dependencies import action


PATH = "/fixture/item"


def resolver(mode="latest-mtime"):
    arguments = ["host-a:/srv/brick", PATH] if mode == "source-brick" else [PATH]
    return ApplyStep(step_id="resolve", step_type="resolve_split_brain_gluster_cli",
                     command_preview=["sudo", "-n", "gluster", "volume", "heal", "testvol",
                                      "split-brain", mode, *arguments])


class ExecutorOutcomeTests(unittest.TestCase):
    def setUp(self):
        # These cases exercise response handling after compatibility admission.
        admitted = patch("gluster_heal_tool.executor.require_execution_features")
        admitted.start()
        self.addCleanup(admitted.stop)

    def test_operational_resolver_failures_stop_fallback_and_dependents(self):
        cases = [(255, "", "Connection lost after dispatch"),
                 (1, "", "Permission denied"), (1, "", "unrecognized resolver failure"),
                 (127, "", "gluster: command not found"), (-15, "", ""),
                 (1, "", "Transport endpoint is not connected"),
                 (0, "", ""), (0, "unexpected response", ""),
                 (0, f"Healed {PATH}.", "Permission denied"),
                 (1, f"Healing {PATH} failed: File not in split-brain.", "Connection lost"),
                 (255, f"Healing {PATH} failed: File not in split-brain.", ""),
                 (0, f"No difference in mtime for file {PATH}", "unrecognized diagnostic"),
                 (0, "Healed /fixture/other.", ""),
                 (0, f"Lookup failed on {PATH}:Input/output error.", ""),
                 subprocess.TimeoutExpired(["fixture-resolver"], 1),
                 OSError("resolver response lost")]
        for case in cases:
            for workers in (1, 2):
                with self.subTest(case=case, workers=workers):
                    parent = action("parent")
                    parent.steps = [resolver(), resolver("source-brick"), parent.steps[0]]
                    items = [parent, action("peer"), action("child", 1, ["parent"])]

                    def command(argv):
                        if argv == ["fixture-only"]:
                            return subprocess.CompletedProcess(argv, 0, "", "")
                        self.assertEqual(parent.steps[0].command_preview, argv)
                        if isinstance(case, Exception):
                            raise case
                        return subprocess.CompletedProcess(argv, *case)

                    with (patch("gluster_heal_tool.executor._run_command", side_effect=command) as run,
                          patch("gluster_heal_tool.executor.os.cpu_count", return_value=8)):
                        report = execute_apply_results(items, keep_going=True, parallel_actions=workers,
                                                       parallel_nice=0)
                    self.assertEqual("unknown", parent.status)
                    self.assertEqual("unknown", parent.steps[0].status)
                    self.assertEqual("completed", items[1].status)
                    self.assertEqual("blocked", items[2].status)
                    self.assertEqual(2, run.call_count)  # resolver and independent peer only
                    self.assertEqual(1, report["summary"]["unknown_actions"])

    def test_recognized_native_success_and_already_resolved_skip_fallback(self):
        for mode in ("latest-mtime", "source-brick"):
            for rc, message in ((0, f"Healed {PATH}."),
                                (0, f"GFID split-brain resolved for file {PATH}"),
                                (1, f"Healing {PATH} failed: File not in split-brain.")):
                with self.subTest(mode=mode, rc=rc, message=message):
                    parent = action("parent")
                    parent.steps = [resolver(mode), parent.steps[0]]
                    with patch("gluster_heal_tool.executor._run_command",
                               return_value=subprocess.CompletedProcess([], rc, message, "")) as run:
                        execute_apply_results([parent], parallel_nice=0)
                    run.assert_called_once()
                    self.assertEqual("ok", parent.steps[0].status)
                    self.assertEqual("skipped", parent.steps[1].status)

    def test_recognized_tie_allows_only_planned_followup(self):
        for rc, message in ((1, f"Healing {PATH} failed: No difference in mtime."),
                            (0, f"No difference in mtime for file {PATH}"),
                            (0, f"Lookup failed on {PATH}:Input/output error.\n"
                                f"No difference in mtime for file {PATH}")):
            with self.subTest(rc=rc, message=message):
                parent = action("parent")
                parent.steps = [resolver(), resolver("source-brick"), parent.steps[0]]
                with patch("gluster_heal_tool.executor._run_command", side_effect=[
                    subprocess.CompletedProcess([], rc, message, ""),
                    subprocess.CompletedProcess([], 0, f"Healed {PATH}.", ""),
                ]) as run:
                    execute_apply_results([parent], parallel_nice=0)
                self.assertEqual(2, run.call_count)
                self.assertEqual(["ok", "ok", "skipped"], [s.status for s in parent.steps])

    def test_source_brick_tie_and_unrelated_or_mixed_tie_are_unknown(self):
        for step, message in (
            (resolver("source-brick"), f"Healing {PATH} failed: No difference in mtime."),
            (resolver(), "Healing /fixture/other failed: No difference in mtime."),
            (resolver(), "No difference in mtime"),
            (resolver(), f"Lookup failed on /fixture/other:Input/output error.\n"
                         f"No difference in mtime for file {PATH}"),
            (resolver(), f"Lookup failed on {PATH}:Transport endpoint is not connected.\n"
                         f"No difference in mtime for file {PATH}"),
            (resolver(), f"Healed {PATH}.\nHealing {PATH} failed: No difference in mtime."),
        ):
            with self.subTest(message=message):
                with patch("gluster_heal_tool.executor._run_command",
                           return_value=subprocess.CompletedProcess([], 0, message, "")):
                    self.assertEqual("unknown", _execute_step(step)[0])

    def test_nonzero_mutations_do_not_tolerate_missing_or_operational_errors(self):
        types = ["mkdir_mount_parent", "ensure_directory_via_mount", "mkdir_directory_backend",
                 "mkdir_directory_backend_child_gap", "attach_directory_gfid", "attach_file_gfid",
                 "attach_directory_mdata", "quarantine_directory_backend", "quarantine_directory_gfid",
                 "quarantine_file_backend", "quarantine_file_gfid", "backup_stale_backend",
                 "restore_via_mount", "restore_via_mount_child_gap", "restore_file_backend_gap_fill",
                 "restore_file_backend_backfill", "remove_restored_mount_file", "remove_stale_backend"]
        for step_type in types:
            for error in ("Input/output error", "Transport endpoint is not connected", "Stale file handle",
                          "cannot stat: Permission denied", "command not found", "No such file or directory",
                          "No such file or directory\nPermission denied"):
                with self.subTest(step_type=step_type, error=error):
                    step = ApplyStep(step_id="mutation", step_type=step_type, host="host-a",
                                     source_path="11111111-2222-3333-4444-555555555555",
                                     target_path=PATH, tolerate_missing=True, command_preview=["fixture-only"])
                    with patch("gluster_heal_tool.executor._run_command",
                               return_value=subprocess.CompletedProcess([], 1, "", error)) as run:
                        self.assertEqual("failed", _execute_step(step)[0])
                    run.assert_called_once()

    def test_only_explicit_missing_removal_target_is_tolerated(self):
        step = ApplyStep(step_id="remove", step_type="remove_stale_backend", target_path=PATH,
                         tolerate_missing=True, command_preview=["rm", "-f", "--", PATH])
        message = f"rm: cannot remove '{PATH}': No such file or directory"
        for rc, output, error, allowed in ((1, "", message, True), (255, "", message, False),
                                          (1, "Permission denied", message, False),
                                          (1, "", message.replace(PATH, "/fixture/other"), False)):
            with self.subTest(rc=rc, output=output, error=error):
                with patch("gluster_heal_tool.executor._run_command",
                           return_value=subprocess.CompletedProcess([], rc, output, error)):
                    self.assertEqual("skipped" if allowed else "failed", _execute_step(step)[0])
        step.tolerate_missing = False
        with patch("gluster_heal_tool.executor._run_command",
                   return_value=subprocess.CompletedProcess([], 1, "", message)):
            self.assertEqual("failed", _execute_step(step)[0])

    def test_backup_copy_failure_after_parent_creation_stops_removal(self):
        parent = action("parent")
        backup = ApplyStep(step_id="backup", step_type="backup_stale_backend", host="host-a",
                           source_path="/fixture/source", target_path=PATH, tolerate_missing=True,
                           command_preview=["fixture-copy"])
        parent.steps.insert(0, backup)
        with patch("gluster_heal_tool.executor._run_command", side_effect=[
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 1, "copy started", "No such file or directory"),
        ]) as run:
            execute_apply_results([parent], keep_going=True, parallel_nice=0)
        self.assertEqual(2, run.call_count)
        self.assertEqual("failed", parent.status)
        self.assertIn("copy started", backup.message)
        self.assertIn("No such file or directory", backup.message)
        self.assertEqual("planned", parent.steps[1].status)

    def test_staging_failure_and_unflagged_quarantine_missing_are_not_skips(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind in ("stage_directory_subtree_local", "quarantine_file_backend"):
                step = ApplyStep(step_id="move", step_type=kind, host="host-a",
                                 source_path="/fixture/source", target_path=f"{directory}/copy",
                                 command_preview=["fixture-only"], tolerate_missing=False)
                with self.subTest(kind=kind), patch("gluster_heal_tool.executor._run_command",
                    return_value=subprocess.CompletedProcess([], 1, "", "No such file or directory")):
                    self.assertEqual("failed", _execute_step(step)[0])


if __name__ == "__main__":
    unittest.main()
