# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Exercise public execution gates with real artifacts and mocked cluster I/O."""
from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from gluster_heal_tool import cli
from gluster_heal_tool.models import ApplyActionResult
from tests.test_reversibility_gate import _bind_test_payload, _BOUND_VOLUME_INFO, _load_manager_module


class ApplyRunGateTests(unittest.TestCase):
    def setUp(self):
        self.manager = _load_manager_module()

    def run_case(self, entry, flags=(), *, gate="", outcome="success", managed=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = {"volume": "gtest"}
            stop = {"controller_stop": True, "controller_reason": "rebuild the plan"}
            if gate == "controller":
                status["controller_cycle"] = stop
            if gate == "snapshot":
                status["reversibility"] = {"snapshot_required": True, "snapshot_acknowledged": False}
            result = ApplyActionResult(
                action_id="repair-a", logical_path="/example/file", action_type="repair_file",
                execution_mode="dry-run", backup_root="", backup_mode="none", batch=False,
                status="planned", notes=["repair strategy: restore_missing_replica"],
            )
            if gate == "invalid-action":
                result.notes = []
            payload = {"schema_version": 1, "actions": [result.to_dict()],
                       "controller_cycle": stop if gate == "artifact-controller" else {}}
            _bind_test_payload(root, payload)
            apply_path = root / "apply.json"
            status_path = root / "status.json"
            apply_path.write_text(json.dumps(payload), encoding="utf-8")
            status_path.write_text(json.dumps(status), encoding="utf-8")
            args = ["apply-run", "--apply-in", str(apply_path), "--status-file", str(status_path),
                    "--run-dir", str(root / "run"), "--execute", *flags]
            if gate != "authorization":
                args.append("--batch")
            if gate != "health":
                args.append("--skip-health-check")
            if managed:
                args.append("--manage-heal")
            else:
                args.append("--allow-heal-on")
            original = {"cluster." + name: "on" if managed else "off" for name in (
                "self-heal-daemon", "data-self-heal", "metadata-self-heal", "entry-self-heal",
            )}
            live = dict(original)
            events = []

            def set_heal(volume, *, enabled):
                events.append("enable" if enabled else "disable")
                live.update({key: "on" if enabled else "off" for key in live})
                return dict(live)

            def restore(volume, settings):
                events.append("restore")
                if outcome == "restore-failure":
                    raise RuntimeError("restore verification failed")
                live.update(settings)
                return dict(live)

            def execute(*args, **kwargs):
                events.append("execute")
                if outcome == "interrupt":
                    raise KeyboardInterrupt()
                return {"actions": [], "summary": {"completed_actions": 1, "failed_actions": 0}}

            executor = Mock(side_effect=execute)
            refresh = Mock(return_value=({}, {}))
            health = Mock(return_value=(status, {"summary": {"ready": gate != "health"}}))
            setters = Mock(side_effect=set_heal)
            restorer = Mock(side_effect=restore)
            verification = Mock(return_value={"available": True, "summary": {"paths_ok": 1}})
            with ExitStack() as stack:
                # Also guard the old manager boundary so this test can reproduce
                # the regression without allowing a real command to escape.
                for module in (cli, self.manager):
                    for name, mock in {
                        "get_volume_info": Mock(return_value=_BOUND_VOLUME_INFO),
                        "get_heal_settings": Mock(side_effect=lambda volume: dict(live)),
                        "set_heal_settings": setters,
                        "set_heal_settings_exact": restorer,
                        "refresh_manager_health_check": health,
                        "execute_apply_results": executor,
                        "_refresh_post_execute_heal": refresh,
                        "_verify_post_execute_temp_mount": verification,
                    }.items():
                        stack.enter_context(patch.object(module, name, mock, create=True))
                stack.enter_context(redirect_stdout(io.StringIO()))
                stderr = stack.enter_context(redirect_stderr(io.StringIO()))
                code = entry.main(args)
            return (code, executor, setters, restorer, refresh, verification,
                    json.loads(status_path.read_text()), events, live, original, stderr.getvalue())

    def test_refused_public_forms_never_execute_or_change_heal(self):
        for entry in (cli, self.manager):
            for flags in ((), ("--execute-ready",), ("--require-snapshot", "--snapshot-ack")):
                for gate in ("controller", "artifact-controller", "health", "authorization", "invalid-action"):
                    with self.subTest(entry=entry.__name__, flags=flags, gate=gate):
                        code, executor, setter, restorer, refresh, *_ = self.run_case(entry, flags, gate=gate, managed=True)
                        self.assertEqual(2, code)
                        executor.assert_not_called()
                        setter.assert_not_called()
                        restorer.assert_not_called()
                        refresh.assert_not_called()
            for gate in ("controller", "artifact-controller", "snapshot", "health", "authorization", "invalid-action"):
                with self.subTest(entry=entry.__name__, gate=gate, form="plain"):
                    code, executor, setter, restorer, refresh, *_ = self.run_case(entry, gate=gate)
                    self.assertEqual(2, code)
                    executor.assert_not_called()
                    setter.assert_not_called()
                    restorer.assert_not_called()
                    refresh.assert_not_called()

    def test_snapshot_requirement_also_applies_to_ready_and_managed_forms(self):
        for entry in (cli, self.manager):
            for flags in ((), ("--execute-ready",), ("--require-snapshot",)):
                with self.subTest(entry=entry.__name__, flags=flags):
                    code, executor, setter, *_ = self.run_case(entry, flags, gate="snapshot", managed=True)
                    self.assertEqual(2, code)
                    executor.assert_not_called()
                    setter.assert_not_called()

    def test_success_and_verification_use_one_execution_and_preserve_heal(self):
        for entry in (cli, self.manager):
            for managed in (False, True):
                with self.subTest(entry=entry.__name__, managed=managed):
                    code, executor, _, _, refresh, verify, status, events, live, original, stderr = self.run_case(
                        entry, ("--verify-temp-mount",), managed=managed,
                    )
                    self.assertEqual(0, code, stderr)
                    executor.assert_called_once()
                    refresh.assert_called_once()
                    verify.assert_called_once()
                    self.assertEqual(original, live)
                    self.assertEqual("apply-run-execute", status["phase"])
                    self.assertIn("restore", events)

    def test_interrupt_records_unknown_state_and_restores_managed_heal(self):
        for entry in (cli, self.manager):
            for managed in (False, True):
                with self.subTest(entry=entry.__name__, managed=managed):
                    code, executor, _, _, refresh, verify, status, _, live, original, _ = self.run_case(
                        entry, outcome="interrupt", managed=managed,
                    )
                    self.assertEqual(130, code)
                    executor.assert_called_once()
                    refresh.assert_not_called()
                    verify.assert_not_called()
                    self.assertEqual("unknown", status["execution_write_state"])
                    self.assertEqual(original, live)

    def test_restore_failure_blocks_completion_for_both_entry_points(self):
        for entry in (cli, self.manager):
            for managed in (False, True):
                with self.subTest(entry=entry.__name__, managed=managed):
                    code, executor, _, _, _, verify, status, *_ = self.run_case(
                        entry, outcome="restore-failure", managed=managed,
                    )
                    self.assertEqual(2, code)
                    executor.assert_called_once()
                    verify.assert_not_called()
                    self.assertTrue(status["completion_blocked"])
                    self.assertTrue(status["heal_restore_required"])


if __name__ == "__main__":
    unittest.main()
