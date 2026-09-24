# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Tests for directory ties."""
from __future__ import annotations

import json
import os
from io import StringIO
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from gluster_heal_tool.directory_tie import (
    build_directory_tie_decisions,
    build_directory_tie_report,
    bounded_tree_diff,
    choose_directory_tie_action,
    classify_directory_tie,
    directory_tie_report_hits_budget,
    prompt_directory_tie_selections,
    prompt_directory_tie_budget_override,
    render_directory_tie_diff,
    render_directory_tie_report,
    resolve_directory_tie_budget,
    sniff_subtree_size,
)
from gluster_heal_tool.canary_directory import build_directory_tie_plan_from_canary_state
from gluster_heal_tool.apply import build_apply_results
from gluster_heal_tool.models import ManifestObject, ResolutionObservation
from gluster_heal_tool.planner import build_plan


REPO_ROOT = Path(__file__).resolve().parents[1]


def _directory_obs(
    host: str,
    backend: str,
    gfid: str,
    *,
    logical_path: str,
    child_names: list[str],
    trusted_gfid: str | None = None,
    present: bool = True,
) -> ResolutionObservation:
    trusted = gfid if trusted_gfid is None else trusted_gfid
    return ResolutionObservation(
        host=host,
        raw_entry=f"/{logical_path}",
        gfid=gfid,
        gfid_path=f"/.glusterfs/{gfid[:2]}/{gfid[2:4]}/{gfid}",
        backend=backend,
        type="directory",
        gfid_exists=True,
        relpath=logical_path,
        mounted=f"/{logical_path}",
        depth=2,
        mounted_checked=True,
        mounted_lexists=present,
        mounted_exists=present,
        mounted_lstat_type="dir" if present else "",
        backend_lexists=present,
        backend_exists=present,
        backend_mtime=100,
        backend_size=0,
        backend_mode="drwxr-xr-x",
        backend_lstat_type="dir",
        backend_is_symlink=False,
        backend_readlink="",
        backend_trusted_gfid=trusted,
        backend_child_names=list(child_names),
        gfid_path_lexists=present,
        gfid_path_exists=present,
        gfid_path_lstat_type="symlink" if present else "",
        gfid_path_is_symlink=present,
        gfid_path_readlink=backend,
        error="",
    )


