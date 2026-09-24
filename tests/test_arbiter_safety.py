# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Focused tests for arbiter role and payload-source safety."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from gluster_heal_tool.apply import build_apply_results
from gluster_heal_tool.apply import load_apply_results, write_apply_results
from gluster_heal_tool.executor import validate_execute_results
from gluster_heal_tool.models import ManifestObject, PlanAction, ResolutionObservation
from gluster_heal_tool.planner import build_plan
from gluster_heal_tool.planner_report import render_plan_summary, summarize_plan
from gluster_heal_tool.role_safety import role_evidence_error, role_for_host


ROLES = {"data-a": "data", "data-b": "data", "arbiter": "arbiter"}


def _file_observation(host: str, gfid: str, mtime: int) -> ResolutionObservation:
    backend = f"/brick/{host}/alpha/payload"
    return ResolutionObservation(
        host=host,
        raw_entry="/alpha/payload",
        gfid=gfid,
        file_gfid=gfid,
        backend=backend,
        relpath="alpha/payload",
        mounted="/alpha/payload",
        mounted_checked=True,
        mounted_lexists=True,
        mounted_exists=True,
        mounted_lstat_type="file",
        backend_lexists=True,
        backend_exists=True,
        backend_mtime=mtime,
        backend_size=10,
        backend_lstat_type="file",
        backend_trusted_gfid=gfid,
    )


def _directory_observation(host: str, gfid: str, mtime: int) -> ResolutionObservation:
    return ResolutionObservation(
        host=host,
        raw_entry="/alpha/directory",
        gfid=gfid,
        backend=f"/brick/{host}/alpha/directory",
        relpath="alpha/directory",
        mounted="/alpha/directory",
        mounted_checked=True,
        mounted_lexists=True,
        mounted_exists=True,
        mounted_lstat_type="dir",
        backend_lexists=True,
        backend_exists=True,
        backend_mtime=mtime,
        backend_lstat_type="dir",
        backend_trusted_gfid=gfid,
    )


def _file_action(*, winner_host: str, roles: dict[str, str] | None, required: bool = True) -> dict[str, object]:
    return PlanAction(
        action_id="repair:alpha/payload",
        logical_path="alpha/payload",
        action_type="repair_file",
        object_type="file",
        depth=2,
        repair_strategy="restore_missing_replica",
        winner_host=winner_host,
        winner_backend="/gluster/test/alpha/payload",
        stage_local_path="/tmp/gluster-stage/alpha/payload",
        mounted_target="/gtest/alpha/payload",
        brick_roles_by_host=roles or {},
        brick_role_evidence_required=required,
    ).to_dict()


