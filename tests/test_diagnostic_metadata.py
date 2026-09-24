# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Exercise exports through real artifact writers and hostile saved evidence."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gluster_heal_tool.apply_reporting import write_apply_results
from gluster_heal_tool.diagnostic_metadata import MetadataExporter
from gluster_heal_tool.health import write_health_report
from gluster_heal_tool.manifest import write_manifest
from gluster_heal_tool.models import ApplyActionResult, ApplyStep, ManifestObject, PlanAction, ResolutionObservation
from gluster_heal_tool.planner_report import write_plan
from gluster_heal_tool.resolver import dump_observations
from gluster_heal_tool.status import update_status
from gluster_heal_tool.support_bundle import MAX_TEXT_BYTES, prepare_support_bundle
from gluster_heal_tool.worker import _inspect_afr_state


GFID = "11111111-2222-3333-4444-555555555555"
PRIVATE = "Private Company document and credential"


class DiagnosticMetadataTests(unittest.TestCase):
    def test_actual_writers_preserve_relationships_and_drop_free_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host = "private-node"
            logical = "/Private Company/report"
            observation = ResolutionObservation(
                host, logical, backend=logical, backend_size=42,
                backend_exists=True, backend_uid=1234, backend_gid=4321,
                backend_mode="-rw-r-----", backend_mode_bits=0o100640,
                backend_trusted_gfid=GFID, error=PRIVATE,
            )
            obj = ManifestObject(logical, "file", 2, file_gfids=[GFID],
                                 source_hosts=[host], observations={host: [observation]},
                                 notes=[PRIVATE], brick_roles_by_host={host: "data", "arbiter-node": "arbiter"})
            manifest_path = root / "manifest.json"
            write_manifest(manifest_path, {logical: obj}, volume="Private Company volume",
                           bricks=[{"host": host, "path": logical, "role": "data"}])
            plan_path = root / "plan.json"
            plan = PlanAction("private-action", logical, "repair_file", "file", 2,
                              winner_host=host, winner_size=42, winner_file_gfid=GFID,
                              notes=[PRIVATE])
            write_plan(plan_path, [plan], manifest_in=manifest_path,
                       manifest_payload=json.loads(manifest_path.read_text()))
            result = ApplyActionResult("private-action", logical, "repair_file", "dry-run",
                                       "/private-backups", "required", False, "planned",
                                       steps=[ApplyStep("private-step", "restore_via_mount", host=host,
                                                        source_path=logical, status="completed", returncode=0,
                                                        message=PRIVATE, command_preview=[PRIVATE])])
            apply_path = root / "apply.json"
            write_apply_results(apply_path, [result], plan_in=plan_path,
                                plan_payload=json.loads(plan_path.read_text()))
            execute_path = root / "execute.json"
            result.execution_mode = "execute"
            result.status = "completed"
            write_apply_results(execute_path, [result], previous_payload=json.loads(apply_path.read_text()))
            observations_path = root / "observations.json"
            dump_observations(observations_path, [observation])
            status_path = root / "status.json"
            update_status(status_path, volume="Private Company volume", write_occurred=True,
                          error=PRIVATE, summary={"completed_actions": 1, "message": PRIVATE})
            artifacts = {"manifest": manifest_path, "plan": plan_path, "apply": apply_path,
                         "execute_results": execute_path,
                         "observations": observations_path, "status": status_path}
            prepared = prepare_support_bundle(root / "bundle", artifacts,
                                              private_identifiers=["organization:Private Company"])
            exported = {name: json.loads((root / "bundle" / f"{name}.json").read_text())
                        for name in artifacts}
            serialized = json.dumps(exported)
            for forbidden in (PRIVATE, logical, host, "arbiter-node", "private-action",
                              "private-step", "1234", "4321", "command_preview", "fingerprint"):
                self.assertNotIn(forbidden, serialized)
            self.assertIn(GFID, serialized)
            metadata = {name: value["metadata"] for name, value in exported.items()}
            action = metadata["plan"]["actions"][0]
            applied = metadata["apply"]["actions"][0]
            self.assertEqual(action["action_id"], applied["action_id"])
            self.assertEqual(action["logical_path"], applied["logical_path"])
            observed = metadata["observations"]["observations"][0]
            self.assertEqual(42, observed["backend_size"])
            self.assertEqual("-rw-r-----", observed["backend_mode"])
            self.assertEqual(0o100640, observed["backend_mode_bits"])
            self.assertEqual("uid1", observed["backend_uid"])
            self.assertEqual(observed["host"], action["winner_host"])
            manifest_obj = metadata["manifest"]["objects"][action["logical_path"]]
            self.assertEqual(observed, manifest_obj["observations"][observed["host"]][0])
            self.assertIn("arbiter", manifest_obj["brick_roles_by_host"].values())
            self.assertEqual(0, applied["steps"][0]["returncode"])
            self.assertEqual("restore_via_mount", applied["steps"][0]["step_type"])
            self.assertEqual("completed", metadata["execute_results"]["actions"][0]["status"])
            self.assertTrue(metadata["status"]["write_occurred"])
            self.assertTrue(all(item["omitted_fields_or_lines"] > 0 for item in exported.values()))
            self.assertTrue(all(item["status"] == "exported_metadata" for item in prepared["inventory"].values()))

    def test_unknown_keys_and_wrong_types_do_not_bypass_projection(self) -> None:
        source = {"observations": [{
            "host": {"host": PRIVATE}, "backend_size": PRIVATE,
            "backend_mode": PRIVATE, "backend_uid": True, "backend_exists": "yes",
            "gfid": PRIVATE, "type": PRIVATE, "notes": [PRIVATE],
            "innocent": {"backend_size": PRIVATE}, PRIVATE: PRIVATE,
            "backend": PRIVATE,
        }]}
        result = MetadataExporter().export("observations", json.dumps(source))
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertEqual({"backend": "path1"}, result["metadata"]["observations"][0])
        self.assertGreater(result["omitted_fields_or_lines"], 5)

    def test_unknown_formats_versions_and_containers_are_not_copied(self) -> None:
        exporter = MetadataExporter()
        for name, content in (
            ("summary", PRIVATE), ("volume_info", PRIVATE), ("plan", "not json"),
            ("plan", '{"actions": "private text"}'),
            ("plan", '{"schema_version": 2, "actions": []}'),
            ("plan", '{"schema_version": true, "actions": []}'),
            ("observations", '[{"host":"private"}]'),
            ("status", '{"unknown": "private"}'),
            ("heal_info", PRIVATE),
        ):
            with self.subTest(name=name, content=content):
                self.assertIsNone(exporter.export(name, content))

    def test_explicit_identifiers_remove_even_otherwise_valid_gfids(self) -> None:
        exporter = MetadataExporter(private_values=[GFID.upper()])
        source = json.dumps({"observations": [{"gfid": GFID, "backend": GFID}]})
        result = exporter.export("observations", source)
        self.assertEqual({"backend": "path1"}, result["metadata"]["observations"][0])
        heal = exporter.export("heal_info", f"Brick node:/brick\n<gfid:{GFID}>\n")
        self.assertNotIn(GFID, json.dumps(heal))

    def test_actual_afr_writer_keeps_counters_and_aliases_volume_names(self) -> None:
        with (patch("os.path.lexists", return_value=True),
              patch("os.listxattr", return_value=["trusted.afr.Private Company-client-0"]),
              patch("os.getxattr", return_value=bytes.fromhex("000000000000000100000000"))):
            source = _inspect_afr_state("/Private Company/document")
        result = MetadataExporter().export("afr_inspection", json.dumps(source))
        self.assertNotIn("Private Company", json.dumps(result))
        self.assertEqual({"name": "afr1", "value_hex": "0x000000000000000100000000", "value_bytes": 12},
                         result["metadata"]["afr_xattrs"][0])
        source["afr_xattrs"][0]["value_hex"] = "0x" + "aa" * 100
        result = MetadataExporter().export("afr_inspection", json.dumps(source))
        self.assertNotIn("value_hex", result["metadata"]["afr_xattrs"][0])

    def test_heal_aliases_match_observation_hosts_and_paths(self) -> None:
        exporter = MetadataExporter()
        heal = exporter.export("heal_info", f"Brick private-node:/private-brick\n"
                               f"/private-file - Is in split-brain\n<gfid:{GFID}>\n"
                               "Status: Connected\nNumber of entries: 2\n" + PRIVATE)
        observed = exporter.export("observations", json.dumps({"observations": [
            {"host": "private-node", "backend": "/private-file"}]}))
        brick = heal["metadata"]["bricks"][0]
        self.assertEqual(brick["host"], observed["metadata"]["observations"][0]["host"])
        self.assertEqual(brick["entries"][0]["path"], observed["metadata"]["observations"][0]["backend"])
        self.assertTrue(brick["entries"][0]["split_brain"])
        self.assertEqual(GFID, brick["entries"][1]["gfid"])
        self.assertEqual(1, heal["omitted_fields_or_lines"])

    def test_topology_and_health_export_keep_only_parsed_operational_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private_host = "private-node"
            private_path = "/private-company/brick"
            private_ip = ".".join(("10", "0", "0", "1"))
            volume_info = root / "volume-info.txt"
            volume_info.write_text(
                "Volume Name: Private Company volume\nType: Replicate\nStatus: Started\n"
                "Volume ID: private-id\nBricks:\n"
                f"Brick1: {private_host}:{private_path}\n"
                f"Brick2: arbiter-node:{private_path} (arbiter)\n",
                encoding="utf-8",
            )
            volume_status = root / "volume-status.txt"
            volume_status.write_text(
                "Status of volume: Private Company volume\n"
                f"Brick {private_host}:{private_path} 49152 N/A Y 100\n"
                f"Brick arbiter-node:{private_path} 49153 N/A N N/A\n"
                "Self-heal Daemon on private-node N/A N/A Y 101\n"
                "unparsed Private Company log detail\n",
                encoding="utf-8",
            )
            health = root / "health.json"
            write_health_report(health, {
                "schema_version": 1, "volume": "Private Company volume", "available": True,
                "volume_type": "Replicate", "hosts": [private_host, "arbiter-node"],
                "host_facts": [{"host": private_host, "short_hostname": private_host,
                                "fqdn": "private-node.example", "ips": [private_ip],
                                "aliases": [private_host, "private-node.example"]}],
                "bricks": [{"host": private_host, "path": private_path, "online": True,
                            "pid_running": True, "aliases": [private_host]}],
                "self_heal_daemons": [{"host": private_host, "online": True,
                                       "pid_running": True, "source": "text"}],
                "checks": [{"kind": "brick_free_space", "ok": True, "host": private_host,
                            "path": private_path, "available_bytes": 42,
                            "message": PRIVATE, "stdout": PRIVATE}],
                "heal_settings": {"effective_settings": {"cluster.self-heal-daemon": "on"},
                                  "all_on": True, "all_off": False, "warnings": [PRIVATE]},
                "reversibility": {"snapshot_required": True, "snapshot_acknowledged": False,
                                  "snapshot_inventory": {"available": True, "count": 1,
                                                         "names": [PRIVATE], "raw": PRIVATE}},
                "summary": {"checks_checked": 1, "checks_ok": 1, "checks_failed": 0,
                            "hosts_checked": 2, "hosts_ok": 2, "hosts_failed": 0,
                            "bricks_checked": 2, "bricks_online": 1, "bricks_offline": 1,
                            "ready": False, "warnings": [PRIVATE]},
            })
            result = prepare_support_bundle(root / "bundle", {
                "volume_info": volume_info, "volume_status": volume_status, "health": health,
            }, private_identifiers=["organization:Private Company"])
            exported = {name: json.loads((root / "bundle" / f"{name}.json").read_text())
                        for name in ("volume_info", "volume_status", "health")}
            serialized = json.dumps(exported)
            for private in (PRIVATE, private_host, "arbiter-node", private_path, private_ip,
                            "private-id", "message", "stdout", "warnings", "names", "raw"):
                self.assertNotIn(private, serialized)
            topology = exported["volume_info"]["metadata"]
            status = exported["volume_status"]["metadata"]
            report = exported["health"]["metadata"]
            self.assertEqual("Replicate", topology["volume_type"])
            self.assertEqual("Started", topology["state"])
            self.assertEqual(topology["bricks"][0]["host"], status["bricks"][0]["host"])
            self.assertEqual(topology["bricks"][0]["path"], status["bricks"][0]["path"])
            self.assertEqual(topology["bricks"][0]["host"], report["bricks"][0]["host"])
            self.assertTrue(report["checks"][0]["ok"])
            self.assertEqual(42, report["checks"][0]["available_bytes"])
            self.assertTrue(all(item["status"] == "exported_metadata" for item in result["inventory"].values()))

    def test_refuses_nonregular_and_oversized_inputs_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fifo = root / "fifo"
            os.mkfifo(fifo)
            large = root / "large"
            with large.open("wb") as output:
                output.truncate(MAX_TEXT_BYTES + 1)
            for source, message in ((fifo, "not a regular"), (root, "regular"),
                                    (large, "size limit")):
                with self.subTest(source=source.name):
                    with self.assertRaisesRegex(ValueError, message):
                        prepare_support_bundle(root / "bundle", {"status": source},
                                               private_identifiers=["person:private"])
                    self.assertFalse((root / "bundle").exists())


if __name__ == "__main__":
    unittest.main()
