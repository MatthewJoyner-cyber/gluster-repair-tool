# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Execution must retain the origin of its evidence, including on CLI bypasses."""
from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gluster_heal_tool import cli
from gluster_heal_tool.models import ApplyActionResult
from gluster_heal_tool.manifest import write_manifest
from gluster_heal_tool.apply_binding import (
    BindingError, bind_manifest, bind_plan, bind_apply, derive_apply_binding,
    validate_apply_binding, validate_plan_context, validate_live_topology,
    volume_identity,
)


VOLUME_INFO = """Volume Name: example
Volume ID: 11111111-2222-4333-8444-555555555555
Type: Replicate
Brick1: node-a:/srv/brick
Brick2: node-b:/srv/brick
"""
BRICKS = [
    {"host": "node-a", "path": "/srv/brick", "role": "data"},
    {"host": "node-b", "path": "/srv/brick", "role": "data"},
]


class ApplyBindingTests(unittest.TestCase):
    @staticmethod
    def manager_module():
        script = Path(__file__).resolve().parents[1] / "gluster-manager.py"
        spec = importlib.util.spec_from_file_location("binding_manager", script)
        manager = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(manager)
        return manager

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = {"schema_version": 1, "objects": {"file": {"gfid": "identity-a"}}}
        bind_manifest(self.manifest, volume="example", bricks=BRICKS,
                      volume_id='11111111-2222-4333-8444-555555555555')
        self.manifest_path = self.root / "manifest.json"
        self.save(self.manifest_path, self.manifest)
        self.plan = {"schema_version": 1, "actions": []}
        bind_plan(self.plan, self.manifest, self.manifest_path)
        self.plan_path = self.root / "plan.json"
        self.save(self.plan_path, self.plan)
        self.apply = {"schema_version": 1, "actions": [], "controller_cycle": {}}
        bind_apply(self.apply, self.plan, self.plan_path)
        self.status = {"volume": "example"}

    def save(self, path, payload):
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_same_volume_retains_evidence_and_plan_fingerprints(self):
        self.assertEqual("example", validate_apply_binding(self.apply, self.status))
        validate_live_topology(self.apply, VOLUME_INFO)
        self.assertEqual({"evidence", "plan"}, set(self.apply["origin_binding"]["sources"]))

    def test_builder_cannot_relabel_plan_from_mutable_status(self):
        with self.assertRaisesRegex(BindingError, "volume"):
            validate_plan_context(self.plan, volume="other", brick_path="/srv/brick")
        with self.assertRaisesRegex(BindingError, "brick"):
            validate_plan_context(self.plan, volume="example", brick_path="/wrong/brick")

    def test_recreated_volume_with_identical_name_and_bricks_is_refused(self):
        info = VOLUME_INFO.replace('11111111-2222-4333-8444-555555555555',
                                   'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee')
        with self.assertRaisesRegex(BindingError, 'volume.*identity|volume.*ID'):
            validate_live_topology(self.apply, info)

    def test_missing_ambiguous_or_invalid_volume_identity_is_refused(self):
        for info in ('Volume Name: example', 'Volume ID: invalid',
                     'Volume ID: 00000000-0000-0000-0000-000000000000',
                     VOLUME_INFO + '\nVolume ID: aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'):
            with self.subTest(info=info), self.assertRaises(BindingError):
                volume_identity(info)

    def test_replaced_brick_or_role_is_not_the_approved_topology(self):
        for info in (
            VOLUME_INFO.replace("node-b", "node-c"),
            VOLUME_INFO.replace("Volume Name: example", "Volume Name: other"),
            VOLUME_INFO.replace("node-b:/srv/brick", "node-b:/srv/replaced"),
            VOLUME_INFO.replace("node-b:/srv/brick", "node-b:/srv/brick (arbiter)"),
            "unavailable",
        ):
            with self.subTest(info=info), self.assertRaises(BindingError):
                validate_live_topology(self.apply, info)

    def test_modified_evidence_plan_target_or_binding_requires_rebuild(self):
        for mutation in ("evidence", "plan", "target", "origin"):
            with self.subTest(mutation=mutation):
                apply = copy.deepcopy(self.apply)
                if mutation == "evidence":
                    self.save(self.manifest_path, {**self.manifest, "objects": {}})
                elif mutation == "plan":
                    self.save(self.plan_path, {**self.plan, "actions": [{"logical_path": "/other"}]})
                elif mutation == "target":
                    apply["actions"] = [{"logical_path": "/other"}]
                else:
                    apply["origin_binding"]["origin"]["volume"] = "other"
                with self.assertRaises(BindingError):
                    validate_apply_binding(apply, self.status)
                self.save(self.manifest_path, self.manifest)
                self.save(self.plan_path, self.plan)

    def test_legacy_is_not_silently_bound(self):
        legacy = {"schema_version": 1, "actions": []}
        with self.assertRaisesRegex(BindingError, "rebuild"):
            validate_apply_binding(legacy, self.status)
        derived = copy.deepcopy(legacy)
        derive_apply_binding(derived, legacy)
        self.assertNotIn("origin_binding", derived)

    def test_continuation_preserves_origin_and_rejects_altered_parent(self):
        derived = {"schema_version": 1, "actions": [{"action_id": "continuation"}]}
        derive_apply_binding(derived, self.apply)
        self.assertEqual("example", validate_apply_binding(derived, self.status))
        self.assertEqual(self.apply["origin_binding"]["sources"], derived["origin_binding"]["sources"])
        self.apply["actions"] = [{"action_id": "unexpected"}]
        with self.assertRaises(BindingError):
            derive_apply_binding(derived, self.apply)

    def test_public_entry_points_refuse_binding_failure_before_health_or_writes(self):
        manager = self.manager_module()
        for module, owner, extra_flags in ((cli, cli, []), (manager, cli, []),
                                           (manager, cli, ["--manage-heal"])):
            for failure in ("volume", "legacy", "target", "topology", "recreated"):
                for skip_health in (False, True):
                    with self.subTest(entry=module.__name__, flags=extra_flags, failure=failure, skip_health=skip_health):
                        payload = copy.deepcopy(self.apply)
                        status = dict(self.status)
                        info = VOLUME_INFO
                        if failure == "volume":
                            status["volume"] = "other"
                        elif failure == "legacy":
                            del payload["origin_binding"]
                        elif failure == "target":
                            payload["actions"] = [{"action_id": "unexpected"}]
                        elif failure == "recreated":
                            info = info.replace('11111111-2222-4333-8444-555555555555',
                                                'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee')
                        else:
                            info = info.replace("node-b", "node-c")
                        apply_path = self.root / "apply.json"
                        status_path = self.root / "status.json"
                        self.save(apply_path, payload)
                        self.save(status_path, status)
                        args = ["apply-run", "--apply-in", str(apply_path), "--status-file", str(status_path),
                                "--execute", "--allow-heal-on", "-y"]
                        if skip_health:
                            args.append("--skip-health-check")
                        args.extend(extra_flags)
                        with (
                            patch.object(owner, "get_volume_info", return_value=info),
                            patch.object(owner, "refresh_manager_health_check") as health,
                            patch.object(owner, "get_heal_settings") as heal_read,
                            patch.object(owner, "set_heal_settings") as heal_write,
                            patch.object(owner, "set_heal_settings_exact") as heal_restore,
                            patch.object(owner, "execute_apply_results") as execute,
                            contextlib.redirect_stdout(io.StringIO()),
                            contextlib.redirect_stderr(io.StringIO()) as stderr,
                        ):
                            self.assertEqual(2, module.main(args))
                        self.assertIn("binding", stderr.getvalue())
                        health.assert_not_called()
                        heal_read.assert_not_called()
                        heal_write.assert_not_called()
                        heal_restore.assert_not_called()
                        execute.assert_not_called()

    def test_changed_facts_during_health_refresh_stop_before_heal_management(self):
        for module in (cli, self.manager_module()):
            for change in ("topology", "status", "evidence"):
                with self.subTest(entry=module.__name__, change=change):
                    apply_path = self.root / "apply.json"
                    status_path = self.root / "status.json"
                    self.save(apply_path, self.apply)
                    self.save(status_path, self.status)
                    self.save(self.manifest_path, self.manifest)

                    def refresh(*args, **kwargs):
                        if change == "evidence":
                            self.save(self.manifest_path, {"objects": {}})
                        status = {"volume": "other"} if change == "status" else dict(self.status)
                        return status, {"summary": {"ready": True}}

                    with (
                        patch.object(cli, "get_volume_info", side_effect=[
                            VOLUME_INFO, VOLUME_INFO.replace("node-b", "node-c") if change == "topology" else VOLUME_INFO,
                        ]),
                        patch.object(cli, "refresh_manager_health_check", side_effect=refresh) as health,
                        patch.object(cli, "get_heal_settings") as heal_read,
                        patch.object(cli, "set_heal_settings") as heal_write,
                        patch.object(cli, "execute_apply_results") as execute,
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()),
                    ):
                        self.assertEqual(2, module.main([
                            "apply-run", "--apply-in", str(apply_path), "--status-file", str(status_path),
                            "--execute", "--allow-heal-on", "-y",
                        ]))
                    health.assert_called_once()
                    self.assertEqual("example", health.call_args.kwargs["volume"])
                    heal_read.assert_not_called()
                    heal_write.assert_not_called()
                    execute.assert_not_called()

    def test_same_volume_different_status_evidence_is_rejected(self):
        newer = self.root / "new-manifest.json"
        self.save(newer, {"objects": {"file": {"gfid": "identity-b"}}})
        with self.assertRaises(BindingError):
            validate_apply_binding(self.apply, {**self.status, "manifest_out": str(newer)})

    def test_real_build_writers_preserve_origin_and_refuse_relabel(self):
        for module in (cli, self.manager_module()):
            with self.subTest(entry=module.__name__):
                write_manifest(self.manifest_path, {}, volume="example", bricks=BRICKS,
                               volume_id='11111111-2222-4333-8444-555555555555')
                status_path = self.root / "status.json"
                apply_path = self.root / "built-apply.json"
                self.save(status_path, self.status)
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(0, module.main([
                        "plan-build", "--manifest-in", str(self.manifest_path), "--plan-out", str(self.plan_path),
                        "--mountpoint", "/example", "--status-file", str(status_path),
                    ]))
                    build_args = ["apply-build", "--plan-in", str(self.plan_path), "--apply-out", str(apply_path),
                                  "--status-file", str(status_path)]
                    self.assertEqual(0, module.main(build_args))
                    payload = json.loads(apply_path.read_text())
                    self.assertEqual("example", validate_apply_binding(payload, json.loads(status_path.read_text())))
                    self.save(status_path, {"volume": "other"})
                    self.assertEqual(2, module.main(build_args))
                    self.assertEqual(payload, json.loads(apply_path.read_text()))

    def test_bare_observation_cache_cannot_acquire_a_new_volume_origin(self):
        status_path = self.root / "status.json"
        with (
            patch.object(cli, "resolve_heal_snapshot", return_value="cached-heal.txt"),
            patch.object(cli, "parse_heal_info_file", return_value=[object()]),
            patch.object(cli.CachedResolver, "from_file"),
            patch.object(cli, "get_volume_info", return_value=VOLUME_INFO),
            patch.object(cli, "build_manifest", return_value=({}, [])),
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(0, cli.main([
                "manifest-build", "--volume", "example", "--hosts", "node-a,node-b",
                "--brick-path", "/srv/brick", "--mountpoint", "/example",
                "--observations-in", "bare-observations.json", "--heal-file", "cached-heal.txt",
                "--manifest-out", str(self.manifest_path), "--status-file", str(status_path),
            ]))
        self.assertNotIn("origin_binding", json.loads(self.manifest_path.read_text()))

    def test_valid_filtered_execution_uses_original_snapshot(self):
        manager = self.manager_module()
        for module in (cli, manager):
            with self.subTest(entry=module.__name__):
                payload = copy.deepcopy(self.apply)
                payload["actions"] = [ApplyActionResult(
                    action_id=name, logical_path=f"/{name}", action_type="repair_file",
                    execution_mode="dry-run", backup_root="", backup_mode="none", batch=True, status="planned",
                    notes=["repair strategy: restore_missing_replica"],
                ).to_dict() for name in ("first", "second")]
                bind_apply(payload, self.plan, self.plan_path)
                apply_path = self.root / "apply.json"
                status_path = self.root / "status.json"
                self.save(apply_path, payload)
                self.save(status_path, self.status)
                with (
                    patch.object(cli, "get_volume_info", return_value=VOLUME_INFO) as topology,
                    patch.object(cli, "get_heal_settings", return_value={}),
                    patch.object(cli, "set_heal_settings") as heal_write,
                    patch.object(cli, "_refresh_post_execute_heal_preserving_settings", return_value=({}, {})),
                    patch.object(cli, "execute_apply_results", return_value={"actions": [], "summary": {}}) as execute,
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as stderr,
                ):
                    rc = module.main([
                        "apply-run", "--apply-in", str(apply_path), "--status-file", str(status_path),
                        "--run-dir", str(self.root / "run"), "--execute", "--skip-health-check",
                        "--allow-heal-on", "--path-contains", "first", "-y",
                    ])
                    self.assertEqual(0, rc, stderr.getvalue())
                topology.assert_called_once_with("example")
                execute.assert_called_once()
                self.assertEqual(["first"], [result.action_id for result in execute.call_args.args[0]])
                heal_write.assert_not_called()
                self.assertEqual(payload, json.loads(apply_path.read_text()))


if __name__ == "__main__":
    unittest.main()