class ArbiterSafetyTests(unittest.TestCase):
    def test_known_aliases_preserve_roles_and_ambiguous_aliases_fail_closed(self) -> None:
        aliases = {
            "data-a": ["data-a.lab.example", "192.0.2.10"],
            "data-b": ["data-b.lab.example", "192.0.2.11"],
            "arbiter": ["arbiter.lab.example", "192.0.2.12"],
        }

        self.assertEqual("data", role_for_host(ROLES, "data-a.lab.example", aliases))
        self.assertEqual("arbiter", role_for_host(ROLES, "192.0.2.12", aliases))
        self.assertEqual(
            "",
            role_evidence_error(
                ROLES,
                required=True,
                candidate_hosts=["data-a.lab.example", "arbiter.lab.example"],
                brick_host_aliases=aliases,
            ),
        )

        ambiguous = {"data-a": ["shared.lab"], "data-b": ["shared.lab"]}
        self.assertEqual("", role_for_host(ROLES, "shared.lab", ambiguous))
        self.assertIn(
            "stale or incomplete",
            role_evidence_error(
                ROLES,
                required=True,
                candidate_hosts=["shared.lab"],
                brick_host_aliases=ambiguous,
            ),
        )

    def test_alias_observations_keep_arbiter_role_safety_through_apply(self) -> None:
        gfid = "a" * 36
        aliases = {
            "data-a": ["data-a.lab.example", "192.0.2.10"],
            "data-b": ["data-b.lab.example", "192.0.2.11"],
            "arbiter": ["arbiter.lab.example", "192.0.2.12"],
        }
        manifest = {
            "alpha/payload": ManifestObject(
                logical_path="alpha/payload",
                object_type="file",
                depth=2,
                source_hosts=["localhost"],
                observations={
                    "data-a.lab.example": [_file_observation("data-a.lab.example", gfid, 10)],
                    "data-b.lab.example": [_file_observation("data-b.lab.example", gfid, 11)],
                    "arbiter.lab.example": [_file_observation("arbiter.lab.example", gfid, 999)],
                },
                brick_roles_by_host=ROLES,
                brick_host_aliases=aliases,
                brick_role_evidence_required=True,
            )
        }
        manifest["alpha/payload"].observations["data-a.lab.example"][0].backend_exists = False
        manifest["alpha/payload"].observations["data-a.lab.example"][0].backend_lexists = False

        action = build_plan(manifest, mountpoint="/gtest")[0]
        result = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            batch=True,
        )[0]

        self.assertEqual("data-b.lab.example", action.winner_host)
        self.assertEqual(["data-a.lab.example"], action.missing_hosts)
        self.assertEqual(aliases, action.brick_host_aliases)
        self.assertNotEqual("blocked", result.status)
        self.assertEqual(aliases, result.brick_host_aliases)
        self.assertEqual((True, []), validate_execute_results([result]))
        self.assertNotIn("arbiter.lab.example", {step.source_host for step in result.steps})

        with TemporaryDirectory() as tmpdir:
            apply_path = Path(tmpdir) / "apply.json"
            write_apply_results(apply_path, [result])
            resumed = load_apply_results(apply_path)

        self.assertEqual(aliases, resumed[0].brick_host_aliases)
        self.assertEqual((True, []), validate_execute_results(resumed))

    def test_absent_operator_path_does_not_create_backend_cleanup(self) -> None:
        missing = ResolutionObservation(
            host="data-a",
            raw_entry="/alpha/missing",
            backend="/brick/data-a/alpha/missing",
            relpath="alpha/missing",
            mounted="/gtest/alpha/missing",
            backend_lexists=False,
            backend_exists=False,
        )
        manifest = {
            "alpha/missing": ManifestObject(
                logical_path="alpha/missing",
                object_type="unknown",
                depth=2,
                input_source="operator_path",
                raw_entries=["/alpha/missing"],
                observations={
                    "data-a": [missing],
                    "data-b": [
                        ResolutionObservation(
                            **{**missing.to_dict(), "host": "data-b", "backend": "/brick/data-b/alpha/missing"}
                        )
                    ],
                    "arbiter": [
                        ResolutionObservation(
                            **{**missing.to_dict(), "host": "arbiter", "backend": "/brick/arbiter/alpha/missing"}
                        )
                    ],
                },
                brick_roles_by_host=ROLES,
                brick_role_evidence_required=True,
            )
        }

        action = build_plan(manifest, mountpoint="/gtest")[0]

        self.assertEqual("review_unclassified_authority", action.action_type)
        self.assertEqual([], action.stale_backends)
        self.assertNotEqual("safe_default", action.decision_class)

    def test_planner_excludes_arbiter_from_file_candidates(self) -> None:
        manifest = {
            "alpha/payload": ManifestObject(
                logical_path="alpha/payload",
                object_type="file",
                depth=2,
                source_hosts=list(ROLES),
                observations={
                    "data-a": [_file_observation("data-a", "a" * 36, 10)],
                    "data-b": [_file_observation("data-b", "a" * 36, 11)],
                    "arbiter": [_file_observation("arbiter", "b" * 36, 999)],
                },
                brick_roles_by_host=ROLES,
                brick_role_evidence_required=True,
            )
        }

        action = build_plan(manifest, mountpoint="/gtest")[0]

        self.assertEqual("data-b", action.winner_host)
        self.assertNotIn("arbiter", {item["host"] for item in action.file_copies})

    def test_arbiter_payload_candidate_is_blocked(self) -> None:
        result = build_apply_results(
            {"actions": [_file_action(winner_host="arbiter", roles=ROLES)]},
            execution_mode="dry-run",
            batch=True,
        )[0]

        self.assertEqual("blocked", result.status)
        self.assertIn("cannot be used as a payload source", " ".join(result.notes))

    def test_data_brick_payload_candidate_is_accepted(self) -> None:
        result = build_apply_results(
            {"actions": [_file_action(winner_host="data-a", roles=ROLES)]},
            execution_mode="dry-run",
            batch=True,
        )[0]

        self.assertNotEqual("blocked", result.status)
        self.assertIn("data-a", {step.host for step in result.steps})

    def test_matching_arbiter_identity_allows_single_data_restore(self) -> None:
        gfid = "a" * 36
        manifest = {
            "alpha/payload": ManifestObject(
                logical_path="alpha/payload",
                object_type="file",
                depth=2,
                source_hosts=["localhost"],
                observations={
                    "data-a": [_file_observation("data-a", gfid, 10)],
                    "data-b": [_file_observation("data-b", gfid, 11)],
                    "arbiter": [_file_observation("arbiter", gfid, 999)],
                },
                brick_roles_by_host=ROLES,
                brick_role_evidence_required=True,
            )
        }
        manifest["alpha/payload"].observations["data-a"][0].backend_exists = False
        manifest["alpha/payload"].observations["data-a"][0].backend_lexists = False

        action = build_plan(manifest, mountpoint="/gtest")[0]

        self.assertEqual("repair_file", action.action_type)
        self.assertEqual("restore_missing_replica", action.repair_strategy)
        self.assertEqual("data-b", action.winner_host)
        self.assertEqual(["data-b"], action.healthy_hosts)
        self.assertEqual(["data-a"], action.missing_hosts)
        self.assertNotIn("arbiter", {item["host"] for item in action.file_copies})
        self.assertIn("arbiter agree on file identity", " ".join(action.notes))

    def test_data_and_arbiter_identity_guides_quarantine_then_source_copy(self) -> None:
        good_gfid = "a" * 36
        bad_gfid = "b" * 36
        manifest = {
            "alpha/payload": ManifestObject(
                logical_path="alpha/payload",
                object_type="file",
                depth=2,
                source_hosts=["localhost"],
                observations={
                    "data-a": [_file_observation("data-a", good_gfid, 20)],
                    "data-b": [_file_observation("data-b", bad_gfid, 30)],
                    "arbiter": [_file_observation("arbiter", good_gfid, 999)],
                },
                file_gfids=[good_gfid, bad_gfid],
                brick_roles_by_host=ROLES,
                brick_role_evidence_required=True,
            )
        }

        action = build_plan(manifest, mountpoint="/gtest")[0]

        self.assertEqual("review_entry_split_brain", action.action_type)
        self.assertEqual("arbiter_backed_data_identity_conflict", action.repair_strategy)
        self.assertEqual("data-a", action.winner_host)
        self.assertEqual(["data-b"], action.conflict_hosts)
        self.assertEqual("operator_only", action.decision_class)
        self.assertEqual("quarantine_loser", action.recommended_choice)
        self.assertIn("strong authority to select", " ".join(action.notes))
        self.assertIn("ordinary index heal is not the source-selection mechanism", " ".join(action.notes))

        result = build_apply_results({"actions": [action.to_dict()]}, execution_mode="dry-run", batch=True)[0]
        self.assertEqual("review", result.status)
        self.assertIn("quarantine the conflicting data copy", "\n".join(result.notes))
        self.assertEqual(["review_arbiter_data_identity_conflict"], [step.step_type for step in result.steps])

        guided = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            batch=True,
            decisions={action.logical_path: {"file_choice": "quarantine_loser"}},
        )[0]
        self.assertEqual("proposed", guided.status)
        self.assertIn("authorizes this source copy", "\n".join(guided.notes))
        self.assertIn("quarantine_file_backend", [step.step_type for step in guided.steps])
        self.assertIn("restore_file_backend_gap_fill", [step.step_type for step in guided.steps])
        self.assertIn("attach_file_gfid", [step.step_type for step in guided.steps])
        self.assertEqual(
            {"data-a"},
            {
                step.source_host
                for step in guided.steps
                if step.step_type == "restore_file_backend_gap_fill"
            },
        )

    def test_missing_arbiter_identity_does_not_choose_between_conflicting_data(self) -> None:
        good_gfid = "a" * 36
        bad_gfid = "b" * 36
        manifest = {
            "alpha/payload": ManifestObject(
                logical_path="alpha/payload",
                object_type="file",
                depth=2,
                source_hosts=["localhost"],
                observations={
                    "data-a": [_file_observation("data-a", good_gfid, 20)],
                    "data-b": [_file_observation("data-b", bad_gfid, 30)],
                    "arbiter": [_file_observation("arbiter", "", 999)],
                },
                file_gfids=[good_gfid, bad_gfid],
                brick_roles_by_host=ROLES,
                brick_role_evidence_required=True,
            )
        }

        action = build_plan(manifest, mountpoint="/gtest")[0]

        self.assertEqual("review_entry_split_brain", action.action_type)
        self.assertNotEqual("arbiter_backed_data_identity_conflict", action.repair_strategy)
        self.assertNotEqual("safe_default", action.decision_class)
        self.assertNotIn("arbiter", {item["host"] for item in action.file_copies})

    def test_missing_arbiter_file_gfid_is_recreated_from_matching_data_identity(self) -> None:
        gfid = "a" * 36
        manifest = {
            "alpha/payload": ManifestObject(
                logical_path="alpha/payload",
                object_type="file",
                depth=2,
                source_hosts=["localhost"],
                observations={
                    "data-a": [_file_observation("data-a", gfid, 10)],
                    "data-b": [_file_observation("data-b", gfid, 11)],
                    "arbiter": [_file_observation("arbiter", gfid, 999)],
                },
                brick_roles_by_host=ROLES,
                brick_role_evidence_required=True,
            )
        }
        manifest["alpha/payload"].observations["arbiter"][0].backend_trusted_gfid = ""

        action = build_plan(manifest, mountpoint="/gtest")[0]
        result = build_apply_results({"actions": [action.to_dict()]}, execution_mode="dry-run", batch=True)[0]

        self.assertEqual("repair_file_metadata", action.action_type)
        self.assertEqual(["arbiter"], action.arbiter_gfid_repair_hosts)
        self.assertEqual("safe_default", action.decision_class)
        summary = summarize_plan([action])
        self.assertEqual(1, summary["ready_repairs"])
        self.assertIn("1 file metadata repairs", render_plan_summary(summary))
        self.assertEqual("/brick/arbiter/alpha/payload", next(step.target_path for step in result.steps if step.step_type == "attach_file_gfid"))
        self.assertEqual(["attach_file_gfid", "relink_arbiter_file_gfid"], [step.step_type for step in result.steps])
        self.assertTrue(validate_execute_results([result])[0])

    def test_missing_arbiter_file_gfid_handle_is_relinked_from_checked_evidence(self) -> None:
        gfid = "a" * 36
        manifest = {
            "alpha/payload": ManifestObject(
                logical_path="alpha/payload",
                object_type="file",
                depth=2,
                source_hosts=["localhost"],
                observations={
                    "data-a": [_file_observation("data-a", gfid, 10)],
                    "data-b": [_file_observation("data-b", gfid, 11)],
                    "arbiter": [_file_observation("arbiter", gfid, 999)],
                },
                brick_roles_by_host=ROLES,
                brick_role_evidence_required=True,
            )
        }
        arbiter = manifest["alpha/payload"].observations["arbiter"][0]
        arbiter.backend_gfid_path = f"/brick/arbiter/.glusterfs/{gfid[:2]}/{gfid[2:4]}/{gfid}"
        arbiter.backend_gfid_path_checked = True
        arbiter.backend_gfid_path_lexists = False

        action = build_plan(manifest, mountpoint="/gtest")[0]
        result = build_apply_results({"actions": [action.to_dict()]}, execution_mode="dry-run", batch=True)[0]

        self.assertEqual("repair_file_metadata", action.action_type)
        self.assertEqual(["arbiter"], action.arbiter_gfid_repair_hosts)
        self.assertEqual(["attach_file_gfid", "relink_arbiter_file_gfid"], [step.step_type for step in result.steps])
        self.assertTrue(validate_execute_results([result])[0])

    def test_conflicting_arbiter_file_gfid_is_replaced_from_matching_data_identity(self) -> None:
        gfid = "a" * 36
        manifest = {
            "alpha/payload": ManifestObject(
                logical_path="alpha/payload",
                object_type="file",
                depth=2,
                source_hosts=["localhost"],
                observations={
                    "data-a": [_file_observation("data-a", gfid, 10)],
                    "data-b": [_file_observation("data-b", gfid, 11)],
                    "arbiter": [_file_observation("arbiter", "b" * 36, 999)],
                },
                brick_roles_by_host=ROLES,
                brick_role_evidence_required=True,
            )
        }
        arbiter = manifest["alpha/payload"].observations["arbiter"][0]
        arbiter.backend_gfid_path = f"/brick/arbiter/.glusterfs/bb/bb/{'b' * 36}"
        arbiter.backend_gfid_path_checked = True
        arbiter.backend_gfid_path_lexists = True

        action = build_plan(manifest, mountpoint="/gtest")[0]
        result = build_apply_results({"actions": [action.to_dict()]}, execution_mode="dry-run", batch=True)[0]

        self.assertEqual("repair_file_metadata", action.action_type)
        self.assertEqual(["arbiter"], action.arbiter_gfid_repair_hosts)
        self.assertEqual("safe_default", action.decision_class)
        self.assertIn("missing or conflicting arbiter trusted.gfid", " ".join(action.notes))
        self.assertEqual(
            ["attach_file_gfid", "relink_arbiter_file_gfid", "quarantine_stale_arbiter_file_gfid"],
            [step.step_type for step in result.steps],
        )
        self.assertEqual(
            ["restore_quarantined_stale_arbiter_file_gfid"],
            [step.step_type for step in result.revert_steps],
        )

    def test_checked_converged_file_identity_has_no_remaining_review_action(self) -> None:
        gfid = "a" * 36
        manifest = {
            "alpha/payload": ManifestObject(
                logical_path="alpha/payload",
                object_type="file",
                depth=2,
                source_hosts=["localhost"],
                observations={
                    "data-a": [_file_observation("data-a", gfid, 10)],
                    "data-b": [_file_observation("data-b", gfid, 11)],
                    "arbiter": [_file_observation("arbiter", gfid, 999)],
                },
                brick_roles_by_host=ROLES,
                brick_role_evidence_required=True,
            )
        }
        for host, observations in manifest["alpha/payload"].observations.items():
            observation = observations[0]
            observation.backend_gfid_path = f"/brick/{host}/.glusterfs/{gfid[:2]}/{gfid[2:4]}/{gfid}"
            observation.backend_gfid_path_checked = True
            observation.backend_gfid_path_lexists = True

        self.assertEqual([], build_plan(manifest, mountpoint="/gtest"))

    def test_missing_arbiter_directory_gfid_is_recreated_from_matching_data_identity(self) -> None:
        gfid = "a" * 36
        manifest = {
            "alpha/directory": ManifestObject(
                logical_path="alpha/directory",
                object_type="directory",
                depth=2,
                source_hosts=["localhost"],
                observations={
                    "data-a": [_directory_observation("data-a", gfid, 10)],
                    "data-b": [_directory_observation("data-b", gfid, 11)],
                    "arbiter": [_directory_observation("arbiter", gfid, 999)],
                },
                brick_roles_by_host=ROLES,
                brick_role_evidence_required=True,
            )
        }
        manifest["alpha/directory"].observations["arbiter"][0].backend_trusted_gfid = ""

        action = build_plan(manifest, mountpoint="/gtest")[0]
        result = build_apply_results({"actions": [action.to_dict()]}, execution_mode="dry-run", batch=True)[0]

        self.assertEqual("repair_directory_metadata", action.action_type)
        self.assertEqual(["arbiter"], action.arbiter_gfid_repair_hosts)
        self.assertEqual("safe_default", action.decision_class)
        self.assertEqual("/brick/arbiter/alpha/directory", next(step.target_path for step in result.steps if step.step_type == "attach_directory_gfid"))
        self.assertEqual(["attach_directory_gfid", "relink_arbiter_directory_gfid"], [step.step_type for step in result.steps])
        self.assertTrue(validate_execute_results([result])[0])

    def test_missing_arbiter_directory_gfid_handle_is_relinked_from_checked_evidence(self) -> None:
        gfid = "a" * 36
        manifest = {
            "alpha/directory": ManifestObject(
                logical_path="alpha/directory",
                object_type="directory",
                depth=2,
                source_hosts=["localhost"],
                observations={
                    "data-a": [_directory_observation("data-a", gfid, 10)],
                    "data-b": [_directory_observation("data-b", gfid, 11)],
                    "arbiter": [_directory_observation("arbiter", gfid, 999)],
                },
                brick_roles_by_host=ROLES,
                brick_role_evidence_required=True,
            )
        }
        arbiter = manifest["alpha/directory"].observations["arbiter"][0]
        arbiter.backend_gfid_path = f"/brick/arbiter/.glusterfs/{gfid[:2]}/{gfid[2:4]}/{gfid}"
        arbiter.backend_gfid_path_checked = True
        arbiter.backend_gfid_path_lexists = False

        action = build_plan(manifest, mountpoint="/gtest")[0]
        result = build_apply_results({"actions": [action.to_dict()]}, execution_mode="dry-run", batch=True)[0]

        self.assertEqual("repair_directory_metadata", action.action_type)
        self.assertEqual(["arbiter"], action.arbiter_gfid_repair_hosts)
        self.assertEqual(["attach_directory_gfid", "relink_arbiter_directory_gfid"], [step.step_type for step in result.steps])
        self.assertTrue(validate_execute_results([result])[0])

    def test_conflicting_arbiter_directory_gfid_is_replaced_from_matching_data_identity(self) -> None:
        gfid = "a" * 36
        manifest = {
            "alpha/directory": ManifestObject(
                logical_path="alpha/directory",
                object_type="directory",
                depth=2,
                source_hosts=["localhost"],
                observations={
                    "data-a": [_directory_observation("data-a", gfid, 10)],
                    "data-b": [_directory_observation("data-b", gfid, 11)],
                    "arbiter": [_directory_observation("arbiter", "b" * 36, 999)],
                },
                brick_roles_by_host=ROLES,
                brick_role_evidence_required=True,
            )
        }
        arbiter = manifest["alpha/directory"].observations["arbiter"][0]
        arbiter.backend_gfid_path = f"/brick/arbiter/.glusterfs/bb/bb/{'b' * 36}"
        arbiter.backend_gfid_path_checked = True
        arbiter.backend_gfid_path_lexists = True

        action = build_plan(manifest, mountpoint="/gtest")[0]
        result = build_apply_results({"actions": [action.to_dict()]}, execution_mode="dry-run", batch=True)[0]

        self.assertEqual("repair_directory_metadata", action.action_type)
        self.assertEqual(["arbiter"], action.arbiter_gfid_repair_hosts)
        self.assertEqual("safe_default", action.decision_class)
        self.assertEqual(
            [
                "attach_directory_gfid",
                "relink_arbiter_directory_gfid",
                "quarantine_stale_arbiter_directory_gfid",
            ],
            [step.step_type for step in result.steps],
        )
        self.assertEqual(
            ["restore_quarantined_stale_arbiter_directory_gfid"],
            [step.step_type for step in result.revert_steps],
        )

    def test_checked_converged_directory_identity_has_no_remaining_review_action(self) -> None:
        gfid = "a" * 36
        manifest = {
            "alpha/directory": ManifestObject(
                logical_path="alpha/directory",
                object_type="directory",
                depth=2,
                source_hosts=["localhost"],
                observations={
                    "data-a": [_directory_observation("data-a", gfid, 10)],
                    "data-b": [_directory_observation("data-b", gfid, 11)],
                    "arbiter": [_directory_observation("arbiter", gfid, 999)],
                },
                brick_roles_by_host=ROLES,
                brick_role_evidence_required=True,
            )
        }
        for host, observations in manifest["alpha/directory"].observations.items():
            observation = observations[0]
            observation.backend_gfid_path = f"/brick/{host}/.glusterfs/{gfid[:2]}/{gfid[2:4]}/{gfid}"
            observation.backend_gfid_path_checked = True
            observation.backend_gfid_path_lexists = True
            observation.backend_mdata_hex = "0x01020304"

        self.assertEqual([], build_plan(manifest, mountpoint="/gtest"))

    def test_arbiter_only_file_residue_is_cleanup_only_after_both_data_copies_are_absent(self) -> None:
        gfid = "a" * 36
        manifest = {
            "alpha/payload": ManifestObject(
                logical_path="alpha/payload",
                object_type="file",
                depth=2,
                source_hosts=["localhost"],
                observations={
                    "data-a": [_file_observation("data-a", gfid, 10)],
                    "data-b": [_file_observation("data-b", gfid, 11)],
                    "arbiter": [_file_observation("arbiter", gfid, 999)],
                },
                brick_roles_by_host=ROLES,
                brick_role_evidence_required=True,
            )
        }
        for host in ("data-a", "data-b"):
            observation = manifest["alpha/payload"].observations[host][0]
            observation.backend_exists = False
            observation.backend_lexists = False
        manifest["alpha/payload"].observations["arbiter"][0].file_gfid_path = "/brick/arbiter/.glusterfs/aa/aa/arbiter-file"

        action = build_plan(manifest, mountpoint="/gtest")[0]
        result = build_apply_results({"actions": [action.to_dict()]}, execution_mode="dry-run", batch=True)[0]

        self.assertEqual("cleanup_dead_file_refs", action.action_type)
        self.assertEqual("delete_dead_file_ref_residue", action.repair_strategy)
        self.assertEqual({"arbiter"}, {step.host for step in result.steps if step.host})
        self.assertIn("both data bricks prove the file absent", " ".join(result.notes))
        self.assertTrue(validate_execute_results([result])[0])

    def test_arbiter_only_directory_residue_is_cleanup_only_after_both_data_copies_are_absent(self) -> None:
        gfid = "a" * 36
        manifest = {
            "alpha/directory": ManifestObject(
                logical_path="alpha/directory",
                object_type="directory",
                depth=2,
                source_hosts=["localhost"],
                observations={
                    "data-a": [_directory_observation("data-a", gfid, 10)],
                    "data-b": [_directory_observation("data-b", gfid, 11)],
                    "arbiter": [_directory_observation("arbiter", gfid, 999)],
                },
                brick_roles_by_host=ROLES,
                brick_role_evidence_required=True,
            )
        }
        for host in ("data-a", "data-b"):
            observation = manifest["alpha/directory"].observations[host][0]
            observation.backend_exists = False
            observation.backend_lexists = False
        arbiter = manifest["alpha/directory"].observations["arbiter"][0]
        arbiter.backend_gfid_path = "/brick/arbiter/.glusterfs/aa/aa/arbiter-directory"
        arbiter.backend_gfid_path_checked = True
        arbiter.backend_gfid_path_lexists = True

        action = build_plan(manifest, mountpoint="/gtest")[0]
        result = build_apply_results({"actions": [action.to_dict()]}, execution_mode="dry-run", batch=True)[0]

        self.assertEqual("cleanup_arbiter_residue", action.action_type)
        self.assertEqual("delete_arbiter_only_residue", action.repair_strategy)
        summary = summarize_plan([action])
        self.assertEqual(1, summary["ready_repairs"])
        self.assertIn("1 arbiter-residue cleanups", render_plan_summary(summary))
        self.assertEqual({"arbiter"}, {step.host for step in result.steps if step.host})
        self.assertIn("both data bricks prove the directory absent", " ".join(result.notes))
        self.assertIn("restore a volume snapshot first", "\n".join(result.notes))
        backend_remove = next(step for step in result.steps if step.step_type == "remove_stale_backend")
        self.assertIn("rm -rf", " ".join(backend_remove.command_preview))
        self.assertIn("remove_stale_gfid", [step.step_type for step in result.steps])
        self.assertTrue(result.revert_steps)
        self.assertTrue(all(step.host == "arbiter" for step in result.revert_steps))
        self.assertTrue(validate_execute_results([result])[0])

    def test_arbiter_residue_is_not_deleted_while_a_data_copy_remains(self) -> None:
        gfid = "a" * 36
        manifest = {
            "alpha/payload": ManifestObject(
                logical_path="alpha/payload",
                object_type="file",
                depth=2,
                source_hosts=["localhost"],
                observations={
                    "data-a": [_file_observation("data-a", gfid, 10)],
                    "data-b": [_file_observation("data-b", gfid, 11)],
                    "arbiter": [_file_observation("arbiter", gfid, 999)],
                },
                brick_roles_by_host=ROLES,
                brick_role_evidence_required=True,
            )
        }
        observation = manifest["alpha/payload"].observations["data-a"][0]
        observation.backend_exists = False
        observation.backend_lexists = False

        action = build_plan(manifest, mountpoint="/gtest")[0]

        self.assertEqual("repair_file", action.action_type)
        self.assertEqual("restore_missing_replica", action.repair_strategy)

    def test_explicit_arbiter_posix_source_has_no_payload_step(self) -> None:
        action = PlanAction(
            action_id="repair:alpha/metadata",
            logical_path="alpha/metadata",
            action_type="repair_posix_metadata",
            object_type="file",
            depth=2,
            repair_strategy="resolve_posix_metadata_source_brick",
            metadata_source_host="arbiter",
            metadata_source_backend="/gluster/test/alpha/metadata",
            metadata_mismatch_hosts=["data-a"],
            metadata_tuple_by_host={
                "arbiter": {"mode_bits": 0o600, "uid": 1000, "gid": 1000},
                "data-a": {"mode_bits": 0o644, "uid": 1000, "gid": 1000},
            },
            brick_roles_by_host=ROLES,
            brick_role_evidence_required=True,
        )

        result = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            batch=True,
            volume="testvol",
            brick_path="/gluster/test",
        )[0]

        self.assertEqual("proposed", result.status)
        self.assertEqual(["resolve_split_brain_gluster_cli"], [step.step_type for step in result.steps])
        self.assertIn("metadata-only source-brick resolution permitted", " ".join(result.notes))

    def test_missing_or_stale_role_evidence_blocks_payload(self) -> None:
        for roles, expected in [({}, "missing"), ({"data-a": "data"}, "stale")]:
            with self.subTest(expected=expected):
                result = build_apply_results(
                    {"actions": [_file_action(winner_host="data-b", roles=roles)]},
                    execution_mode="dry-run",
                    batch=True,
                )[0]

                self.assertEqual("blocked", result.status)
                self.assertIn(expected, " ".join(result.notes))


if __name__ == "__main__":
    unittest.main()