class DirectoryTieLibraryTests(unittest.TestCase):
    def test_classify_directory_tie_child_gap_promotes_to_reconcile(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "tied-dir"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        gfid = "11111111-2222-3333-4444-555555555555"
        manifest = ManifestObject(
            logical_path=logical_path,
            object_type="directory",
            depth=2,
            raw_entries=[f"/{logical_path}"],
            source_hosts=list(hosts),
            gfids=[gfid],
            observations={
                "brick-a": [_directory_obs("brick-a", backend, gfid, logical_path=logical_path, child_names=["alpha", "beta"])],
                "brick-b": [_directory_obs("brick-b", backend, gfid, logical_path=logical_path, child_names=["alpha", "beta"])],
                "brick-c": [_directory_obs("brick-c", backend, gfid, logical_path=logical_path, child_names=["alpha", "beta"])],
                "brick-d": [_directory_obs("brick-d", backend, gfid, logical_path=logical_path, child_names=["alpha"])],
            },
            notes=["synthetic directory tie child-gap"],
        )

        classification = classify_directory_tie(manifest)
        budget = sniff_subtree_size(manifest)
        diff = bounded_tree_diff(manifest)

        self.assertEqual("child_gap", classification.branch_type)
        self.assertEqual(["brick-d"], classification.child_gap_hosts)
        self.assertEqual("reconcile_directory_children", choose_directory_tie_action(classification, budget))
        self.assertFalse(budget.over_budget)
        self.assertEqual("reconcile_directory_children", diff.planner_hint)
        self.assertIn("branch_type=child_gap", render_directory_tie_diff(diff))

    def test_classify_directory_tie_metadata_only_promotes_to_repair(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "metadata-dir"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        gfid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        manifest = ManifestObject(
            logical_path=logical_path,
            object_type="directory",
            depth=2,
            raw_entries=[f"/{logical_path}"],
            source_hosts=list(hosts),
            gfids=[gfid],
            observations={
                "brick-a": [_directory_obs("brick-a", backend, gfid, logical_path=logical_path, child_names=["alpha"])],
                "brick-b": [_directory_obs("brick-b", backend, gfid, logical_path=logical_path, child_names=["alpha"])],
                "brick-c": [_directory_obs("brick-c", backend, gfid, logical_path=logical_path, child_names=["alpha"])],
                "brick-d": [_directory_obs("brick-d", backend, gfid, logical_path=logical_path, child_names=["alpha"], trusted_gfid="")],
            },
            notes=["synthetic directory metadata tie"],
        )

        classification = classify_directory_tie(manifest)
        budget = sniff_subtree_size(manifest)

        self.assertEqual("metadata_only", classification.branch_type)
        self.assertEqual(["brick-d"], classification.metadata_mismatch_hosts)
        self.assertEqual("repair_directory_metadata", choose_directory_tie_action(classification, budget))

    def test_path_resolution_detects_backend_trusted_gfid_tie(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "path-resolved-conflict"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        gfid_a = "11111111-2222-3333-4444-555555555555"
        gfid_b = "66666666-7777-8888-9999-aaaaaaaaaaaa"
        observations = {
            "brick-a": [_directory_obs("brick-a", backend, gfid_a, logical_path=logical_path, child_names=[])],
            "brick-b": [_directory_obs("brick-b", backend, gfid_a, logical_path=logical_path, child_names=[])],
            "brick-c": [_directory_obs("brick-c", backend, gfid_b, logical_path=logical_path, child_names=[])],
            "brick-d": [_directory_obs("brick-d", backend, gfid_b, logical_path=logical_path, child_names=[])],
        }
        for host_observations in observations.values():
            host_observations[0].gfid = ""
            host_observations[0].gfid_path = ""
        manifest = ManifestObject(
            logical_path=logical_path,
            object_type="directory",
            depth=2,
            raw_entries=[f"/{logical_path}"],
            source_hosts=list(hosts),
            file_gfids=[gfid_a, gfid_b],
            observations=observations,
            notes=["saw_dir", "multiple-file-gfids-share-logical-path"],
        )

        classification = classify_directory_tie(manifest)
        action = build_plan({logical_path: manifest}, mountpoint="/testvol")[0]

        self.assertEqual("gfid_conflict", classification.branch_type)
        self.assertEqual("review_directory_gfid_conflict", action.action_type)
        self.assertEqual("rename_conflicting_directory", action.repair_strategy)
        self.assertEqual(
            {"brick-a", "brick-b", "brick-c", "brick-d"},
            {str(copy["host"]) for copy in action.directory_copies},
        )

    def test_classify_directory_tie_gfid_conflict_stays_review(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "conflict-dir"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        gfid_a = "11111111-2222-3333-4444-555555555555"
        gfid_b = "66666666-7777-8888-9999-aaaaaaaaaaaa"
        manifest = ManifestObject(
            logical_path=logical_path,
            object_type="directory",
            depth=2,
            raw_entries=[f"/{logical_path}"],
            source_hosts=list(hosts),
            gfids=[gfid_a, gfid_b],
            observations={
                "brick-a": [_directory_obs("brick-a", backend, gfid_a, logical_path=logical_path, child_names=["alpha"])],
                "brick-b": [_directory_obs("brick-b", backend, gfid_a, logical_path=logical_path, child_names=["alpha"])],
                "brick-c": [_directory_obs("brick-c", backend, gfid_b, logical_path=logical_path, child_names=["alpha"])],
                "brick-d": [_directory_obs("brick-d", backend, gfid_b, logical_path=logical_path, child_names=["alpha"])],
            },
            notes=["synthetic directory GFID conflict"],
        )

        classification = classify_directory_tie(manifest)
        diff = bounded_tree_diff(manifest)

        self.assertEqual("gfid_conflict", classification.branch_type)
        self.assertEqual("review_directory_gfid_conflict", choose_directory_tie_action(classification, diff.budget))
        self.assertEqual("review_directory_gfid_conflict", diff.planner_hint)
        self.assertTrue(diff.next_depth_matches)
        self.assertEqual(["alpha"], diff.next_depth_signature)

    def test_directory_tie_report_exposes_tree_diff_choices(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "report-conflict-dir"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        gfid_a = "11111111-2222-3333-4444-555555555555"
        gfid_b = "66666666-7777-8888-9999-aaaaaaaaaaaa"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=2,
                raw_entries=[f"/{logical_path}"],
                source_hosts=list(hosts),
                gfids=[gfid_a, gfid_b],
                observations={
                    "brick-a": [_directory_obs("brick-a", backend, gfid_a, logical_path=logical_path, child_names=["alpha"])],
                    "brick-b": [_directory_obs("brick-b", backend, gfid_a, logical_path=logical_path, child_names=["alpha"])],
                    "brick-c": [_directory_obs("brick-c", backend, gfid_b, logical_path=logical_path, child_names=["alpha"])],
                    "brick-d": [_directory_obs("brick-d", backend, gfid_b, logical_path=logical_path, child_names=["alpha"])],
                },
                notes=["saw_dir", "dir_name_gfid_conflict"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")
        review_action = plan[0].to_dict()
        review_action["action_type"] = "review_directory_gfid_conflict"
        review_action["repair_strategy"] = "rename_conflicting_directory"
        report = build_directory_tie_report({"actions": [review_action]})
        self.assertEqual(1, report["directory_tie_actions"])
        item = report["items"][0]
        self.assertEqual("gfid_conflict", item["branch_type"])
        self.assertIn("merge", item["choices"])
        self.assertIn("quarantine_both", item["choices"])
        self.assertIn("prune_left", item["choices"])
        self.assertIn("decision_matrix", item)
        self.assertGreaterEqual(len(item["decision_matrix"]), 1)
        self.assertEqual("early", item["decision_matrix"][0]["stage"])
        self.assertEqual("collapse_shared_subtree", item["recommended_choice"])
        rendered = render_directory_tie_report(report)
        self.assertIn("DIRECTORY TIE REPORT", rendered)
        self.assertIn("choices: auto, merge", rendered)
        self.assertIn("quarantine_both", rendered)
        self.assertIn("decision matrix:", rendered)
        self.assertIn("stage early", rendered)
        self.assertIn("recommended_choice: collapse_shared_subtree", rendered)

    def test_directory_tie_report_accepts_merge_canary_input(self) -> None:
        state = {
            "kind": "directory-gfid-merge",
            "volume": "gtest",
            "scenario": "repair-canary-dir-merge-8",
            "mount_root": "/gtest",
            "mount_target": "/gtest/repair-canary-dir-merge-8/alpha",
            "mount_child": "/gtest/repair-canary-dir-merge-8/alpha/payload.txt",
            "child_name": "payload.txt",
            "backend_target": "/srv/gluster/brick-store/gtest/brick/repair-canary-dir-merge-8/alpha",
            "left_hosts": ["node-a", "node-b"],
            "right_hosts": ["node-c", "node-d"],
            "left_gfid_uuid": "11111111-2222-3333-4444-555555555555",
            "right_gfid_uuid": "66666666-7777-8888-9999-aaaaaaaaaaaa",
            "brick_roles_by_host": {"node-a": "data", "node-b": "data", "node-c": "arbiter", "node-d": "data"},
        }
        with patch("gluster_heal_tool.canary_shared._read_state", return_value=state):
            plan = build_directory_tie_plan_from_canary_state(volume="gtest", scenario="repair-canary-dir-merge-8")
        report = build_directory_tie_report(plan)

        self.assertEqual("canary_state", report["input_source"])
        self.assertEqual("harness-only", report["proof_scope"])
        self.assertEqual(
            {
                "kind": "directory-gfid-merge",
                "volume": "gtest",
                "scenario": "repair-canary-dir-merge-8",
            },
            report["canary"],
        )
        item = report["items"][0]
        self.assertEqual("harness-only", item["proof_scope"])
        self.assertEqual(report["canary"], item["canary"])
        self.assertEqual("gfid_conflict", item["branch_type"])
        self.assertEqual("collapse_shared_subtree", item["recommended_choice"])
        self.assertEqual({"node-a": "data", "node-b": "data", "node-c": "arbiter", "node-d": "data"}, plan["actions"][0]["brick_roles_by_host"])
        self.assertIn("merge", item["choices"])
        rendered = render_directory_tie_report(report)
        self.assertIn("brick roles by host:", rendered)
        self.assertIn("node-c: arbiter", rendered)
        self.assertIn("input_source: canary_state", rendered)
        self.assertIn("proof_scope: harness-only", rendered)
        self.assertIn(
            "canary: kind=directory-gfid-merge, volume=gtest, scenario=repair-canary-dir-merge-8",
            rendered,
        )

    def test_directory_tie_report_accepts_tie_canary_input(self) -> None:
        state = {
            "kind": "directory-gfid-tie",
            "volume": "gtest",
            "scenario": "repair-canary-dir-tie-8",
            "mount_root": "/gtest",
            "mount_target": "/gtest/repair-canary-dir-tie-8/alpha",
            "mount_child": "",
            "child_name": "",
            "backend_target": "/srv/gluster/brick-store/gtest/brick/repair-canary-dir-tie-8/alpha",
            "left_hosts": ["node-a", "node-b"],
            "right_hosts": ["node-c", "node-d"],
            "left_gfid_uuid": "11111111-2222-3333-4444-555555555555",
            "right_gfid_uuid": "66666666-7777-8888-9999-aaaaaaaaaaaa",
            "brick_roles_by_host": {"node-a": "data", "node-b": "data", "node-c": "arbiter", "node-d": "data"},
        }
        with patch("gluster_heal_tool.canary_shared._read_state", return_value=state):
            plan = build_directory_tie_plan_from_canary_state(volume="gtest", scenario="repair-canary-dir-tie-8")
        report = build_directory_tie_report(plan)

        self.assertEqual("canary_state", report["input_source"])
        self.assertEqual("harness-only", report["proof_scope"])
        self.assertEqual(
            {
                "kind": "directory-gfid-tie",
                "volume": "gtest",
                "scenario": "repair-canary-dir-tie-8",
            },
            report["canary"],
        )
        item = report["items"][0]
        self.assertEqual("harness-only", item["proof_scope"])
        self.assertEqual(report["canary"], item["canary"])
        self.assertEqual("gfid_conflict", item["branch_type"])
        self.assertEqual("quarantine_both", item["recommended_choice"])
        self.assertEqual({"node-a": "data", "node-b": "data", "node-c": "arbiter", "node-d": "data"}, plan["actions"][0]["brick_roles_by_host"])
        self.assertIn("quarantine_both", item["choices"])
        rendered = render_directory_tie_report(report)
        self.assertIn("brick roles by host:", rendered)
        self.assertIn("node-c: arbiter", rendered)

    def test_directory_tie_report_emits_progress_and_budget_override(self) -> None:
        logical_path = "progress-dir"
        plan = {
            "actions": [
                {
                    "action_id": f"repair:{logical_path}",
                    "logical_path": logical_path,
                    "action_type": "review_directory_children",
                    "object_type": "directory",
                    "depth": 5,
                    "repair_strategy": "reconcile_directory_children",
                    "mounted_target": f"/testvol/{logical_path}",
                    "healthy_hosts": ["brick-a", "brick-b", "brick-c"],
                    "missing_hosts": ["brick-d"],
                    "directory_child_names_by_host": {
                        "brick-a": ["alpha", "beta"],
                        "brick-b": ["alpha", "beta"],
                        "brick-c": ["alpha", "beta"],
                        "brick-d": ["alpha"],
                    },
                    "missing_directory_children_by_host": {"brick-d": ["beta"]},
                    "notes": ["synthetic directory tie progress case"],
                }
            ]
        }
        progress = StringIO()
        report = build_directory_tie_report(plan, progress_stream=progress, progress_interval_seconds=0.0)
        self.assertIn("DIRECTORY TIE PROGRESS", progress.getvalue())
        self.assertTrue(directory_tie_report_hits_budget(report))

        budget_prompt = StringIO()
        next_budget = prompt_directory_tie_budget_override(
            report,
            current_budget=(3, 1024, 4096),
            input_fn=lambda _prompt: "y",
            output_stream=budget_prompt,
        )
        self.assertEqual((4, 2048, 8192), next_budget)
        self.assertIn(
            "choose [e] expand or [s] skip",
            budget_prompt.getvalue(),
        )
        self.assertIn("does not change Gluster data", budget_prompt.getvalue())

    def test_directory_tie_budget_resolves_conservatively_from_memory(self) -> None:
        self.assertEqual((3, 1024, 4096), resolve_directory_tie_budget(mem_available_kib=8 * 1024 * 1024))
        self.assertEqual((1, 64, 256), resolve_directory_tie_budget(mem_available_kib=128 * 1024))
        self.assertEqual(
            (2, 512, 2048),
            resolve_directory_tie_budget(2, None, None, mem_available_kib=4 * 1024 * 1024),
        )

    def test_directory_tie_prompt_and_decision_payload_capture_selection(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "prompt-conflict-dir"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        gfid_a = "11111111-2222-3333-4444-555555555555"
        gfid_b = "66666666-7777-8888-9999-aaaaaaaaaaaa"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=2,
                raw_entries=[f"/{logical_path}"],
                source_hosts=list(hosts),
                gfids=[gfid_a, gfid_b],
                observations={
                    "brick-a": [_directory_obs("brick-a", backend, gfid_a, logical_path=logical_path, child_names=["alpha"])],
                    "brick-b": [_directory_obs("brick-b", backend, gfid_a, logical_path=logical_path, child_names=["alpha"])],
                    "brick-c": [_directory_obs("brick-c", backend, gfid_b, logical_path=logical_path, child_names=["alpha"])],
                    "brick-d": [_directory_obs("brick-d", backend, gfid_b, logical_path=logical_path, child_names=["alpha"])],
                },
                notes=["saw_dir", "dir_name_gfid_conflict"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")
        review_action = plan[0].to_dict()
        review_action["action_type"] = "review_directory_gfid_conflict"
        review_action["repair_strategy"] = "rename_conflicting_directory"
        report = build_directory_tie_report({"actions": [review_action]})
        output = StringIO()
        answers = iter(["merge", "CONFIRM"])
        selections = prompt_directory_tie_selections(
            report,
            input_fn=lambda _prompt: next(answers),
            output_stream=output,
        )
        decisions = build_directory_tie_decisions(report, selections=selections)

        self.assertEqual("merge", selections[logical_path])
        self.assertEqual("merge", decisions[logical_path]["directory_choice"])
        self.assertEqual("operator", decisions[logical_path]["selection_source"])
        rendered = output.getvalue()
        self.assertIn("recommendation: quarantine_both", rendered)
        self.assertIn("recorded directory copies:", rendered)
        self.assertIn("effect: attach canonical trusted.gfid", rendered)
        self.assertIn("rollback:", rendered)
        self.assertIn("protection:", rendered)

    def test_directory_tie_prompt_accepts_quarantine_both_selection(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "prompt-quarantine-dir"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        gfid_a = "11111111-2222-3333-4444-555555555555"
        gfid_b = "66666666-7777-8888-9999-aaaaaaaaaaaa"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=2,
                raw_entries=[f"/{logical_path}"],
                source_hosts=list(hosts),
                gfids=[gfid_a, gfid_b],
                observations={
                    "brick-a": [_directory_obs("brick-a", backend, gfid_a, logical_path=logical_path, child_names=["alpha"])],
                    "brick-b": [_directory_obs("brick-b", backend, gfid_a, logical_path=logical_path, child_names=["alpha"])],
                    "brick-c": [_directory_obs("brick-c", backend, gfid_b, logical_path=logical_path, child_names=["alpha"])],
                    "brick-d": [_directory_obs("brick-d", backend, gfid_b, logical_path=logical_path, child_names=["alpha"])],
                },
                notes=["saw_dir", "dir_name_gfid_conflict"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")
        review_action = plan[0].to_dict()
        review_action["action_type"] = "review_directory_gfid_conflict"
        review_action["repair_strategy"] = "rename_conflicting_directory"
        report = build_directory_tie_report({"actions": [review_action]})
        output = StringIO()
        answers = iter(["quarantine_both", "CONFIRM"])
        selections = prompt_directory_tie_selections(
            report,
            input_fn=lambda _prompt: next(answers),
            output_stream=output,
        )
        decisions = build_directory_tie_decisions(report, selections=selections)

        self.assertEqual("quarantine_both", selections[logical_path])
        self.assertEqual("quarantine_both", decisions[logical_path]["directory_choice"])
        self.assertEqual("operator", decisions[logical_path]["selection_source"])
        rendered = output.getvalue()
        self.assertIn("[s] skip this item (no decision recorded)", rendered)
        self.assertIn("proposed quarantine targets:", rendered)
        self.assertIn(".gluster-quarantine.quarantine_both.", rendered)
        self.assertIn("rollback:", rendered)
        self.assertIn("protection:", rendered)

    def test_directory_tie_loser_preview_prefers_gfid_over_shared_backend(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "prompt-shared-backend-dir"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        gfid_a = "11111111-2222-3333-4444-555555555555"
        gfid_b = "66666666-7777-8888-9999-aaaaaaaaaaaa"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=2,
                raw_entries=[f"/{logical_path}"],
                source_hosts=list(hosts),
                gfids=[gfid_a, gfid_b],
                observations={
                    host: [
                        _directory_obs(
                            host,
                            backend,
                            gfid_a if host in hosts[:2] else gfid_b,
                            logical_path=logical_path,
                            child_names=["alpha"],
                        )
                    ]
                    for host in hosts
                },
                notes=["saw_dir", "dir_name_gfid_conflict"],
            )
        }

        review_action = build_plan(manifest, mountpoint="/testvol")[0].to_dict()
        review_action["action_type"] = "review_directory_gfid_conflict"
        review_action["repair_strategy"] = "rename_conflicting_directory"
        review_action["directory_canonical_gfid"] = gfid_a
        review_action["directory_canonical_host"] = "brick-a"
        review_action["directory_canonical_backend"] = backend
        report = build_directory_tie_report({"actions": [review_action]})
        output = StringIO()
        answers = iter(["quarantine_loser", "CONFIRM"])

        selections = prompt_directory_tie_selections(
            report,
            input_fn=lambda _prompt: next(answers),
            output_stream=output,
        )

        self.assertEqual("quarantine_loser", selections[logical_path])
        rendered = output.getvalue()
        self.assertIn(f"    - brick-c: {backend} ->", rendered)
        self.assertIn(f"    - brick-d: {backend} ->", rendered)
        self.assertNotIn(f"    - brick-a: {backend} ->", rendered)
        self.assertNotIn(f"    - brick-b: {backend} ->", rendered)

    def test_directory_tie_prompt_rejects_unsupported_auto_without_decision(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "prompt-auto-dir"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        gfid_a = "11111111-2222-3333-4444-555555555555"
        gfid_b = "66666666-7777-8888-9999-aaaaaaaaaaaa"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=2,
                raw_entries=[f"/{logical_path}"],
                source_hosts=list(hosts),
                gfids=[gfid_a, gfid_b],
                observations={
                    "brick-a": [_directory_obs("brick-a", backend, gfid_a, logical_path=logical_path, child_names=["alpha"])],
                    "brick-b": [_directory_obs("brick-b", backend, gfid_a, logical_path=logical_path, child_names=["alpha"])],
                    "brick-c": [_directory_obs("brick-c", backend, gfid_b, logical_path=logical_path, child_names=["alpha"])],
                    "brick-d": [_directory_obs("brick-d", backend, gfid_b, logical_path=logical_path, child_names=["alpha"])],
                },
                notes=["saw_dir", "dir_name_gfid_conflict"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")
        review_action = plan[0].to_dict()
        review_action["action_type"] = "review_directory_gfid_conflict"
        review_action["repair_strategy"] = "rename_conflicting_directory"
        report = build_directory_tie_report({"actions": [review_action]})
        output = StringIO()
        selections = prompt_directory_tie_selections(
            report,
            input_fn=lambda _prompt: "auto",
            output_stream=output,
        )
        decisions = build_directory_tie_decisions(report, selections=selections)

        self.assertEqual({}, selections)
        self.assertEqual({}, decisions)
        self.assertIn(
            "unsupported choice auto; skipped without recording a decision",
            output.getvalue(),
        )

    def test_directory_tie_report_prunes_long_matrices(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "pruned-conflict-dir"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        gfid_a = "11111111-2222-3333-4444-555555555555"
        gfid_b = "66666666-7777-8888-9999-aaaaaaaaaaaa"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=2,
                raw_entries=[f"/{logical_path}"],
                source_hosts=list(hosts),
                gfids=[gfid_a, gfid_b],
                observations={
                    "brick-a": [_directory_obs("brick-a", backend, gfid_a, logical_path=logical_path, child_names=["alpha"])],
                    "brick-b": [_directory_obs("brick-b", backend, gfid_a, logical_path=logical_path, child_names=["alpha"])],
                    "brick-c": [_directory_obs("brick-c", backend, gfid_b, logical_path=logical_path, child_names=["alpha"])],
                    "brick-d": [_directory_obs("brick-d", backend, gfid_b, logical_path=logical_path, child_names=["alpha"])],
                },
                notes=["saw_dir", "dir_name_gfid_conflict"],
            )
        }
        long_matrix = [
            {"stage": f"stage-{idx}", "if": "demo", "then": f"action-{idx}", "next": "continue", "why": "synthetic", "risk": "review"}
            for idx in range(1, 8)
        ]

        plan = build_plan(manifest, mountpoint="/testvol")
        review_action = plan[0].to_dict()
        review_action["action_type"] = "review_directory_gfid_conflict"
        review_action["repair_strategy"] = "rename_conflicting_directory"
        with patch("gluster_heal_tool.directory_tie._directory_tie_decision_matrix", return_value=long_matrix):
            report = build_directory_tie_report({"actions": [review_action]})

        item = report["items"][0]
        self.assertTrue(item["decision_matrix_pruned"])
        self.assertEqual(2, item["decision_matrix_pruned_rows"])
        self.assertEqual("pruned", item["decision_matrix"][-1]["stage"])
        rendered = render_directory_tie_report(report)
        self.assertIn("decision_matrix_pruned: true", rendered)
        self.assertIn("rows_trimmed=2", rendered)
        self.assertIn("matrix trimmed to keep memory and table sizes reasonable", rendered)

    def test_directory_tie_build_command_writes_report(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "command-conflict-dir"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        gfid_a = "11111111-2222-3333-4444-555555555555"
        gfid_b = "66666666-7777-8888-9999-aaaaaaaaaaaa"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=2,
                raw_entries=[f"/{logical_path}"],
                source_hosts=list(hosts),
                gfids=[gfid_a, gfid_b],
                observations={
                    "brick-a": [_directory_obs("brick-a", backend, gfid_a, logical_path=logical_path, child_names=["alpha"])],
                    "brick-b": [_directory_obs("brick-b", backend, gfid_a, logical_path=logical_path, child_names=["alpha"])],
                    "brick-c": [_directory_obs("brick-c", backend, gfid_b, logical_path=logical_path, child_names=["alpha"])],
                    "brick-d": [_directory_obs("brick-d", backend, gfid_b, logical_path=logical_path, child_names=["alpha"])],
                },
                notes=["saw_dir", "dir_name_gfid_conflict"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")
        review_action = plan[0].to_dict()
        review_action["action_type"] = "review_directory_gfid_conflict"
        review_action["repair_strategy"] = "rename_conflicting_directory"
        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            plan_path = tmpdir_path / "plan.json"
            report_path = tmpdir_path / "report.json"
            plan_path.write_text(json.dumps({"schema_version": 1, "actions": [review_action]}) + "\n")

            completed = subprocess.run(
                [
                    "python3",
                    str(REPO_ROOT / "gluster-manager.py"),
                    "directory-tie-build",
                    "--plan-in",
                    str(plan_path),
                    "--report-out",
                    str(report_path),
                    "--decision-out",
                    str(tmpdir_path / "decisions.json"),
                    "--max-depth",
                    "3",
                    "--max-children-per-dir",
                    "1024",
                    "--max-total-nodes",
                    "4096",
                    "--status-file",
                    str(tmpdir_path / "status.json"),
                ],
                capture_output=True,
                text=True,
                check=True,
            )

            report = json.loads(report_path.read_text())
            decisions = json.loads((tmpdir_path / "decisions.json").read_text())
            self.assertEqual(1, report["directory_tie_actions"])
            self.assertEqual("collapse_shared_subtree", decisions["decisions"][logical_path]["directory_choice"])
            self.assertIn("DIRECTORY TIE REPORT", completed.stdout)
            self.assertIn("Directory tie actions: 1", completed.stdout)

    def test_directory_tie_build_command_accepts_canary_inputs(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            state_root = tmpdir_path / "state"
            canary_state_dir = state_root / "gtest"
            canary_state_dir.mkdir(parents=True)
            (canary_state_dir / "repair-canary-dir-merge-8.json").write_text(
                json.dumps(
                    {
                        "kind": "directory-gfid-merge",
                        "volume": "gtest",
                        "scenario": "repair-canary-dir-merge-8",
                        "mount_root": "/gtest",
                        "mount_target": "/gtest/repair-canary-dir-merge-8/alpha",
                        "mount_child": "/gtest/repair-canary-dir-merge-8/alpha/payload.txt",
                        "child_name": "payload.txt",
                        "left_hosts": ["node-a", "node-b"],
                        "right_hosts": ["node-c", "node-d"],
                        "brick_roles_by_host": {"node-a": "data", "node-b": "data", "node-c": "data", "node-d": "data"},
                    }
                )
                + "\n"
            )
            report_path = tmpdir_path / "report.json"

            completed = subprocess.run(
                [
                    "python3",
                    str(REPO_ROOT / "gluster-manager.py"),
                    "directory-tie-build",
                    "--canary-volume",
                    "gtest",
                    "--canary-name",
                    "repair-canary-dir-merge-8",
                    "--report-out",
                    str(report_path),
                    "--status-file",
                    str(tmpdir_path / "status.json"),
                ],
                capture_output=True,
                text=True,
                check=True,
                env={
                    **os.environ,
                    "GLUSTER_GTEST_CANARY_STATE_ROOT": str(state_root),
                },
            )

            report = json.loads(report_path.read_text())
            self.assertEqual("canary_state", report["input_source"])
            self.assertEqual("harness-only", report["proof_scope"])
            self.assertEqual("directory-gfid-merge", report["canary"]["kind"])
            self.assertIn("DIRECTORY TIE REPORT", completed.stdout)
            self.assertIn("input_source: canary_state", completed.stdout)
            self.assertIn("proof_scope: harness-only", completed.stdout)
            self.assertIn("recommended_choice: collapse_shared_subtree", completed.stdout)

    def test_directory_tie_depth_and_width_budgets_warn_early(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "wide-dir"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        gfid = "99999999-8888-7777-6666-555555555555"
        manifest = ManifestObject(
            logical_path=logical_path,
            object_type="directory",
            depth=5,
            raw_entries=[f"/{logical_path}"],
            source_hosts=list(hosts),
            gfids=[gfid],
            observations={
                "brick-a": [_directory_obs("brick-a", backend, gfid, logical_path=logical_path, child_names=["a", "b", "c", "d"])],
                "brick-b": [_directory_obs("brick-b", backend, gfid, logical_path=logical_path, child_names=["a", "b", "c", "d"])],
                "brick-c": [_directory_obs("brick-c", backend, gfid, logical_path=logical_path, child_names=["a", "b", "c", "d"])],
                "brick-d": [_directory_obs("brick-d", backend, gfid, logical_path=logical_path, child_names=["a"])],
            },
            notes=["synthetic oversized directory tie"],
        )

        depth_budget = sniff_subtree_size(manifest, max_depth=3, max_children_per_dir=1024, max_total_nodes=4096)
        width_budget = sniff_subtree_size(manifest, max_depth=8, max_children_per_dir=2, max_total_nodes=4096)
        diff = bounded_tree_diff(manifest, max_depth=3, max_children_per_dir=2, max_total_nodes=4)

        self.assertTrue(depth_budget.over_budget)
        self.assertIn("max_depth=3", depth_budget.stop_reason)
        self.assertTrue(width_budget.over_budget)
        self.assertIn("max_children_per_dir=2", width_budget.stop_reason)
        self.assertTrue(diff.budget.over_budget)
        self.assertEqual("review_directory_children", diff.planner_hint)
        self.assertIn("budget_stop=", render_directory_tie_diff(diff))

    def test_planner_surfaces_directory_tie_preview_notes(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "planner-preview-dir"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        gfid = "22222222-3333-4444-5555-666666666666"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=2,
                raw_entries=[f"/{logical_path}"],
                source_hosts=list(hosts),
                gfids=[gfid],
                observations={
                    "brick-a": [_directory_obs("brick-a", backend, gfid, logical_path=logical_path, child_names=["alpha", "beta"])],
                    "brick-b": [_directory_obs("brick-b", backend, gfid, logical_path=logical_path, child_names=["alpha", "beta"])],
                    "brick-c": [_directory_obs("brick-c", backend, gfid, logical_path=logical_path, child_names=["alpha", "beta"])],
                    "brick-d": [_directory_obs("brick-d", backend, gfid, logical_path=logical_path, child_names=["alpha"])],
                },
                notes=["synthetic planner preview"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_directory_children", action.action_type)
        self.assertEqual("reconcile_directory_children", action.repair_strategy)
        self.assertEqual([], action.depends_on)
        rendered = " ".join(action.notes)
        self.assertIn("directory tie preview: child_gap; reconcile_directory_children", rendered)
        self.assertIn("parent presence alone does not prove child intent", rendered)
        self.assertIn("collect immediate --gfid-child", rendered)
