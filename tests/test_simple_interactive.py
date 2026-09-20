# SPDX-License-Identifier: GPL-2.0-only
"""Tests for the read-only simple repair interaction layer."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gluster_heal_tool.apply_reporting import is_ready_to_execute, write_apply_results
from gluster_heal_tool.apply import load_decisions
from gluster_heal_tool.cli import build_parser
from gluster_heal_tool.executor import validate_execute_results
from gluster_heal_tool.models import ApplyActionResult, ApplyStep, BackupArtifact
from gluster_heal_tool.simple_assistants import build_simple_assistant_report
from gluster_heal_tool.simple_interactive import (
    _choice_preview,
    _post_marker_alternate_source_continuation,
    _post_copy_split_brain_marker_continuation,
    _mark_persistent_split_brain_marker_for_support,
    _post_preservation_file_continuation,
    _protection,
    _render_ready_batch,
    _support_summary,
    build_simple_decision_cards,
    resume_simple_interaction,
    run_simple_interaction,
)
from gluster_heal_tool.simple_mode import (
    _resolve_simple_evidence,
    _simple_interactive_requested,
    resume_simple_run,
)


def _result(
    *,
    action_id: str,
    action_type: str,
    logical_path: str,
    status: str,
    strategy: str,
    recommended_choice: str = "",
    recommended_reason: str = "",
    native_heal_first: bool = False,
) -> ApplyActionResult:
    return ApplyActionResult(
        action_id=action_id,
        logical_path=logical_path,
        action_type=action_type,
        execution_mode="dry-run",
        backup_root="/tmp/backup",
        backup_mode="required",
        batch=False,
        status=status,
        native_heal_first=native_heal_first,
        recommended_choice=recommended_choice,
        recommended_reason=recommended_reason,
        notes=[f"repair strategy: {strategy}"],
    )


def _paths(root: Path) -> SimpleNamespace:
    backup = root / "backup"
    backup.mkdir()
    paths = SimpleNamespace(
        root=root,
        status=root / "status.json",
        health=root / "health.json",
        manifest=root / "manifest.json",
        observations=root / "observations.json",
        plan=root / "plan.json",
        apply=root / "apply.json",
        decisions=root / "decisions.json",
        summary=root / "summary.txt",
        assistants=root / "assistants.json",
        backup=backup,
        interaction=root / "interaction.json",
        events=root / "events.jsonl",
        support_summary=root / "support-summary.txt",
    )
    paths.status.write_text("{}", encoding="utf-8")
    paths.decisions.write_text(
        json.dumps({"schema_version": 1, "decisions": {}}),
        encoding="utf-8",
    )
    return paths


class SimpleInteractiveTests(unittest.TestCase):
    def test_simple_evidence_forwards_fresh_host_aliases(self) -> None:
        aliases = {"data-a": ["data-a.lab.example", "192.0.2.10"]}
        with patch(
            "gluster_heal_tool.simple_mode.resolve_via_path",
            return_value={"brick_path": "/brick/data-a"},
        ) as resolve:
            summary, brick_path = _resolve_simple_evidence(
                volume="gtest3",
                path="/mnt/gtest3/alpha/payload.txt",
                backend_path=None,
                gfid=None,
                gfid_child=None,
                index_entry=None,
                heal_file=None,
                heal_latest=False,
                heal_fresh=False,
                mountpoint="/mnt/gtest3",
                resolver_path="/resolver",
                worker_path="/worker",
                manifest_out="/tmp/manifest.json",
                observations_out="/tmp/observations.json",
                heal_out="/tmp/heal.txt",
                heal_root="/tmp/heal-info",
                ssh_user="repair",
                log_path="/tmp/evidence.log",
                verbose=False,
                brick_host_aliases=aliases,
            )

        self.assertEqual({"brick_path": "/brick/data-a"}, summary)
        self.assertEqual("/brick/data-a", brick_path)
        self.assertEqual(aliases, resolve.call_args.kwargs["brick_host_aliases"])

    def test_resume_keeps_summary_json_evidence_route_over_status_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            write_apply_results(paths.apply, [])
            paths.status.write_text(
                json.dumps(
                    {
                        "summary": {"volume": "gtest3", "write_occurred": True},
                        "write_occurred": True,
                        "write_outcome_unknown": True,
                        "execution_write_state": "unknown",
                    }
                ),
                encoding="utf-8",
            )
            (paths.root / "summary.json").write_text(
                json.dumps(
                    {
                        "volume": "gtest3",
                        "mountpoint": "/gtest3",
                        "evidence_inputs": {"path": "/mnt/gtest3/data/file"},
                        "write_occurred": False,
                    }
                ),
                encoding="utf-8",
            )
            captured: dict[str, object] = {}

            def resume(
                _paths_value: SimpleNamespace,
                summary: dict[str, object],
                **_kwargs: object,
            ) -> int:
                captured.update(summary)
                return 0

            with patch("gluster_heal_tool.simple_mode.resume_simple_interaction", side_effect=resume):
                self.assertEqual(0, resume_simple_run(str(paths.root), input_stream=io.StringIO(), output_stream=io.StringIO()))

            self.assertEqual({"path": "/mnt/gtest3/data/file"}, captured["evidence_inputs"])
            self.assertTrue(captured["write_occurred"])
            self.assertTrue(captured["write_outcome_unknown"])
            self.assertEqual("unknown", captured["execution_write_state"])

    def test_resume_recovers_recognized_path_route_from_status_when_summary_was_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            write_apply_results(paths.apply, [])
            requested_path = "/mnt/gtest3/data/file"
            paths.status.write_text(
                json.dumps(
                    {
                        "summary": {"volume": "gtest3", "write_occurred": True},
                        "volume": "gtest3",
                        "input_source": "operator_path",
                        "requested_seed": requested_path,
                        "ssh_user": "gluster-repair",
                        "connect_timeout": 10.0,
                    }
                ),
                encoding="utf-8",
            )
            (paths.root / "summary.json").write_text(
                json.dumps({"volume": "gtest3", "write_occurred": True}),
                encoding="utf-8",
            )
            captured: dict[str, object] = {}

            def resume(
                _paths_value: SimpleNamespace,
                summary: dict[str, object],
                **_kwargs: object,
            ) -> int:
                captured.update(summary)
                return 0

            with patch("gluster_heal_tool.simple_mode.resume_simple_interaction", side_effect=resume):
                self.assertEqual(0, resume_simple_run(str(paths.root), input_stream=io.StringIO(), output_stream=io.StringIO()))

            self.assertEqual({"path": requested_path}, captured["evidence_inputs"])
            self.assertEqual("gluster-repair", captured["ssh_user"])

    def test_post_copy_marker_continuation_requires_fresh_equal_data_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            logical_path = "data/arbiter-conflict"
            original = _result(
                action_id=f"repair:/{logical_path}",
                action_type="review_entry_split_brain",
                logical_path=logical_path,
                status="proposed",
                strategy="quarantine_conflicting_file",
            )
            original.steps.extend(
                [
                    ApplyStep(step_id="quarantine-backend", step_type="quarantine_file_backend"),
                    ApplyStep(step_id="quarantine-gfid", step_type="quarantine_file_gfid"),
                    ApplyStep(
                        step_id="copy",
                        step_type="restore_file_backend_gap_fill",
                        source_host="data-a",
                        source_path="/bricks/a/data/arbiter-conflict",
                        target_path="/bricks/b/data/arbiter-conflict",
                    ),
                    ApplyStep(
                        step_id="attach",
                        step_type="attach_file_gfid",
                        source_path="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                        target_path="/bricks/b/data/arbiter-conflict",
                    ),
                ]
            )
            write_apply_results(
                paths.root / "apply-before-post-preservation-continuation.json",
                [original],
                decision_file=str(paths.decisions),
                controller_cycle={},
            )
            (paths.root / "execute-results.json").write_text(
                json.dumps(
                    {
                        "actions": [
                            {
                                "action_id": original.action_id,
                                "status": "completed",
                                "steps": [
                                    {"step_type": "restore_file_backend_gap_fill", "status": "ok"},
                                    {"step_type": "attach_file_gfid", "status": "ok"},
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            current = _result(
                action_id=f"review:/{logical_path}",
                action_type="review_entry_split_brain",
                logical_path=logical_path,
                status="review",
                strategy="ambiguous_entry_split_brain_file",
            )
            assistants = {
                "items": [
                    {
                        "action_id": current.action_id,
                        "details": {
                            "action": {
                                "action_type": "review_entry_split_brain",
                                "notes": ["heal_info_marks_split_brain"],
                                "brick_roles_by_host": {
                                    "data-a": "data",
                                    "data-b": "data",
                                    "arbiter": "arbiter",
                                },
                                "brick_role_evidence_required": True,
                                "file_copies": [
                                    {
                                        "host": "data-a",
                                        "backend": "/bricks/a/data/arbiter-conflict",
                                        "identity": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                                    },
                                    {
                                        "host": "data-b",
                                        "backend": "/bricks/b/data/arbiter-conflict",
                                        "identity": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                                    },
                                ],
                            },
                            "checksum": {"outcome": "content-equal"},
                        },
                    }
                ]
            }

            continuation = _post_copy_split_brain_marker_continuation(
                paths,
                {"volume": "testvol", "brick_path": ""},
                [current],
                assistants,
            )

            self.assertIsNotNone(continuation)
            resumed, details = continuation or ([], {})
            self.assertEqual("data-a", details["source_host"])
            self.assertEqual("clear_split_brain_marker", resumed[0].action_type)
            self.assertEqual(["resolve_split_brain_gluster_cli"], [step.step_type for step in resumed[0].steps])
            self.assertTrue(is_ready_to_execute(resumed[0]))
            self.assertEqual((True, []), validate_execute_results(resumed))
            self.assertIn("source-brick", resumed[0].steps[0].command_preview)
            self.assertNotIn("arbiter", resumed[0].steps[0].command_preview)

            paths.status.write_text(
                json.dumps({"split_brain_marker_continuation": details}),
                encoding="utf-8",
            )
            (paths.root / "execute-results.json").write_text(
                json.dumps(
                    {
                        "actions": [
                            {
                                "action_id": details["continuation_action_id"],
                                "status": "completed-with-nonblocking-skips",
                                "steps": [
                                    {
                                        "step_type": "resolve_split_brain_gluster_cli",
                                        "status": "skipped",
                                        "message": "ssh transport failed",
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            self.assertIsNone(
                _post_marker_alternate_source_continuation(
                    paths,
                    {"volume": "testvol"},
                    [current],
                    assistants,
                )
            )
            (paths.root / "execute-results.json").write_text(
                json.dumps(
                    {
                        "actions": [
                            {
                                "action_id": details["continuation_action_id"],
                                "status": "completed",
                                "steps": [
                                    {
                                        "step_type": "resolve_split_brain_gluster_cli",
                                        "status": "ok",
                                        "message": "source-brick resolver applied",
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            alternate = _post_marker_alternate_source_continuation(
                paths,
                {"volume": "testvol"},
                [current],
                assistants,
            )

            self.assertIsNotNone(alternate)
            alternate_results, alternate_details = alternate or ([], {})
            self.assertEqual("data-b", alternate_details["source_host"])
            self.assertEqual("data-b", alternate_results[0].steps[0].host)
            self.assertTrue(is_ready_to_execute(alternate_results[0]))
            self.assertEqual((True, []), validate_execute_results(alternate_results))

            paths.status.write_text(
                json.dumps(
                    {
                        "split_brain_marker_continuation": details,
                        "split_brain_marker_alternate_continuation": alternate_details,
                    }
                ),
                encoding="utf-8",
            )
            (paths.root / "execute-results.json").write_text(
                json.dumps(
                    {
                        "actions": [
                            {
                                "action_id": alternate_details["continuation_action_id"],
                                "status": "completed-with-nonblocking-skips",
                                "steps": [
                                    {
                                        "step_type": "resolve_split_brain_gluster_cli",
                                        "status": "skipped",
                                        "message": "permission denied",
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            self.assertFalse(_mark_persistent_split_brain_marker_for_support(paths, [current], assistants))
            (paths.root / "execute-results.json").write_text(
                json.dumps(
                    {
                        "actions": [
                            {
                                "action_id": alternate_details["continuation_action_id"],
                                "status": "completed",
                                "steps": [
                                    {
                                        "step_type": "resolve_split_brain_gluster_cli",
                                        "status": "ok",
                                        "message": "source-brick resolver applied",
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            self.assertTrue(_mark_persistent_split_brain_marker_for_support(paths, [current], assistants))
            cards, ready_batch = build_simple_decision_cards([current], assistants)
            self.assertEqual([], ready_batch)
            self.assertEqual("Create a bounded maintainer evidence handoff", cards[0].recommendation)
            self.assertEqual((), cards[0].available_choices)

    def test_post_preservation_file_continuation_replays_only_remaining_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            result = _result(
                action_id="repair:/data/file",
                action_type="review_entry_split_brain",
                logical_path="/data/file",
                status="proposed",
                strategy="quarantine_conflicting_file",
            )
            for step_id, step_type in (
                ("quarantine-backend", "quarantine_file_backend"),
                ("quarantine-gfid", "quarantine_file_gfid"),
                ("copy", "restore_file_backend_gap_fill"),
                ("attach", "attach_file_gfid"),
            ):
                result.steps.append(ApplyStep(step_id=step_id, step_type=step_type))
            (paths.root / "execute-results.json").write_text(
                json.dumps(
                    {
                        "actions": [
                            {
                                "action_id": result.action_id,
                                "status": "failed",
                                "steps": [
                                    {"status": "ok"},
                                    {"status": "ok"},
                                    {"status": "failed"},
                                    {"status": "planned"},
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            continuation = _post_preservation_file_continuation(paths, [result])

            self.assertIsNotNone(continuation)
            resumed, details = continuation or ([], {})
            self.assertEqual(["restore_file_backend_gap_fill", "attach_file_gfid"], [step.step_type for step in resumed[0].steps])
            self.assertEqual(["quarantine-backend", "quarantine-gfid"], details["completed_step_ids"])

    def test_quarantine_source_copy_describes_named_rollback_protection(self) -> None:
        result = _result(
            action_id="repair:/data/arbiter-conflict",
            action_type="review_entry_split_brain",
            logical_path="/data/arbiter-conflict",
            status="proposed",
            strategy="quarantine_conflicting_file",
        )
        result.steps.append(
            ApplyStep(
                step_id="quarantine",
                step_type="quarantine_file_backend",
                source_path="/brick/conflict",
                target_path="/brick/conflict.gluster-quarantine",
            )
        )
        result.revert_steps.append(
            ApplyStep(
                step_id="restore",
                step_type="restore_quarantined_file_backend",
                source_path="/brick/conflict.gluster-quarantine",
                target_path="/brick/conflict",
            )
        )

        output = io.StringIO()
        _render_ready_batch([result], output)

        self.assertIn("quarantine moves are the reversible preservation", _protection(result))
        self.assertIn("no separate backup copy is planned", output.getvalue())
        self.assertIn("named quarantine targets and recorded revert steps", output.getvalue())

    def test_every_guided_choice_preview_has_effect_rollback_and_protection(self) -> None:
        action = {
            "action_id": "review:/data/shared",
            "metadata_backend_by_host": {
                "brick-a": "/brick/a/file",
                "brick-b": "/brick/b/file",
            },
            "metadata_tuple_by_host": {
                "brick-a": {"mode": 0o640, "uid": 1},
                "brick-b": {"mode": 0o600, "uid": 2},
            },
            "directory_backend_by_host": {
                "brick-a": "/brick/a/dir",
                "brick-b": "/brick/b/dir",
            },
            "directory_mdata_by_host": {
                "brick-a": "0x01",
                "brick-b": "0x02",
            },
            "brick_roles_by_host": {
                "brick-a": "data",
                "brick-b": "data",
            },
            "file_copies": [
                {
                    "host": "brick-a",
                    "backend": "/brick/a/file",
                    "identity": "file-gfid-a",
                    "file_gfid_path": "/brick/a/.glusterfs/file-gfid-a",
                    "size": 10,
                    "mtime": 20,
                },
                {
                    "host": "brick-b",
                    "backend": "/brick/b/file",
                    "identity": "file-gfid-b",
                    "file_gfid_path": "/brick/b/.glusterfs/file-gfid-b",
                    "size": 10,
                    "mtime": 10,
                },
            ],
            "directory_copies": [
                {
                    "host": "brick-a",
                    "backend": "/brick/a/dir",
                    "identity": "dir-gfid-a",
                    "gfid_path": "/brick/a/.glusterfs/dir-gfid-a",
                },
                {
                    "host": "brick-b",
                    "backend": "/brick/b/dir",
                    "identity": "dir-gfid-b",
                    "gfid_path": "/brick/b/.glusterfs/dir-gfid-b",
                },
            ],
            "directory_canonical_host": "brick-a",
            "directory_canonical_backend": "/brick/a/dir",
            "directory_canonical_gfid": "dir-gfid-a",
            "missing_directory_children_by_host": {
                "brick-b": ["payload.txt"],
            },
            "depends_on": ["repair:/data/shared/payload.txt"],
        }
        decisions = [
            {"metadata_source_host": "brick-a"},
            {
                "directory_choice": "repair_directory_mdata",
                "mdata_source_host": "brick-a",
            },
            {"keep_gfid": "file-gfid-a"},
            {"native_heal_first": "refresh"},
            {"file_choice": "quarantine_both"},
            {
                "directory_choice": "quarantine_side",
                "directory_quarantine_identity": "dir-gfid-b",
            },
            {"type_mismatch_choice": "quarantine_both"},
            {"directory_choice": "merge"},
            {"directory_choice": "reconcile_directory_children"},
        ]

        previews = [
            _choice_preview(
                action,
                decision=decision,
                protection="required backup",
            )
            for decision in decisions
        ]

        for preview in previews:
            self.assertTrue(
                any(line.startswith("effect:") for line in preview),
                preview,
            )
            self.assertTrue(
                any(line.startswith("rollback:") for line in preview),
                preview,
            )
            self.assertEqual("protection: required backup", preview[-1])
        combined = "\n".join(line for preview in previews for line in preview)
        self.assertIn("current={\"mode\":384,\"uid\":2}", combined)
        self.assertIn("proposed={\"mode\":416,\"uid\":1}", combined)
        self.assertIn(
            "GFID link: /brick/a/.glusterfs/file-gfid-a ->",
            combined,
        )
        self.assertIn("native side effect:", combined)
        self.assertIn("current GFID dir-gfid-b", combined)
        self.assertIn("payload.txt", combined)

    def test_guided_choices_only_include_recorded_non_arbiter_sources(self) -> None:
        review = _result(
            action_id="review:/data/file",
            action_type="review_posix_metadata_no_majority",
            logical_path="/data/file",
            status="review",
            strategy="review_posix_metadata_no_majority",
        )
        assistants = {
            "items": [
                {
                    "action_id": review.action_id,
                    "fingerprint": "posix-evidence",
                    "details": {
                        "action": {
                            "action_id": review.action_id,
                            "action_type": "review_posix_metadata_no_majority",
                            "metadata_backend_by_host": {
                                "brick-a": "/bricks/a/file",
                                "brick-b": "/bricks/b/file",
                                "arbiter": "/bricks/arb/file",
                            },
                        },
                        "posix": {
                            "rows": [
                                {
                                    "host": "brick-a",
                                    "role": "data",
                                    "backend": "/bricks/a/file",
                                    "tuple": {"uid": 1},
                                },
                                {
                                    "host": "brick-b",
                                    "role": "data",
                                    "backend": "/bricks/b/file",
                                    "tuple": {"uid": 2},
                                },
                                {
                                    "host": "arbiter",
                                    "role": "arbiter",
                                    "backend": "/bricks/arb/file",
                                    "tuple": {"uid": 3},
                                },
                            ]
                        },
                    },
                }
            ]
        }
        cards, ready_batch = build_simple_decision_cards([review], assistants)
        self.assertEqual([], ready_batch)
        self.assertEqual(1, len(cards))
        self.assertEqual(
            ["Use POSIX metadata from brick-a", "Use POSIX metadata from brick-b"],
            [choice["label"] for choice in cards[0].available_choices],
        )
        self.assertNotIn(
            "arbiter",
            " ".join(cards[0].available_choices[0]["preview"]),
        )

    def test_guided_choices_exclude_arbiter_aliases_for_posix_and_directory_mdata(self) -> None:
        posix_review = _result(
            action_id="review:/data/posix",
            action_type="review_posix_metadata_no_majority",
            logical_path="/data/posix",
            status="review",
            strategy="review_posix_metadata_no_majority",
        )
        directory_review = _result(
            action_id="review:/data/directory",
            action_type="review_directory_metadata",
            logical_path="/data/directory",
            status="review",
            strategy="review_directory_mdata_state",
        )
        roles = {"data-a": "data", "data-b": "data", "arbiter": "arbiter"}
        aliases = {
            "data-a": ["data-a.example.test"],
            "data-b": ["192.0.2.22"],
            "arbiter": ["arbiter.example.test", "192.0.2.23"],
        }
        actions = [
            {
                "action_id": posix_review.action_id,
                "logical_path": posix_review.logical_path,
                "action_type": posix_review.action_type,
                "repair_strategy": "choose_posix_metadata_source",
                "brick_roles_by_host": roles,
                "brick_host_aliases": aliases,
                "metadata_tuple_by_host": {
                    "data-a.example.test": {"uid": 1},
                    "192.0.2.22": {"uid": 2},
                    "arbiter.example.test": {"uid": 3},
                },
                "metadata_backend_by_host": {
                    "data-a.example.test": "/bricks/a/file",
                    "192.0.2.22": "/bricks/b/file",
                    "arbiter.example.test": "/bricks/arb/file",
                },
            },
            {
                "action_id": directory_review.action_id,
                "logical_path": directory_review.logical_path,
                "action_type": directory_review.action_type,
                "repair_strategy": "review_directory_mdata_state",
                "brick_roles_by_host": roles,
                "brick_host_aliases": aliases,
                "directory_mdata_by_host": {
                    "data-a.example.test": "0x01",
                    "192.0.2.22": "0x02",
                    "192.0.2.23": "0x03",
                },
                "directory_backend_by_host": {
                    "data-a.example.test": "/bricks/a/dir",
                    "192.0.2.22": "/bricks/b/dir",
                    "192.0.2.23": "/bricks/arb/dir",
                },
            },
        ]
        assistants = build_simple_assistant_report(
            {"actions": actions},
            volume="gtest3a",
            worker_path="/unused",
            ssh_user="gluster-repair",
        )

        cards, ready_batch = build_simple_decision_cards([posix_review, directory_review], assistants)

        self.assertEqual([], ready_batch)
        labels = [choice["label"] for card in cards for choice in card.available_choices]
        self.assertIn("Use POSIX metadata from data-a.example.test", labels)
        self.assertIn("Use POSIX metadata from 192.0.2.22", labels)
        self.assertIn("Use directory metadata from data-a.example.test", labels)
        self.assertNotIn("Use POSIX metadata from arbiter.example.test", labels)
        self.assertNotIn("Use directory metadata from 192.0.2.23", labels)

    def test_guided_choice_requires_exact_confirmation_and_records_planner_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            review = _result(
                action_id="review:/data/file",
                action_type="review_posix_metadata_no_majority",
                logical_path="/data/file",
                status="review",
                strategy="review_posix_metadata_no_majority",
            )
            assistants = paths.root / "assistants.json"
            assistants.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "action_id": review.action_id,
                                "fingerprint": "posix-evidence",
                                "details": {
                                    "action": {
                                        "action_id": review.action_id,
                                        "action_type": "review_posix_metadata_no_majority",
                                        "metadata_backend_by_host": {
                                            "brick-a": "/bricks/a/file"
                                        },
                                    },
                                    "posix": {
                                        "rows": [
                                            {
                                                "host": "brick-a",
                                                "role": "data",
                                                "backend": "/bricks/a/file",
                                                "tuple": {"uid": 1},
                                            }
                                        ]
                                    },
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            summary = {
                "volume": "gtest3",
                "run_dir": str(paths.root),
                "write_occurred": False,
            }
            with patch(
                "gluster_heal_tool.simple_interactive._same_source_replan",
                return_value=([review], {}),
            ) as replan:
                output = io.StringIO()
                result = run_simple_interaction(
                    paths,
                    summary,
                    [review],
                    input_stream=io.StringIO(
                        "1" + chr(10) + "confirm" + chr(10)
                        + "1" + chr(10) + "CONFIRM" + chr(10)
                        + "q" + chr(10)
                    ),
                    output_stream=output,
                )
            self.assertEqual(0, result)
            self.assertEqual(1, replan.call_count)
            decisions = json.loads(paths.decisions.read_text(encoding="utf-8"))
            saved = decisions["decisions"][review.logical_path]
            self.assertEqual("brick-a", saved["metadata_source_host"])
            self.assertEqual(
                {"metadata_source_host": "brick-a"},
                saved["decision_payload"],
            )
            self.assertTrue(saved["confirmed"])
            self.assertTrue(saved["preserve_first"])
            self.assertIn("Choice cancelled", output.getvalue())
            self.assertIn(
                "Decision recorded and the dry-run plan was rebuilt",
                output.getvalue(),
            )
            self.assertIn("guided-decision", paths.events.read_text(encoding="utf-8"))

    def test_guided_quarantine_reaches_operator_approved_execution_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            review = _result(
                action_id="repair:/data/directory-tie",
                action_type="review_directory_gfid_conflict",
                logical_path="/data/directory-tie",
                status="review",
                strategy="rename_conflicting_directory",
                recommended_choice="quarantine_both",
            )
            assistant_report = {
                "items": [
                    {
                        "action_id": review.action_id,
                        "fingerprint": "directory-tie-evidence",
                        "details": {
                            "action": {
                                "action_type": "review_directory_gfid_conflict",
                                "repair_strategy": "rename_conflicting_directory",
                                "recommended_choice": "quarantine_both",
                                "directory_canonical_host": "brick-a",
                                "directory_canonical_gfid": "gfid-a",
                                "directory_copies": [
                                    {
                                        "host": "brick-a",
                                        "backend": "/brick/a/directory-tie",
                                        "identity": "gfid-a",
                                    },
                                    {
                                        "host": "brick-b",
                                        "backend": "/brick/b/directory-tie",
                                        "identity": "gfid-a",
                                    },
                                    {
                                        "host": "brick-c",
                                        "backend": "/brick/c/directory-tie",
                                        "identity": "gfid-b",
                                    },
                                    {
                                        "host": "brick-d",
                                        "backend": "/brick/d/directory-tie",
                                        "identity": "gfid-b",
                                    },
                                ],
                            },
                            "directory_tie": {
                                "recommended_choice": "quarantine_both",
                                "classification": {
                                    "reason": "same logical directory name appears with multiple GFIDs",
                                },
                            },
                        },
                    }
                ]
            }
            (paths.root / "assistants.json").write_text(
                json.dumps(assistant_report),
                encoding="utf-8",
            )
            approved = _result(
                action_id=review.action_id,
                action_type="review_directory_gfid_conflict",
                logical_path=review.logical_path,
                status="proposed",
                strategy="quarantine_conflicting_directory",
            )
            approved.decision = {
                "logical_path": review.logical_path,
                "directory_choice": "quarantine_both",
                "decision_payload": {"directory_choice": "quarantine_both"},
                "confirmed": True,
                "evidence_fingerprint": "directory-tie-evidence",
            }
            execution_report = {
                "summary": {
                    "executed_actions": 1,
                    "completed_actions": 1,
                    "failed_actions": 0,
                },
                "actions": [
                    {
                        "action_id": approved.action_id,
                        "status": "completed",
                    }
                ],
            }
            with (
                patch(
                    "gluster_heal_tool.simple_interactive._same_source_replan",
                    return_value=([approved], assistant_report),
                ),
                patch(
                    "gluster_heal_tool.simple_interactive._execute_ready_batch",
                    return_value=(True, execution_report),
                ) as execute,
                patch(
                    "gluster_heal_tool.simple_interactive._refresh_evidence",
                    return_value=([], {}),
                ),
            ):
                output = io.StringIO()
                result = run_simple_interaction(
                    paths,
                    {
                        "volume": "gtest4",
                        "run_dir": str(paths.root),
                        "write_occurred": False,
                    },
                    [review],
                    input_stream=io.StringIO("3\nCONFIRM\na\n"),
                    output_stream=output,
                    allow_execution=True,
                    safe_auto=True,
                    safe_auto_authorized=True,
                )

            self.assertEqual(0, result)
            execute.assert_called_once()
            self.assertIn("Operator-approved batch preview", output.getvalue())
            self.assertIn("Choice: [a] authorize this batch", output.getvalue())
            self.assertNotIn(
                "Safe-auto authorization is active; executing this ready-safe batch.",
                output.getvalue(),
            )
            saved = json.loads(paths.decisions.read_text(encoding="utf-8"))
            self.assertIn(review.logical_path, saved["decisions"])
            self.assertNotIn(review.action_id, saved["decisions"])

    def test_load_decisions_maps_legacy_action_id_to_logical_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "decisions.json"
            path.write_text(
                json.dumps(
                    {
                        "decisions": {
                            "repair:/data/directory-tie": {
                                "logical_path": "/data/directory-tie",
                                "directory_choice": "quarantine_both",
                                "confirmed": True,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            decisions = load_decisions(path)

            self.assertEqual(
                "quarantine_both",
                decisions["/data/directory-tie"]["directory_choice"],
            )
            self.assertNotIn("repair:/data/directory-tie", decisions)

    def test_stale_guided_decision_is_not_loaded_for_planning(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "decisions.json"
            path.write_text(
                json.dumps(
                    {
                        "decisions": {
                            "/data/file": {
                                "metadata_source_host": "brick-a",
                                "stale": True,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual({}, load_decisions(path))

    def test_native_heal_first_choice_refreshes_evidence_after_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            review = _result(
                action_id="review:/data/file",
                action_type="review_entry_split_brain",
                logical_path="/data/file",
                status="review",
                strategy="native_heal_first",
                native_heal_first=True,
            )
            summary = {"volume": "gtest3", "run_dir": str(paths.root)}
            with patch(
                "gluster_heal_tool.simple_interactive._refresh_evidence",
                return_value=([], {}),
            ) as refresh:
                output = io.StringIO()
                result = run_simple_interaction(
                    paths,
                    summary,
                    [review],
                    input_stream=io.StringIO("1" + chr(10) + "CONFIRM" + chr(10)),
                    output_stream=output,
                )
            self.assertEqual(0, result)
            refresh.assert_called_once()
            self.assertIn("Native heal/rescan completed", output.getvalue())
            decisions = json.loads(paths.decisions.read_text(encoding="utf-8"))
            self.assertEqual(
                "refresh",
                decisions["decisions"][review.logical_path]["native_heal_first"],
            )

    def test_skip_is_visible_and_records_no_write_choice(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            review = _result(
                action_id="review:/data/file",
                action_type="review_entry_split_brain",
                logical_path="/data/file",
                status="review",
                strategy="ambiguous_entry_split_brain_file",
            )
            output = io.StringIO()

            result = run_simple_interaction(
                paths,
                {"volume": "gtest3", "run_dir": str(paths.root)},
                [review],
                input_stream=io.StringIO("s\n"),
                output_stream=output,
            )

            self.assertEqual(0, result)
            self.assertIn("[s] skip this item (no changes)", output.getvalue())
            decisions = json.loads(paths.decisions.read_text(encoding="utf-8"))
            saved = decisions["decisions"][review.action_id]
            self.assertEqual("skip", saved["operator_choice"])
            self.assertEqual("skip", saved["choice"])
            self.assertTrue(saved["read_only"])
            self.assertEqual({}, load_decisions(paths.decisions))

    def test_parser_exposes_interactive_and_preview_modes(self) -> None:
        parser = build_parser()
        interactive = parser.parse_args(["repair", "--volume", "gtest3", "--interactive"])
        preview = parser.parse_args(["repair", "--volume", "gtest3", "--preview"])
        expert = parser.parse_args(["repair", "--volume", "gtest3", "--expert", "--interactive"])
        yes = parser.parse_args(["repair", "--volume", "gtest3", "-y"])
        self.assertTrue(interactive.interactive)
        self.assertTrue(preview.preview)
        self.assertTrue(expert.expert)
        self.assertTrue(yes.safe_auto_yes)
        with self.assertRaises(SystemExit):
            parser.parse_args(["repair", "--volume", "gtest3", "--interactive", "--preview"])

    def test_non_tty_defaults_to_no_prompt(self) -> None:
        args = SimpleNamespace(interactive=False, preview=False)
        with patch("gluster_heal_tool.simple_mode.sys.stdin", io.StringIO()):
            with patch("gluster_heal_tool.simple_mode.sys.stdout", io.StringIO()):
                self.assertFalse(_simple_interactive_requested(args))

    def test_tty_defaults_to_easy_interactive_execution(self) -> None:
        from gluster_heal_tool.simple_mode import run_simple_command

        parser = build_parser()
        args = parser.parse_args(["repair", "--volume", "gtest3"])
        with patch("gluster_heal_tool.simple_mode._simple_interactive_requested", return_value=True):
            with patch("gluster_heal_tool.simple_mode.run_simple_preview", return_value=0) as preview:
                self.assertEqual(0, run_simple_command(args))
        self.assertTrue(preview.call_args.kwargs["interactive"])
        self.assertTrue(preview.call_args.kwargs["execute"])

    def test_execute_flag_is_exposed_and_non_tty_is_rejected(self) -> None:
        parser = build_parser()
        execute = parser.parse_args(["repair", "--volume", "gtest3", "--interactive", "--execute"])
        self.assertTrue(execute.execute)
        safe_auto = parser.parse_args(
            ["repair", "--volume", "gtest3", "--safe-auto", "--batch", "-y"]
        )
        self.assertTrue(safe_auto.safe_auto)
        self.assertTrue(safe_auto.safe_auto_batch)
        self.assertTrue(safe_auto.safe_auto_yes)
        args = SimpleNamespace(
            interactive=False,
            preview=False,
            execute=True,
            status=None,
            resume=None,
        )
        with patch("gluster_heal_tool.simple_mode.sys.stdin", io.StringIO()):
            with patch("gluster_heal_tool.simple_mode.sys.stdout", io.StringIO()):
                with patch("gluster_heal_tool.simple_mode.sys.stderr", io.StringIO()):
                    from gluster_heal_tool.simple_mode import run_simple_command

                    self.assertEqual(2, run_simple_command(args))

    def test_non_tty_safe_auto_requires_explicit_yes_authorization(self) -> None:
        from gluster_heal_tool.simple_mode import run_simple_command

        parser = build_parser()
        args = parser.parse_args(["repair", "--volume", "gtest3", "--safe-auto"])
        with patch("gluster_heal_tool.simple_mode.sys.stdin", io.StringIO()):
            with patch("gluster_heal_tool.simple_mode.sys.stdout", io.StringIO()):
                with patch("gluster_heal_tool.simple_mode.sys.stderr", io.StringIO()) as error:
                    self.assertEqual(2, run_simple_command(args))
                    self.assertIn("--batch -y", error.getvalue())

        authorized = parser.parse_args(
            ["repair", "--volume", "gtest3", "--safe-auto", "--batch", "-y"]
        )
        with patch("gluster_heal_tool.simple_mode.run_simple_preview", return_value=0) as preview:
            with patch("gluster_heal_tool.simple_mode.sys.stdin", io.StringIO()):
                with patch("gluster_heal_tool.simple_mode.sys.stdout", io.StringIO()):
                    with patch("gluster_heal_tool.simple_mode.sys.stderr", io.StringIO()):
                        self.assertEqual(0, run_simple_command(authorized))
        self.assertTrue(preview.call_args.kwargs["safe_auto"])
        self.assertTrue(preview.call_args.kwargs["safe_auto_authorized"])
        self.assertTrue(preview.call_args.kwargs["non_interactive_safe_auto"])

        yes_only = parser.parse_args(["repair", "--volume", "gtest3", "-y"])
        with patch("gluster_heal_tool.simple_mode.run_simple_preview", return_value=0) as preview:
            with patch("gluster_heal_tool.simple_mode.sys.stdin", io.StringIO()):
                with patch("gluster_heal_tool.simple_mode.sys.stdout", io.StringIO()):
                    with patch("gluster_heal_tool.simple_mode.sys.stderr", io.StringIO()):
                        self.assertEqual(0, run_simple_command(yes_only))
        self.assertTrue(preview.call_args.kwargs["safe_auto"])
        self.assertTrue(preview.call_args.kwargs["safe_auto_authorized"])
        self.assertTrue(preview.call_args.kwargs["non_interactive_safe_auto"])

    def test_safe_auto_tty_prompts_once_and_executes_ready_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            ready = _result(
                action_id="repair:/data/file",
                action_type="repair_file",
                logical_path="/data/file",
                status="planned",
                strategy="restore_missing_replica",
            )
            report = {
                "summary": {
                    "executed_actions": 1,
                    "completed_actions": 1,
                    "failed_actions": 0,
                },
                "actions": [{"action_id": ready.action_id, "status": "completed"}],
            }
            with patch(
                "gluster_heal_tool.simple_interactive._execute_ready_batch",
                return_value=(True, report),
            ) as execute, patch(
                "gluster_heal_tool.simple_interactive._refresh_evidence",
                return_value=([], {}),
            ):
                output = io.StringIO()
                result = run_simple_interaction(
                    paths,
                    {"volume": "gtest3", "run_dir": str(paths.root)},
                    [ready],
                    input_stream=io.StringIO("a" + chr(10)),
                    output_stream=output,
                    allow_execution=True,
                    safe_auto=True,
                )
            self.assertEqual(0, result)
            execute.assert_called_once()
            self.assertIn("Choice: [a] authorize this batch", output.getvalue())

    def test_safe_auto_does_not_reprompt_for_later_ready_batches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            first = _result(
                action_id="repair:/data/first",
                action_type="repair_file",
                logical_path="/data/first",
                status="planned",
                strategy="restore_missing_replica",
            )
            second = _result(
                action_id="repair:/data/second",
                action_type="repair_file",
                logical_path="/data/second",
                status="planned",
                strategy="restore_missing_replica",
            )
            reports = [
                (
                    True,
                    {
                        "summary": {
                            "executed_actions": 1,
                            "completed_actions": 1,
                            "failed_actions": 0,
                        },
                        "actions": [{"action_id": first.action_id, "status": "completed"}],
                    },
                ),
                (
                    True,
                    {
                        "summary": {
                            "executed_actions": 1,
                            "completed_actions": 1,
                            "failed_actions": 0,
                        },
                        "actions": [{"action_id": second.action_id, "status": "completed"}],
                    },
                ),
            ]
            with patch(
                "gluster_heal_tool.simple_interactive._execute_ready_batch",
                side_effect=reports,
            ) as execute, patch(
                "gluster_heal_tool.simple_interactive._refresh_evidence",
                side_effect=[([second], {}), ([], {})],
            ):
                output = io.StringIO()
                result = run_simple_interaction(
                    paths,
                    {"volume": "gtest3", "run_dir": str(paths.root)},
                    [first],
                    input_stream=io.StringIO("a" + chr(10)),
                    output_stream=output,
                    allow_execution=True,
                    safe_auto=True,
                )
            self.assertEqual(0, result)
            self.assertEqual(2, execute.call_count)
            self.assertIn("Safe-auto authorization is active", output.getvalue())

    def test_non_tty_safe_auto_stops_at_decision_with_resume_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            review = _result(
                action_id="review:/data/file",
                action_type="review_type_mismatch",
                logical_path="/data/file",
                status="review",
                strategy="review_type_mismatch",
            )
            output = io.StringIO()
            result = run_simple_interaction(
                paths,
                {"volume": "gtest3", "run_dir": str(paths.root)},
                [review],
                input_stream=io.StringIO(),
                output_stream=output,
                allow_execution=True,
                safe_auto=True,
                safe_auto_authorized=True,
                non_interactive_safe_auto=True,
            )
            self.assertEqual(2, result)
            self.assertIn("Safe-auto stopped before an unresolved operator decision", output.getvalue())
            self.assertIn("--resume", output.getvalue())
            status = json.loads(paths.status.read_text(encoding="utf-8"))
            self.assertEqual("simple-safe-auto-stopped-at-decision", status["phase"])

    def test_clear_ready_actions_are_grouped_without_decision_cards(self) -> None:
        ready = _result(
            action_id="repair:/data/file",
            action_type="repair_file",
            logical_path="/data/file",
            status="planned",
            strategy="restore_missing_replica",
        )
        cards, ready_batch = build_simple_decision_cards([ready])
        self.assertEqual([], cards)
        self.assertEqual([ready], ready_batch)

    def test_ready_batch_dependency_closure_keeps_unresolved_child_out(self) -> None:
        dependency = _result(
            action_id="review:/data/parent",
            action_type="review_type_mismatch",
            logical_path="/data/parent",
            status="review",
            strategy="review_type_mismatch",
        )
        child = _result(
            action_id="repair:/data/parent/child",
            action_type="repair_file",
            logical_path="/data/parent/child",
            status="planned",
            strategy="restore_missing_replica",
        )
        child.depends_on = [dependency.action_id]
        cards, ready_batch = build_simple_decision_cards([dependency, child])
        self.assertEqual([], ready_batch)
        self.assertEqual(
            {dependency.action_id, child.action_id},
            {card.decision_id for card in cards},
        )

    def test_execute_ready_batch_requires_explicit_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            ready = _result(
                action_id="repair:/data/file",
                action_type="repair_file",
                logical_path="/data/file",
                status="planned",
                strategy="restore_missing_replica",
            )
            summary = {
                "volume": "gtest3",
                "run_dir": str(paths.root),
                "write_occurred": False,
            }
            with patch("gluster_heal_tool.simple_interactive._execute_ready_batch") as execute:
                output = io.StringIO()
                result = run_simple_interaction(
                    paths,
                    summary,
                    [ready],
                    input_stream=io.StringIO("d\n"),
                    output_stream=output,
                    allow_execution=True,
                )
            self.assertEqual(0, result)
            execute.assert_not_called()
            self.assertIn("Ready-safe batch deferred", output.getvalue())
            status = json.loads(paths.status.read_text(encoding="utf-8"))
            self.assertFalse(status["write_occurred"])
            interaction = json.loads(paths.interaction.read_text(encoding="utf-8"))
            self.assertEqual("deferred", interaction["ready_batch_state"])

    def test_ready_batch_preview_is_complete_and_can_be_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            ready = _result(
                action_id="repair:/data/file",
                action_type="repair_file",
                logical_path="/data/file",
                status="planned",
                strategy="restore_missing_replica",
            )
            ready.steps = [
                ApplyStep(
                    step_id="repair:/data/file:01",
                    step_type="restore_backend",
                    host="brick-b",
                    source_path="/stage/file",
                    target_path="/brick/b/file",
                    command_preview=["cp", "-a", "/stage/file", "/brick/b/file"],
                )
            ]
            ready.revert_steps = [
                ApplyStep(
                    step_id="repair:/data/file:revert:01",
                    step_type="remove_restored_backend",
                    host="brick-b",
                    target_path="/brick/b/file",
                    command_preview=["rm", "-f", "/brick/b/file"],
                )
            ]
            ready.backup_artifacts = [
                BackupArtifact(
                    host="brick-b",
                    source_path="/brick/b/file",
                    backup_path="/tmp/backup/brick-b/file",
                    kind="backend",
                    estimated_bytes=10,
                )
            ]
            summary = {
                "volume": "gtest3",
                "run_dir": str(paths.root),
                "write_occurred": False,
            }
            with patch(
                "gluster_heal_tool.simple_interactive._execute_ready_batch"
            ) as execute:
                output = io.StringIO()
                result = run_simple_interaction(
                    paths,
                    summary,
                    [ready],
                    input_stream=io.StringIO("s\n"),
                    output_stream=output,
                    allow_execution=True,
                )

            self.assertEqual(0, result)
            execute.assert_not_called()
            rendered = output.getvalue()
            self.assertIn(
                "host=brick-b; source=/stage/file; target=/brick/b/file",
                rendered,
            )
            self.assertIn("rollback/revert steps:", rendered)
            self.assertIn("/tmp/backup/brick-b/file", rendered)
            self.assertIn("[s] skip it (no changes)", rendered)
            self.assertIn("Ready batch skipped; no changes were made.", rendered)
            interaction = json.loads(
                paths.interaction.read_text(encoding="utf-8")
            )
            self.assertEqual("skipped", interaction["ready_batch_state"])

    def test_interrupted_batch_preserves_unknown_write_state_and_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            ready = _result(
                action_id="repair:/data/file",
                action_type="repair_file",
                logical_path="/data/file",
                status="planned",
                strategy="restore_missing_replica",
            )
            summary = {
                "volume": "gtest3",
                "run_dir": str(paths.root),
                "write_occurred": False,
            }
            with patch(
                "gluster_heal_tool.simple_interactive._execute_ready_batch",
                return_value=(
                    False,
                    {
                        "exit_code": 130,
                        "interrupted": True,
                        "actions": [],
                        "summary": {"executed_actions": 1, "completed_actions": 0, "failed_actions": 1},
                    },
                ),
            ):
                output = io.StringIO()
                result = run_simple_interaction(
                    paths,
                    summary,
                    [ready],
                    input_stream=io.StringIO("a\n"),
                    output_stream=output,
                    allow_execution=True,
                )
            self.assertEqual(2, result)
            self.assertIn("Do not retry", output.getvalue())
            self.assertIn("--status", output.getvalue())
            status = json.loads(paths.status.read_text(encoding="utf-8"))
            self.assertEqual("simple-interactive-execution-interrupted", status["phase"])
            self.assertEqual("unknown", status["execution_write_state"])
            self.assertTrue(status["write_occurred"])
            self.assertTrue(status["write_outcome_unknown"])
            interaction = json.loads(paths.interaction.read_text(encoding="utf-8"))
            self.assertEqual("execution-interrupted", interaction["ready_batch_state"])
            self.assertTrue(interaction["write_outcome_unknown"])
            self.assertEqual("write-unknown", interaction["mode"])

    def test_reportless_execution_is_explicitly_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            with patch("gluster_heal_tool.cli.main", return_value=2):
                from gluster_heal_tool.simple_interactive import _execute_ready_batch

                success, report = _execute_ready_batch(paths, {}, io.StringIO())

        self.assertFalse(success)
        self.assertTrue(report["write_outcome_unknown"])
        self.assertEqual([], report["actions"])

    def test_all_skipped_authorized_batch_records_no_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            ready = _result(
                action_id="repair:/data/file",
                action_type="repair_file",
                logical_path="/data/file",
                status="planned",
                strategy="restore_missing_replica",
            )
            summary = {"volume": "gtest3", "run_dir": str(paths.root), "write_occurred": False}
            report = {
                "summary": {"executed_actions": 0, "completed_actions": 0, "failed_actions": 0},
                "actions": [],
            }
            with patch(
                "gluster_heal_tool.simple_interactive._execute_ready_batch",
                return_value=(True, report),
            ), patch(
                "gluster_heal_tool.simple_interactive._refresh_evidence",
                return_value=([], {}),
            ):
                result = run_simple_interaction(
                    paths,
                    summary,
                    [ready],
                    input_stream=io.StringIO("a\n"),
                    output_stream=io.StringIO(),
                    allow_execution=True,
                )

            self.assertEqual(0, result)
            status = json.loads(paths.status.read_text(encoding="utf-8"))
            saved_summary = json.loads((paths.root / "summary.json").read_text(encoding="utf-8"))
            interaction = json.loads(paths.interaction.read_text(encoding="utf-8"))
            for artifact in (status, saved_summary, interaction):
                self.assertFalse(artifact["write_occurred"])
                self.assertFalse(artifact["write_outcome_unknown"])

    def test_all_skipped_authorized_batch_preserves_prior_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            paths.status.write_text(
                json.dumps({"write_occurred": True, "execution_write_state": "completed"}),
                encoding="utf-8",
            )
            ready = _result(
                action_id="repair:/data/file",
                action_type="repair_file",
                logical_path="/data/file",
                status="planned",
                strategy="restore_missing_replica",
            )
            summary = {"volume": "gtest3", "run_dir": str(paths.root), "write_occurred": True}
            report = {
                "summary": {"executed_actions": 0, "completed_actions": 0, "failed_actions": 0},
                "actions": [],
            }
            with patch(
                "gluster_heal_tool.simple_interactive._execute_ready_batch",
                return_value=(True, report),
            ), patch(
                "gluster_heal_tool.simple_interactive._refresh_evidence",
                return_value=([], {}),
            ):
                result = run_simple_interaction(
                    paths,
                    summary,
                    [ready],
                    input_stream=io.StringIO("a\n"),
                    output_stream=io.StringIO(),
                    allow_execution=True,
                )

            self.assertEqual(0, result)
            status = json.loads(paths.status.read_text(encoding="utf-8"))
            saved_summary = json.loads((paths.root / "summary.json").read_text(encoding="utf-8"))
            interaction = json.loads(paths.interaction.read_text(encoding="utf-8"))
            for artifact in (status, saved_summary, interaction):
                self.assertTrue(artifact["write_occurred"])
            self.assertEqual("completed", status["execution_write_state"])

    def test_failed_refresh_keeps_completed_write_in_every_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            ready = _result(
                action_id="repair:/data/file",
                action_type="repair_file",
                logical_path="/data/file",
                status="planned",
                strategy="restore_missing_replica",
            )
            summary = {"volume": "gtest3", "run_dir": str(paths.root), "write_occurred": False}
            report = {
                "summary": {"executed_actions": 1, "completed_actions": 1, "failed_actions": 0},
                "actions": [{"action_id": ready.action_id, "status": "completed"}],
            }
            with patch(
                "gluster_heal_tool.simple_interactive._execute_ready_batch",
                return_value=(True, report),
            ), patch(
                "gluster_heal_tool.simple_interactive._refresh_evidence",
                side_effect=RuntimeError("evidence host unavailable"),
            ):
                result = run_simple_interaction(
                    paths,
                    summary,
                    [ready],
                    input_stream=io.StringIO("a\n"),
                    output_stream=io.StringIO(),
                    allow_execution=True,
                )

            self.assertEqual(2, result)
            status = json.loads(paths.status.read_text(encoding="utf-8"))
            saved_summary = json.loads((paths.root / "summary.json").read_text(encoding="utf-8"))
            interaction = json.loads(paths.interaction.read_text(encoding="utf-8"))
            for artifact in (status, saved_summary, interaction):
                self.assertTrue(artifact["write_occurred"])
            self.assertEqual("simple-interactive-evidence-refresh-blocked", status["phase"])
            self.assertEqual("evidence-refresh-failed", interaction["ready_batch_state"])
            self.assertEqual("executed", interaction["mode"])

    def test_authorized_batch_refreshes_and_does_not_replay_completed_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            ready = _result(
                action_id="repair:/data/file",
                action_type="repair_file",
                logical_path="/data/file",
                status="planned",
                strategy="restore_missing_replica",
            )
            summary = {
                "volume": "gtest3",
                "run_dir": str(paths.root),
                "write_occurred": False,
            }
            execution_report = {
                "summary": {
                    "executed_actions": 1,
                    "completed_actions": 1,
                    "failed_actions": 0,
                },
                "actions": [
                    {
                        "action_id": ready.action_id,
                        "status": "completed",
                    }
                ],
            }
            with patch(
                "gluster_heal_tool.simple_interactive._execute_ready_batch",
                return_value=(True, execution_report),
            ) as execute, patch(
                "gluster_heal_tool.simple_interactive._refresh_evidence",
                return_value=([], {}),
            ) as refresh:
                output = io.StringIO()
                result = run_simple_interaction(
                    paths,
                    summary,
                    [ready],
                    input_stream=io.StringIO("a\n"),
                    output_stream=output,
                    allow_execution=True,
                )
            self.assertEqual(0, result)
            execute.assert_called_once()
            refresh.assert_called_once()
            self.assertIn("fresh evidence was collected", output.getvalue())
            status = json.loads(paths.status.read_text(encoding="utf-8"))
            self.assertTrue(status["write_occurred"])
            self.assertEqual("simple-interactive-clear", status["phase"])
            interaction = json.loads(paths.interaction.read_text(encoding="utf-8"))
            self.assertTrue(interaction["write_occurred"])
            self.assertEqual("executed", interaction["mode"])
            events = paths.events.read_text(encoding="utf-8")
            self.assertIn("ready-batch-executed", events)

    def test_review_card_exposes_preservation_recommendation(self) -> None:
        review = _result(
            action_id="review:/data/file",
            action_type="review_type_mismatch",
            logical_path="/data/file",
            status="review",
            strategy="review_type_mismatch",
        )
        cards, ready_batch = build_simple_decision_cards([review])
        self.assertEqual([], ready_batch)
        self.assertEqual(1, len(cards))
        self.assertEqual("Quarantine both file and directory branches", cards[0].recommendation)
        self.assertEqual("heuristic", cards[0].confidence)

    def test_type_mismatch_loser_recommendation_exposes_executable_choice(self) -> None:
        review = _result(
            action_id="review:/data/type-mismatch",
            action_type="review_type_mismatch",
            logical_path="/data/type-mismatch",
            status="review",
            strategy="review_type_mismatch",
            recommended_choice="quarantine_loser",
            recommended_reason="complete non-overlapping backend mtime ranges",
        )
        assistants = {
            "items": [
                {
                    "action_id": review.action_id,
                    "details": {
                        "action": {
                            "action_type": "review_type_mismatch",
                            "repair_strategy": "review_type_mismatch",
                            "recommended_choice": "quarantine_loser",
                            "type_mismatch_loser": "file",
                            "file_copies": [
                                {"host": "brick-a", "backend": "/brick/a/alpha", "mtime": 100},
                            ],
                            "directory_copies": [
                                {"host": "brick-b", "backend": "/brick/b/alpha", "mtime": 200},
                            ],
                        },
                    },
                }
            ]
        }

        cards, ready_batch = build_simple_decision_cards([review], assistants)
        self.assertEqual([], ready_batch)
        self.assertEqual(1, len(cards))
        decisions = [spec["decision"] for spec in cards[0].available_choices]
        self.assertIn({"type_mismatch_choice": "quarantine_loser"}, decisions)
        self.assertIn({"type_mismatch_choice": "quarantine_both"}, decisions)
        loser_choice = next(
            spec
            for spec in cards[0].available_choices
            if spec["decision"] == {"type_mismatch_choice": "quarantine_loser"}
        )
        loser_preview = "\n".join(loser_choice["preview"])
        self.assertIn("brick-a: /brick/a/alpha", loser_preview)
        self.assertNotIn("brick-b: /brick/b/alpha", loser_preview)
        both_choice = next(
            spec
            for spec in cards[0].available_choices
            if spec["decision"] == {"type_mismatch_choice": "quarantine_both"}
        )
        both_preview = "\n".join(both_choice["preview"])
        self.assertIn("brick-a: /brick/a/alpha", both_preview)
        self.assertIn("brick-a: /brick/a/alpha ->", both_preview)
        self.assertIn("(file; GFID not recorded)", both_preview)
        self.assertIn("brick-b: /brick/b/alpha ->", both_preview)
        self.assertIn("(directory; GFID not recorded)", both_preview)

    def test_type_mismatch_directory_loser_preview_targets_only_directory_branch(self) -> None:
        review = _result(
            action_id="review:/data/type-mismatch-directory",
            action_type="review_type_mismatch",
            logical_path="/data/type-mismatch-directory",
            status="review",
            strategy="review_type_mismatch",
            recommended_choice="quarantine_loser",
        )
        assistants = {
            "items": [
                {
                    "action_id": review.action_id,
                    "details": {
                        "action": {
                            "action_type": "review_type_mismatch",
                            "repair_strategy": "review_type_mismatch",
                            "recommended_choice": "quarantine_loser",
                            "type_mismatch_loser": "directory",
                            "file_copies": [
                                {"host": "brick-a", "backend": "/brick/shared/alpha", "mtime": 200},
                            ],
                            "directory_copies": [
                                {"host": "brick-b", "backend": "/brick/shared/alpha", "mtime": 100},
                            ],
                        },
                    },
                }
            ]
        }

        cards, ready_batch = build_simple_decision_cards([review], assistants)

        self.assertEqual([], ready_batch)
        loser_choice = next(
            spec
            for spec in cards[0].available_choices
            if spec["decision"] == {"type_mismatch_choice": "quarantine_loser"}
        )
        preview = "\n".join(loser_choice["preview"])
        self.assertIn("brick-b: /brick/shared/alpha", preview)
        self.assertNotIn("brick-a: /brick/shared/alpha", preview)

    def test_directory_side_preview_selects_gfid_not_shared_backend_path(self) -> None:
        review = _result(
            action_id="review:/data/directory-conflict",
            action_type="review_directory_gfid_conflict",
            logical_path="/data/directory-conflict",
            status="review",
            strategy="ambiguous_directory_gfid_conflict",
            recommended_choice="quarantine_loser",
        )
        assistants = {
            "items": [
                {
                    "action_id": review.action_id,
                    "details": {
                        "action": {
                            "action_type": "review_directory_gfid_conflict",
                            "repair_strategy": "ambiguous_directory_gfid_conflict",
                            "directory_canonical_host": "brick-a",
                            "directory_canonical_backend": "/brick/shared/alpha",
                            "directory_canonical_gfid": "gfid-a",
                            "directory_copies": [
                                {
                                    "host": "brick-a",
                                    "backend": "/brick/shared/alpha",
                                    "identity": "gfid-a",
                                },
                                {
                                    "host": "brick-b",
                                    "backend": "/brick/shared/alpha",
                                    "identity": "gfid-b",
                                    "gfid_path": "/brick/.glusterfs/gf/id/gfid-b",
                                },
                            ],
                        },
                    },
                }
            ]
        }

        cards, ready_batch = build_simple_decision_cards([review], assistants)

        self.assertEqual([], ready_batch)
        side_choice = next(
            spec
            for spec in cards[0].available_choices
            if spec["decision"]
            == {
                "directory_choice": "quarantine_side",
                "directory_quarantine_identity": "gfid-b",
            }
        )
        preview = "\n".join(side_choice["preview"])
        self.assertIn("brick-b: /brick/shared/alpha", preview)
        self.assertIn("GFID gfid-b", preview)
        self.assertIn(".gluster-quarantine.quarantine_side.", preview)
        self.assertIn("GFID link: /brick/.glusterfs/gf/id/gfid-b ->", preview)
        self.assertIn("rollback: move every listed quarantine target back", preview)
        self.assertIn("protection:", preview)
        self.assertNotIn("brick-a: /brick/shared/alpha", preview)

    def test_directory_gfid_tie_offers_each_side_and_both(self) -> None:
        review = _result(
            action_id="review:/data/directory-gfid-tie",
            action_type="review_directory_gfid_conflict",
            logical_path="/data/directory-gfid-tie",
            status="review",
            strategy="rename_conflicting_directory",
            recommended_choice="quarantine_both",
        )
        assistants = {
            "items": [
                {
                    "action_id": review.action_id,
                    "details": {
                        "action": {
                            "action_type": "review_directory_gfid_conflict",
                            "repair_strategy": "rename_conflicting_directory",
                            "recommended_choice": "quarantine_both",
                            "directory_canonical_host": "brick-a",
                            "directory_canonical_gfid": "gfid-a",
                            "directory_copies": [
                                {
                                    "host": "brick-a",
                                    "backend": "/brick/a/directory-tie",
                                    "identity": "gfid-a",
                                },
                                {
                                    "host": "brick-b",
                                    "backend": "/brick/b/directory-tie",
                                    "identity": "gfid-a",
                                },
                                {
                                    "host": "brick-c",
                                    "backend": "/brick/c/directory-tie",
                                    "identity": "gfid-b",
                                },
                                {
                                    "host": "brick-d",
                                    "backend": "/brick/d/directory-tie",
                                    "identity": "gfid-b",
                                },
                            ],
                        },
                    },
                }
            ]
        }

        cards, ready_batch = build_simple_decision_cards([review], assistants)

        self.assertEqual([], ready_batch)
        decisions = [spec["decision"] for spec in cards[0].available_choices]
        self.assertEqual(
            [
                {
                    "directory_choice": "quarantine_side",
                    "directory_quarantine_identity": "gfid-a",
                },
                {
                    "directory_choice": "quarantine_side",
                    "directory_quarantine_identity": "gfid-b",
                },
                {"directory_choice": "quarantine_both"},
            ],
            decisions,
        )
        labels = [spec["label"] for spec in cards[0].available_choices]
        self.assertIn("GFID gfid-a (brick-a, brick-b)", labels[0])
        self.assertIn("GFID gfid-b (brick-c, brick-d)", labels[1])

    def test_directory_presence_tie_offers_both_not_loser_quarantine(self) -> None:
        review = _result(
            action_id="review:/data/directory-tie",
            action_type="review_directory_gfid_conflict",
            logical_path="/data/directory-tie",
            status="review",
            strategy="ambiguous_directory_presence_tie",
            recommended_choice="quarantine_both",
        )
        assistants = {
            "items": [
                {
                    "action_id": review.action_id,
                    "details": {
                        "action": {
                            "action_type": "review_directory_gfid_conflict",
                            "repair_strategy": "ambiguous_directory_presence_tie",
                            "recommended_choice": "quarantine_both",
                            "directory_canonical_host": "brick-a",
                            "directory_canonical_gfid": "gfid-a",
                            "directory_copies": [
                                {
                                    "host": "brick-a",
                                    "backend": "/brick/a/directory-tie",
                                    "identity": "gfid-a",
                                },
                            ],
                        },
                    },
                }
            ]
        }

        cards, ready_batch = build_simple_decision_cards([review], assistants)

        self.assertEqual([], ready_batch)
        self.assertEqual(1, len(cards))
        decisions = [spec["decision"] for spec in cards[0].available_choices]
        self.assertIn({"directory_choice": "quarantine_both"}, decisions)
        self.assertNotIn({"directory_choice": "quarantine_loser"}, decisions)

    def test_completed_quarantine_both_all_missing_is_terminal(self) -> None:
        review = _result(
            action_id="review:/data/quarantined-directory",
            action_type="review_directory_gfid_conflict",
            logical_path="/data/quarantined-directory",
            status="review",
            strategy="review_directory_presence",
        )
        assistants = {
            "items": [
                {
                    "action_id": review.action_id,
                    "details": {
                        "action": {
                            "repair_strategy": "review_directory_presence",
                            "healthy_hosts": [],
                            "missing_hosts": [
                                "brick-a",
                                "brick-b",
                                "brick-c",
                                "brick-d",
                            ],
                            "brick_roles_by_host": {
                                "brick-a": "data",
                                "brick-b": "data",
                                "brick-c": "data",
                                "brick-d": "data",
                            },
                        },
                    },
                }
            ]
        }

        cards, ready_batch = build_simple_decision_cards(
            [review],
            assistants,
            completed_preservation_action_ids={review.action_id},
        )

        self.assertEqual([], cards)
        self.assertEqual([], ready_batch)

    def test_completed_quarantine_both_partial_absence_still_requires_review(self) -> None:
        review = _result(
            action_id="review:/data/partly-quarantined-directory",
            action_type="review_directory_gfid_conflict",
            logical_path="/data/partly-quarantined-directory",
            status="review",
            strategy="review_directory_presence",
        )
        assistants = {
            "items": [
                {
                    "action_id": review.action_id,
                    "details": {
                        "action": {
                            "repair_strategy": "review_directory_presence",
                            "healthy_hosts": ["brick-d"],
                            "missing_hosts": ["brick-a", "brick-b", "brick-c"],
                            "brick_roles_by_host": {
                                "brick-a": "data",
                                "brick-b": "data",
                                "brick-c": "data",
                                "brick-d": "data",
                            },
                        },
                    },
                }
            ]
        }

        cards, ready_batch = build_simple_decision_cards(
            [review],
            assistants,
            completed_preservation_action_ids={review.action_id},
        )

        self.assertEqual(1, len(cards))
        self.assertEqual([], ready_batch)

    def test_all_missing_directory_recommends_recovery_or_skip(self) -> None:
        review = _result(
            action_id="review:/data/missing-directory",
            action_type="review_directory_metadata",
            logical_path="/data/missing-directory",
            status="review",
            strategy="review_directory_presence",
        )
        assistants = {
            "items": [
                {
                    "action_id": review.action_id,
                    "details": {
                        "action": {
                            "repair_strategy": "review_directory_presence",
                        },
                        "directory_tie": {
                            "branch_type": "missing",
                            "classification": {
                                "missing_hosts": [
                                    "brick-a",
                                    "brick-b",
                                    "brick-c",
                                    "brick-d",
                                ],
                            },
                        },
                    },
                }
            ]
        }

        cards, ready_batch = build_simple_decision_cards([review], assistants)

        self.assertEqual([], ready_batch)
        self.assertEqual(1, len(cards))
        self.assertIn("authoritative backup", cards[0].recommendation)
        self.assertIn("skip", cards[0].recommendation)
        self.assertIn("nothing left to quarantine", cards[0].rationale)
        self.assertEqual((), cards[0].available_choices)

    def test_content_different_assistant_recommends_quarantine_and_exposes_choice(self) -> None:
        review = _result(
            action_id="review:/data/tied-file",
            action_type="review_entry_split_brain",
            logical_path="/data/tied-file",
            status="review",
            strategy="ambiguous_entry_split_brain_file",
        )
        assistants = {
            "items": [
                {
                    "action_id": review.action_id,
                    "details": {
                        "action": {
                            "action_type": "review_entry_split_brain",
                            "repair_strategy": "ambiguous_entry_split_brain_file",
                            "file_copies": [
                                {"host": "brick-a", "backend": "/brick/a/tied-file", "identity": "gfid-a"},
                                {"host": "brick-b", "backend": "/brick/b/tied-file", "identity": "gfid-b"},
                            ],
                        },
                        "checksum": {"outcome": "content-different"},
                    },
                }
            ]
        }

        cards, ready_batch = build_simple_decision_cards([review], assistants)
        self.assertEqual([], ready_batch)
        self.assertEqual(1, len(cards))
        self.assertEqual("Quarantine both file copies", cards[0].recommendation)
        self.assertIn(
            {"file_choice": "quarantine_both"},
            [spec["decision"] for spec in cards[0].available_choices],
        )

    def test_arbiter_backed_conflict_keeps_planner_quarantine_loser_recommendation(self) -> None:
        review = _result(
            action_id="review:/data/arbiter-conflict",
            action_type="review_entry_split_brain",
            logical_path="/data/arbiter-conflict",
            status="review",
            strategy="arbiter_backed_data_identity_conflict",
            recommended_choice="quarantine_loser",
            recommended_reason="The matching data brick plus arbiter identity is the tie-breaker.",
        )
        assistants = {
            "items": [
                {
                    "action_id": review.action_id,
                    "details": {
                        "action": {
                            "action_type": "review_entry_split_brain",
                            "repair_strategy": "arbiter_backed_data_identity_conflict",
                            "recommended_choice": "quarantine_loser",
                            "file_copies": [
                                {"host": "data-a", "backend": "/brick/a/file", "identity": "gfid-a"},
                                {"host": "data-b", "backend": "/brick/b/file", "identity": "gfid-b"},
                            ],
                        },
                        "checksum": {"outcome": "content-different"},
                    },
                }
            ]
        }

        cards, ready_batch = build_simple_decision_cards([review], assistants)

        self.assertEqual([], ready_batch)
        self.assertEqual("quarantine loser", cards[0].recommendation)
        self.assertIn("arbiter identity is the tie-breaker", cards[0].rationale)
        self.assertIn(
            {"file_choice": "quarantine_loser"},
            [spec["decision"] for spec in cards[0].available_choices],
        )
        self.assertIn(
            {"file_choice": "quarantine_both"},
            [spec["decision"] for spec in cards[0].available_choices],
        )

    def test_clear_plan_never_reads_input_or_executes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            summary = {
                "volume": "gtest3",
                "run_dir": str(paths.root),
                "artifacts": {"status": str(paths.status)},
            }
            ready = _result(
                action_id="repair:/data/file",
                action_type="repair_file",
                logical_path="/data/file",
                status="planned",
                strategy="restore_missing_replica",
            )
            output = io.StringIO()
            result = run_simple_interaction(
                paths,
                summary,
                [ready],
                input_stream=io.StringIO("q\n"),
                output_stream=output,
            )
            self.assertEqual(0, result)
            self.assertIn("No sticking points require operator input.", output.getvalue())
            self.assertNotIn("Decision 1", output.getvalue())
            interaction = json.loads(paths.interaction.read_text(encoding="utf-8"))
            self.assertEqual("clear", interaction["state"])
            status = json.loads(paths.status.read_text(encoding="utf-8"))
            self.assertFalse(status["write_occurred"])

    def test_quit_persists_pending_sticking_point_for_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            review = _result(
                action_id="review:/data/file",
                action_type="review_type_mismatch",
                logical_path="/data/file",
                status="review",
                strategy="review_type_mismatch",
            )
            summary = {
                "volume": "gtest3",
                "run_dir": str(paths.root),
                "artifacts": {"status": str(paths.status)},
            }
            output = io.StringIO()
            run_simple_interaction(
                paths,
                summary,
                [review],
                input_stream=io.StringIO("q\n"),
                output_stream=output,
            )
            interaction = json.loads(paths.interaction.read_text(encoding="utf-8"))
            self.assertEqual("paused", interaction["state"])
            self.assertEqual("review:/data/file", interaction["pending_decision"])
            self.assertIn("repair --resume", output.getvalue())

            write_apply_results(paths.apply, [review])
            resume_output = io.StringIO()
            resume_simple_interaction(
                paths,
                summary,
                input_stream=io.StringIO("d\n"),
                output_stream=resume_output,
            )
            decisions = json.loads(paths.decisions.read_text(encoding="utf-8"))
            self.assertEqual("defer", decisions["decisions"]["review:/data/file"]["choice"])
            resumed = json.loads(paths.interaction.read_text(encoding="utf-8"))
            self.assertEqual("complete", resumed["state"])

    def test_support_choice_writes_bounded_handoff_without_a_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            review = _result(
                action_id="review:/data/file",
                action_type="review_type_mismatch",
                logical_path="/data/file",
                status="review",
                strategy="review_type_mismatch",
            )
            summary = {
                "volume": "gtest3",
                "run_dir": str(paths.root),
                "artifacts": {
                    "status": str(paths.status),
                    "plan": str(paths.plan),
                    "apply": str(paths.apply),
                },
            }
            output = io.StringIO()
            run_simple_interaction(
                paths,
                summary,
                [review],
                input_stream=io.StringIO("h\n"),
                output_stream=output,
            )
            handoff = paths.support_summary.read_text(encoding="utf-8")
            self.assertIn("LOCAL UNREDACTED", handoff)
            self.assertIn("Do not share it directly", handoff)
            self.assertIn("write occurred: no", handoff)
            self.assertIn("Do not include file payloads", handoff)
            self.assertNotIn("payload bytes:", handoff.lower())
            decisions = json.loads(paths.decisions.read_text(encoding="utf-8"))
            self.assertEqual("support-case", decisions["decisions"]["review:/data/file"]["choice"])

    def test_support_handoff_recovers_artifacts_missing_from_resume_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            review = _result(
                action_id="review:/data/file",
                action_type="review_persistent_split_brain_marker",
                logical_path="/data/file",
                status="review",
                strategy="review_persistent_split_brain_marker",
            )
            cards, _ready_batch = build_simple_decision_cards([review], {})

            handoff = _support_summary(
                paths,
                {
                    "volume": "gtest3",
                    "run_dir": str(paths.root),
                    "artifacts": {"status": str(paths.status)},
                },
                cards[0],
            )

            self.assertIn(f"status: {paths.status}", handoff)
            self.assertIn(f"manifest: {paths.manifest}", handoff)
            self.assertIn(f"observations: {paths.observations}", handoff)
            self.assertIn(f"plan: {paths.plan}", handoff)
            self.assertIn(f"apply: {paths.apply}", handoff)
            self.assertIn(f"decisions: {paths.decisions}", handoff)
            self.assertIn(f"assistants: {paths.assistants}", handoff)
            self.assertIn(f"execute_results: {paths.root / 'execute-results.json'}", handoff)
            self.assertIn(f"execute_results: {paths.root / 'execute-results.json'} (missing; not yet redacted)", handoff)
            self.assertNotIn("collected", handoff.lower())
            self.assertIn("Terminal condition:", handoff)
            self.assertIn("Do not retry source selection", handoff)

    def test_changed_assistant_fingerprint_invalidates_saved_choice(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            review = _result(
                action_id="review:/data/file",
                action_type="review_type_mismatch",
                logical_path="/data/file",
                status="review",
                strategy="review_type_mismatch",
                recommended_choice="keep review",
                recommended_reason="preserve the unresolved case",
            )
            summary = {
                "volume": "gtest3",
                "run_dir": str(paths.root),
                "artifacts": {"status": str(paths.status)},
            }
            assistants = paths.root / "assistants.json"
            assistants.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "action_id": review.action_id,
                                "fingerprint": "evidence-one",
                                "topology_fingerprint": "topology-one",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            run_simple_interaction(
                paths,
                summary,
                [review],
                input_stream=io.StringIO("r"),
                output_stream=io.StringIO(),
            )
            interaction = json.loads(paths.interaction.read_text(encoding="utf-8"))
            interaction["position"] = 0
            interaction["state"] = "pending"
            paths.interaction.write_text(json.dumps(interaction), encoding="utf-8")
            assistants.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "action_id": review.action_id,
                                "fingerprint": "evidence-two",
                                "topology_fingerprint": "topology-one",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            output = io.StringIO()
            run_simple_interaction(
                paths,
                summary,
                [review],
                input_stream=io.StringIO("q"),
                output_stream=output,
            )
            decisions = json.loads(paths.decisions.read_text(encoding="utf-8"))
            saved = decisions["decisions"][review.action_id]
            self.assertTrue(saved["stale"])
            self.assertIn("fingerprint changed", saved["invalidation_reason"])
            self.assertIn("Invalidated 1 saved choice", output.getvalue())

    def test_resume_refresh_invalidates_and_reopens_saved_choice(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            review = _result(
                action_id="review:/data/file",
                action_type="review_type_mismatch",
                logical_path="/data/file",
                status="review",
                strategy="review_type_mismatch",
                recommended_choice="keep review",
            )
            summary = {
                "volume": "gtest3",
                "run_dir": str(paths.root),
                "evidence_inputs": {"path": "/data/file"},
            }
            old_assistants = {
                "items": [
                    {
                        "action_id": review.action_id,
                        "fingerprint": "evidence-one",
                    }
                ]
            }
            new_assistants = {
                "items": [
                    {
                        "action_id": review.action_id,
                        "fingerprint": "evidence-two",
                    }
                ]
            }
            assistants_path = paths.root / "assistants.json"
            assistants_path.write_text(
                json.dumps(old_assistants),
                encoding="utf-8",
            )
            run_simple_interaction(
                paths,
                summary,
                [review],
                input_stream=io.StringIO("r\n"),
                output_stream=io.StringIO(),
            )
            write_apply_results(paths.apply, [review])

            def refresh(
                _paths_value: SimpleNamespace,
                _summary_value: dict[str, object],
            ) -> tuple[list[ApplyActionResult], dict[str, object]]:
                assistants_path.write_text(
                    json.dumps(new_assistants),
                    encoding="utf-8",
                )
                return [review], new_assistants

            output = io.StringIO()
            with patch(
                "gluster_heal_tool.simple_interactive._refresh_evidence",
                side_effect=refresh,
            ):
                resume_simple_interaction(
                    paths,
                    summary,
                    input_stream=io.StringIO("q\n"),
                    output_stream=output,
                )

            decisions = json.loads(paths.decisions.read_text(encoding="utf-8"))
            saved = decisions["decisions"][review.action_id]
            self.assertTrue(saved["stale"])
            self.assertIn("fingerprint changed", saved["invalidation_reason"])
            self.assertIn("Invalidated 1 saved choice", output.getvalue())
            self.assertIn("Resume refreshed the original", output.getvalue())
            interaction = json.loads(paths.interaction.read_text(encoding="utf-8"))
            self.assertEqual("paused", interaction["state"])
            self.assertEqual(review.action_id, interaction["pending_decision"])

    def test_safe_auto_authorization_survives_guided_replan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            review = _result(action_id="review:/data/file", action_type="review_entry_split_brain", logical_path="/data/file", status="review", strategy="native_heal_first", native_heal_first=True)
            ready = _result(action_id="repair:/data/file", action_type="repair_file", logical_path="/data/file", status="planned", strategy="restore_missing_replica")
            report = {"summary": {"executed_actions": 1, "completed_actions": 1, "failed_actions": 0}, "actions": [{"action_id": ready.action_id, "status": "completed"}]}
            with patch("gluster_heal_tool.simple_interactive._refresh_evidence", side_effect=[([ready], {}), ([], {})]), patch("gluster_heal_tool.simple_interactive._execute_ready_batch", return_value=(True, report)) as execute:
                output = io.StringIO()
                result = run_simple_interaction(paths, {"volume": "gtest3", "run_dir": str(paths.root)}, [review], input_stream=io.StringIO("1\nCONFIRM\n"), output_stream=output, allow_execution=True, safe_auto=True, safe_auto_authorized=True)
            self.assertEqual(0, result)
            execute.assert_called_once()
            self.assertIn("Safe-auto authorization is active", output.getvalue())

    def test_invalid_input_and_detail_controls_never_choose_a_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            review = _result(
                action_id="review:/data/file",
                action_type="review_type_mismatch",
                logical_path="/data/file",
                status="review",
                strategy="review_type_mismatch",
            )
            output = io.StringIO()
            result = run_simple_interaction(
                paths,
                {"volume": "gtest3", "run_dir": str(paths.root)},
                [review],
                input_stream=io.StringIO("e\nz\nd\n"),
                output_stream=output,
            )
            self.assertEqual(0, result)
            rendered = output.getvalue()
            self.assertIn("Evidence notes:", rendered)
            self.assertIn("Choose a displayed supported choice", rendered)
            self.assertIn("No changes were made in read-only simple mode.", rendered)
            decisions = json.loads(paths.decisions.read_text(encoding="utf-8"))
            self.assertEqual("defer", decisions["decisions"][review.action_id]["choice"])

    def test_eof_saves_pending_decision_for_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            review = _result(
                action_id="review:/data/file",
                action_type="review_type_mismatch",
                logical_path="/data/file",
                status="review",
                strategy="review_type_mismatch",
            )
            output = io.StringIO()
            result = run_simple_interaction(
                paths,
                {"volume": "gtest3", "run_dir": str(paths.root)},
                [review],
                input_stream=io.StringIO(""),
                output_stream=output,
            )
            self.assertEqual(0, result)
            interaction = json.loads(paths.interaction.read_text(encoding="utf-8"))
            self.assertEqual("paused", interaction["state"])
            self.assertEqual(review.action_id, interaction["pending_decision"])
            self.assertIn("Saved. Resume with:", output.getvalue())
            self.assertEqual({}, load_decisions(paths.decisions))

    def test_keyboard_interrupt_saves_pending_decision_for_resume(self) -> None:
        class InterruptingInput:
            def readline(self) -> str:
                raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _paths(Path(tmpdir))
            review = _result(
                action_id="review:/data/file",
                action_type="review_type_mismatch",
                logical_path="/data/file",
                status="review",
                strategy="review_type_mismatch",
            )
            output = io.StringIO()
            result = run_simple_interaction(
                paths,
                {"volume": "gtest3", "run_dir": str(paths.root)},
                [review],
                input_stream=InterruptingInput(),
                output_stream=output,
            )
            self.assertEqual(0, result)
            interaction = json.loads(paths.interaction.read_text(encoding="utf-8"))
            self.assertEqual("paused", interaction["state"])
            self.assertEqual(review.action_id, interaction["pending_decision"])
            self.assertIn("Saved. Resume with:", output.getvalue())
            self.assertEqual({}, load_decisions(paths.decisions))

    def test_preview_mode_is_noninteractive_and_read_only(self) -> None:
        from gluster_heal_tool.simple_mode import run_simple_command

        args = build_parser().parse_args(["repair", "--volume", "gtest3", "--preview"])
        with patch("gluster_heal_tool.simple_mode.run_simple_preview", return_value=0) as preview:
            self.assertEqual(0, run_simple_command(args))
        kwargs = preview.call_args.kwargs
        self.assertFalse(kwargs["interactive"])
        self.assertFalse(kwargs["execute"])
        self.assertFalse(kwargs["safe_auto"])
        self.assertFalse(kwargs["safe_auto_authorized"])

    def test_expert_help_describes_authority_boundary(self) -> None:
        parser = build_parser()
        subparsers = next(
            action for action in parser._subparsers._group_actions
            if hasattr(action, "choices")
        )
        help_text = subparsers.choices["repair"].format_help()
        self.assertIn("explicit expert authority semantics", help_text)
        self.assertNotIn("expert/granular presentation", help_text)


if __name__ == "__main__":
    unittest.main()
