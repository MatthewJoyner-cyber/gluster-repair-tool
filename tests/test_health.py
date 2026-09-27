# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Tests for health-check helpers."""
from __future__ import annotations

import unittest
import json
import subprocess
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch

from gluster_heal_tool.cli import build_parser
from gluster_heal_tool.health import (
    _parse_self_heal_daemon_status,
    _self_heal_daemon_check,
    _volume_status_shd_xml,
    build_volume_health_report,
    render_volume_health_summary,
)
from gluster_heal_tool.health import health_check_warnings
from gluster_heal_tool.status import status_warnings


def _fake_host_identity_result(host: str, command: list[str]) -> dict[str, object] | None:
    if len(command) >= 3 and command[:2] == ["sh", "-lc"]:
        script = str(command[2] or "")
        if script.startswith("short=$(hostname -s"):
            short = host.split(".", 1)[0]
            fqdn = host if "." in host else f"{short}.example"
            ips = host if host and host[0].isdigit() else ""
            return {
                "ok": True,
                "returncode": 0,
                "stdout": f"short={short}\nfqdn={fqdn}\nips={ips}\n",
                "stderr": "",
            }
    return None


def _fake_heal_activity_result(
    command: list[str],
    *,
    load1: str | None = "0.10",
    cpu_count: str | None = "8",
    glusterfsd_cpu: str | None = None,
) -> dict[str, object] | None:
    if command == ["uptime"] and load1 is not None:
        return {
            "ok": True,
            "returncode": 0,
            "stdout": f" 10:00:00 up 1 day,  1 user,  load average: {load1}, 0.00, 0.00\n",
            "stderr": "",
        }
    if command == ["nproc"] and cpu_count is not None:
        return {
            "ok": True,
            "returncode": 0,
            "stdout": f"{cpu_count}\n",
            "stderr": "",
        }
    if command == ["ps", "-C", "glusterfsd", "-o", "%cpu=", "--no-headers"] and glusterfsd_cpu is not None:
        return {
            "ok": True,
            "returncode": 0,
            "stdout": f"{glusterfsd_cpu}\n",
            "stderr": "",
        }
    return None

class HealthTests(unittest.TestCase):
    def setUp(self) -> None:
        version = patch("gluster_heal_tool.health.get_gluster_version", return_value="11.1")
        version.start()
        self.addCleanup(version.stop)

    def test_structured_self_heal_status_rejects_malformed_xml_for_text_fallback(self) -> None:
        with patch(
            "gluster_heal_tool.health._run_local_with_sudo_fallback",
            return_value=subprocess.CompletedProcess([], 0, "<not-xml", ""),
        ):
            available, rows, error = _volume_status_shd_xml("gtest")

        self.assertFalse(available)
        self.assertEqual([], rows)
        self.assertIn("invalid structured SHD status", error)

    def test_wrapped_self_heal_daemon_row_is_parsed(self) -> None:
        rows = _parse_self_heal_daemon_status(
            "Self-heal Daemon on very-long-host.example\n"
            "N/A N/A N N/A\n"
        )

        self.assertEqual(1, len(rows))
        self.assertEqual("very-long-host.example", rows[0]["host"])
        self.assertFalse(rows[0]["online"])

    def test_required_self_heal_daemons_follow_physical_hosts_not_brick_count(self) -> None:
        bricks = [
            ("node-a", "/brick/a"),
            ("192.0.2.10", "/brick/b"),
            ("node-d", "/brick/c"),
        ]
        facts = [
            {"host": "node-a", "aliases": ["node-a", "192.0.2.10"]},
            {"host": "192.0.2.10", "aliases": ["node-a", "192.0.2.10"]},
            {"host": "node-d", "aliases": ["node-d"]},
        ]
        with patch("gluster_heal_tool.health.socket.gethostname", return_value="node-a"), patch(
            "gluster_heal_tool.health.socket.getfqdn", return_value="node-a.example"
        ):
            ready = _self_heal_daemon_check(
                [
                    {"host": "localhost", "online": True, "pid_running": True},
                    {"host": "node-d", "online": True, "pid_running": True},
                ],
                bricks=bricks,
                host_facts=facts,
                required=True,
            )
            missing = _self_heal_daemon_check(
                [{"host": "localhost", "online": True, "pid_running": True}],
                bricks=bricks,
                host_facts=facts,
                required=True,
            )

        self.assertTrue(ready["ok"])
        self.assertEqual(2, len(ready["required_hosts"]))
        self.assertFalse(missing["ok"])
        self.assertIn("node-d", missing["missing_hosts"])

    def test_required_self_heal_daemons_fail_closed_when_rows_are_missing(self) -> None:
        check = _self_heal_daemon_check(
            [],
            bricks=[("host-a", "/brick/a")],
            host_facts=[{"host": "host-a", "aliases": ["host-a"]}],
            required=True,
        )

        self.assertFalse(check["ok"])
        self.assertIn("host-a", check["missing_hosts"])

    def test_summarize_heal_settings_normalizes_gluster_daemon_values(self) -> None:
        from gluster_heal_tool.volume import summarize_heal_settings

        summary = summarize_heal_settings(
            "gtest4",
            {
                "cluster.self-heal-daemon": "enable",
                "cluster.data-self-heal": "on",
                "cluster.metadata-self-heal": "on",
                "cluster.entry-self-heal": "on",
            },
        )

        self.assertTrue(summary["all_on"])
        self.assertEqual("on", summary["effective_settings"]["cluster.self-heal-daemon"])
        self.assertIn("heal-related volume options are on", summary["warnings"])
        self.assertNotIn("heal-related volume options are in a mixed state", summary["warnings"])

    def test_build_volume_health_report_marks_unmounted_brick_as_blocker(self) -> None:
        volume_info = """Volume Name: gtest
Type: Replicate
Volume ID: 1234
Status: Started
Number of Bricks: 2
Transport-type: tcp
Bricks:
Brick1: host-a:/srv/gluster/brick-store/gtest
Brick2: host-b:/srv/gluster/brick-store/gtest
"""
        status_text = """Status of volume: gtest
Brick host-a:/srv/gluster/brick-store/gtest     49152     N/A        Y       1234
Brick host-b:/srv/gluster/brick-store/gtest     49153     N/A        Y       2345
"""

        def fake_run_remote(host, command, **kwargs):  # noqa: ANN001
            identity = _fake_host_identity_result(host, command)
            if identity is not None:
                return identity
            activity = _fake_heal_activity_result(command)
            if activity is not None:
                return activity
            if command == ["true"]:
                return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
            if command == ["systemctl", "is-active", "glusterd"]:
                return {"ok": True, "returncode": 0, "stdout": "active\n", "stderr": ""}
            if command[:2] == ["test", "-x"]:
                return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
            if command[:1] == ["tail"]:
                return {"ok": True, "returncode": 0, "stdout": "no recent self-heal lock skip lines for volume gtest\n", "stderr": ""}
            if command[:4] == ["findmnt", "-T", "/srv/gluster/brick-store/gtest", "-n"] and host == "host-b":
                return {"ok": False, "returncode": 1, "stdout": "", "stderr": "not mounted"}
            if command[:4] == ["findmnt", "-T", "/srv/gluster/brick-store/gtest", "-n"]:
                return {"ok": True, "returncode": 0, "stdout": "/srv/gluster/brick-store\n", "stderr": ""}
            if command[:1] == ["df"]:
                return {"ok": True, "returncode": 0, "stdout": "Filesystem 1B-blocks Used Available Use% Mounted on\n/dev/sda 100 10 90 10% /srv/gluster/brick-store/gtest\n", "stderr": ""}
            raise AssertionError(command)

        with patch("gluster_heal_tool.health.get_gluster_version", return_value="9.6"), patch(
            "gluster_heal_tool.health.get_volume_info", return_value=volume_info), patch(
            "gluster_heal_tool.health.get_heal_settings",
            return_value={
                "cluster.self-heal-daemon": "on",
                "cluster.data-self-heal": "on",
                "cluster.metadata-self-heal": "on",
                "cluster.entry-self-heal": "on",
            },
        ), patch("gluster_heal_tool.health._volume_status_text", return_value=status_text), patch(
            "gluster_heal_tool.health._snapshot_inventory_text",
            return_value=(False, "", "snapshot inventory unavailable"),
        ), patch(
            "gluster_heal_tool.health._run_remote_check",
            side_effect=fake_run_remote,
        ):
            report = build_volume_health_report("gtest", ssh_user="repair", connect_timeout=5.0)

        self.assertTrue(report["available"])
        self.assertEqual("gtest", report["volume"])
        self.assertEqual("Replicate", report["volume_type"])
        self.assertEqual(2, report["summary"]["hosts_checked"])
        self.assertEqual(2, report["summary"]["bricks_checked"])
        self.assertFalse(report["summary"]["ready"])
        self.assertIn("brick path has no mounted ancestor on host-b", " | ".join(report["summary"]["blockers"]))
        self.assertIn("Health check: blocked", render_volume_health_summary(report))
        self.assertIn("Gluster 9.6", render_volume_health_summary(report))
        self.assertIn("no compatibility claim", " | ".join(report["summary"]["warnings"]))

    def test_build_volume_health_report_handles_wrapped_status_lines(self) -> None:
        volume_info = """Volume Name: gtest3a
Type: Replicate
Volume ID: 1234
Status: Started
Number of Bricks: 3
Transport-type: tcp
Bricks:
Brick1: node-a:/gluster/gtest3a/gtest3a/brick
Brick2: node-b:/gluster/gtest3a/gtest3a/brick
Brick3: node-c:/gluster/gtest3a/arbiter/brick
"""
        status_text = """Status of volume: gtest3a
Brick node-a:/gluster/gtest3a/gtest3a/brick 60630     0          Y       1054426
Brick node-b:/gluster/gtest3a/gtest3a/b
rick                                        60844     0          Y       215280
Brick node-c:/gluster/gtest3a/arbiter/b
rick                                          53038     0          Y       1047190
"""

        def fake_run_remote(host, command, **kwargs):  # noqa: ANN001
            identity = _fake_host_identity_result(host, command)
            if identity is not None:
                return identity
            activity = _fake_heal_activity_result(command)
            if activity is not None:
                return activity
            if command == ["true"]:
                return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
            if command == ["systemctl", "is-active", "glusterd"]:
                return {"ok": True, "returncode": 0, "stdout": "active\n", "stderr": ""}
            if command[:2] == ["test", "-x"]:
                return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
            if command[:1] == ["tail"]:
                return {"ok": True, "returncode": 0, "stdout": "no recent self-heal lock skip lines for volume gtest3a\n", "stderr": ""}
            if command[:4] == ["findmnt", "-T", "/gluster/gtest3a/gtest3a/brick", "-n"]:
                return {"ok": True, "returncode": 0, "stdout": "/gluster/gtest3a\n", "stderr": ""}
            if command[:4] == ["findmnt", "-T", "/gluster/gtest3a/arbiter/brick", "-n"]:
                return {"ok": True, "returncode": 0, "stdout": "/gluster/gtest3a\n", "stderr": ""}
            if command[:1] == ["df"]:
                return {"ok": True, "returncode": 0, "stdout": "Filesystem 1B-blocks Used Available Use% Mounted on\n/dev/sda 100 10 90 10% /gluster/gtest3a/gtest3a/brick\n", "stderr": ""}
            raise AssertionError(command)

        with patch("gluster_heal_tool.health.get_gluster_version", return_value="9.6"), patch(
            "gluster_heal_tool.health.get_volume_info", return_value=volume_info), patch(
            "gluster_heal_tool.health.get_heal_settings",
            return_value={
                "cluster.self-heal-daemon": "on",
                "cluster.data-self-heal": "on",
                "cluster.metadata-self-heal": "on",
                "cluster.entry-self-heal": "on",
            },
        ), patch("gluster_heal_tool.health._volume_status_text", return_value=status_text), patch(
            "gluster_heal_tool.health._snapshot_inventory_text",
            return_value=(False, "", "snapshot inventory unavailable"),
        ), patch(
            "gluster_heal_tool.health._run_remote_check",
            side_effect=fake_run_remote,
        ):
            report = build_volume_health_report("gtest3a", ssh_user="repair", connect_timeout=5.0)

        self.assertTrue(report["summary"]["ready"])
        self.assertEqual(3, report["summary"]["bricks_online"])
        self.assertEqual([], report["summary"]["blockers"])
        self.assertEqual("9.6", report["gluster_version"])
        self.assertIn("no compatibility claim", " | ".join(report["summary"]["warnings"]))
        self.assertIn("Health check: ready", render_volume_health_summary(report))

    def test_build_volume_health_report_warns_when_self_heal_daemon_is_off(self) -> None:
        volume_info = """Volume Name: gtest
Type: Replicate
Volume ID: 1234
Status: Started
Number of Bricks: 2
Transport-type: tcp
Bricks:
Brick1: host-a:/srv/gluster/brick-store/gtest
Brick2: host-b:/srv/gluster/brick-store/gtest
"""
        status_text = """Status of volume: gtest
Brick host-a:/srv/gluster/brick-store/gtest     49152     N/A        Y       1234
Brick host-b:/srv/gluster/brick-store/gtest     49153     N/A        Y       2345
"""

        def fake_run_remote(host, command, **kwargs):  # noqa: ANN001
            identity = _fake_host_identity_result(host, command)
            if identity is not None:
                return identity
            activity = _fake_heal_activity_result(command)
            if activity is not None:
                return activity
            if command == ["true"]:
                return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
            if command == ["systemctl", "is-active", "glusterd"]:
                return {"ok": True, "returncode": 0, "stdout": "active\n", "stderr": ""}
            if command[:2] == ["test", "-x"]:
                return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
            if command[:1] == ["tail"]:
                return {"ok": True, "returncode": 0, "stdout": "no recent self-heal lock skip lines for volume gtest\n", "stderr": ""}
            if command[:4] == ["findmnt", "-T", "/srv/gluster/brick-store/gtest", "-n"]:
                return {"ok": True, "returncode": 0, "stdout": "/srv/gluster/brick-store\n", "stderr": ""}
            if command[:1] == ["df"]:
                return {"ok": True, "returncode": 0, "stdout": "Filesystem 1B-blocks Used Available Use% Mounted on\n/dev/sda 100 10 90 10% /srv/gluster/brick-store/gtest\n", "stderr": ""}
            raise AssertionError(command)

        with patch("gluster_heal_tool.health.get_volume_info", return_value=volume_info), patch(
            "gluster_heal_tool.health.get_heal_settings",
            return_value={
                "cluster.self-heal-daemon": "off",
                "cluster.data-self-heal": "on",
                "cluster.metadata-self-heal": "on",
                "cluster.entry-self-heal": "on",
            },
        ), patch("gluster_heal_tool.health._volume_status_text", return_value=status_text), patch(
            "gluster_heal_tool.health._snapshot_inventory_text",
            return_value=(False, "", "snapshot inventory unavailable"),
        ), patch(
            "gluster_heal_tool.health._run_remote_check",
            side_effect=fake_run_remote,
        ):
            report = build_volume_health_report("gtest", ssh_user="repair", connect_timeout=5.0)

        warning = "cluster.self-heal-daemon is off; split-brain and below-quorum states may stay hidden until heal is enabled"
        self.assertIn(warning, report["heal_settings"]["warnings"])
        self.assertIn(warning, report["summary"]["warnings"])

    def test_build_volume_health_report_blocks_offline_self_heal_daemon(self) -> None:
        volume_info = """Volume Name: gtest4
Type: Replicate
Volume ID: 1234
Status: Started
Number of Bricks: 1 x 4 = 4
Transport-type: tcp
Bricks:
Brick1: host-a:/gluster/gtest4/brick
Brick2: host-b:/gluster/gtest4/brick
Brick3: host-c:/gluster/gtest4/brick
Brick4: host-d:/gluster/gtest4/brick
"""
        status_text = """Status of volume: gtest4
Brick host-a:/gluster/gtest4/brick 49152 0 Y 1234
Brick host-b:/gluster/gtest4/brick 49153 0 Y 2345
Brick host-c:/gluster/gtest4/brick 49154 0 Y 3456
Brick host-d:/gluster/gtest4/brick 49155 0 Y 4567
Self-heal Daemon on host-a N/A N/A Y 5678
Self-heal Daemon on host-b N/A N/A Y 6789
Self-heal Daemon on host-c N/A N/A N N/A
Self-heal Daemon on host-d N/A N/A Y 7890
"""

        def fake_run_remote(host, command, **kwargs):  # noqa: ANN001
            identity = _fake_host_identity_result(host, command)
            if identity is not None:
                return identity
            activity = _fake_heal_activity_result(command)
            if activity is not None:
                return activity
            if command == ["true"]:
                return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
            if command == ["systemctl", "is-active", "glusterd"]:
                return {"ok": True, "returncode": 0, "stdout": "active\n", "stderr": ""}
            if command[:2] == ["test", "-x"]:
                return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
            if command[:1] == ["tail"]:
                return {"ok": True, "returncode": 0, "stdout": "no recent self-heal lock skip lines for volume gtest4\n", "stderr": ""}
            if command[:4] == ["findmnt", "-T", "/gluster/gtest4/brick", "-n"]:
                return {"ok": True, "returncode": 0, "stdout": "/gluster/gtest4\n", "stderr": ""}
            if command[:1] == ["df"]:
                return {"ok": True, "returncode": 0, "stdout": "Filesystem 1B-blocks Used Available Use% Mounted on\n/dev/sda 100 10 90 10% /gluster/gtest4/brick\n", "stderr": ""}
            raise AssertionError(command)

        with patch("gluster_heal_tool.health.get_volume_info", return_value=volume_info), patch(
            "gluster_heal_tool.health.get_heal_settings",
            return_value={
                "cluster.self-heal-daemon": "on",
                "cluster.data-self-heal": "on",
                "cluster.metadata-self-heal": "on",
                "cluster.entry-self-heal": "on",
            },
        ), patch("gluster_heal_tool.health._volume_status_text", return_value=status_text), patch(
            "gluster_heal_tool.health._snapshot_inventory_text",
            return_value=(False, "", "snapshot inventory unavailable"),
        ), patch(
            "gluster_heal_tool.health._run_remote_check",
            side_effect=fake_run_remote,
        ):
            report = build_volume_health_report("gtest4", ssh_user="repair", connect_timeout=5.0)

        self.assertFalse(report["summary"]["ready"])
        self.assertEqual("host-c", report["self_heal_daemons"][2]["host"])
        self.assertIn(
            "self-heal daemon reported offline in volume status: host-c",
            report["summary"]["blockers"],
        )

    def test_build_volume_health_report_records_snapshot_inventory(self) -> None:
        volume_info = """Volume Name: gtest
Type: Replicate
Volume ID: 1234
Status: Started
Number of Bricks: 2
Transport-type: tcp
Bricks:
Brick1: host-a:/srv/gluster/brick-store/gtest
Brick2: host-b:/srv/gluster/brick-store/gtest
"""
        status_text = """Status of volume: gtest
Brick host-a:/srv/gluster/brick-store/gtest     49152     N/A        Y       1234
Brick host-b:/srv/gluster/brick-store/gtest     49153     N/A        Y       2345
"""

        def fake_run_remote(host, command, **kwargs):  # noqa: ANN001
            identity = _fake_host_identity_result(host, command)
            if identity is not None:
                return identity
            activity = _fake_heal_activity_result(command)
            if activity is not None:
                return activity
            if command == ["true"]:
                return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
            if command == ["systemctl", "is-active", "glusterd"]:
                return {"ok": True, "returncode": 0, "stdout": "active\n", "stderr": ""}
            if command[:2] == ["test", "-x"]:
                return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
            if command[:1] == ["tail"]:
                return {"ok": True, "returncode": 0, "stdout": "no recent self-heal lock skip lines for volume gtest\n", "stderr": ""}
            if command[:4] == ["findmnt", "-T", "/srv/gluster/brick-store/gtest", "-n"]:
                return {"ok": True, "returncode": 0, "stdout": "/srv/gluster/brick-store\n", "stderr": ""}
            if command[:1] == ["df"]:
                return {"ok": True, "returncode": 0, "stdout": "Filesystem 1B-blocks Used Available Use% Mounted on\n/dev/sda 100 10 90 10% /srv/gluster/brick-store/gtest\n", "stderr": ""}
            raise AssertionError(command)

        with patch("gluster_heal_tool.health.get_volume_info", return_value=volume_info), patch(
            "gluster_heal_tool.health.get_heal_settings",
            return_value={
                "cluster.self-heal-daemon": "on",
                "cluster.data-self-heal": "on",
                "cluster.metadata-self-heal": "on",
                "cluster.entry-self-heal": "on",
            },
        ), patch("gluster_heal_tool.health._volume_status_text", return_value=status_text), patch(
            "gluster_heal_tool.health._snapshot_inventory_text",
            return_value=(True, "Snapshot Name: snap-one\nSnapshot Name: snap-two\n", ""),
        ), patch("gluster_heal_tool.health._run_remote_check", side_effect=fake_run_remote):
            report = build_volume_health_report("gtest", ssh_user="repair", connect_timeout=5.0, require_snapshot=True, snapshot_ack=True)

        self.assertTrue(report["reversibility"]["snapshot_required"])
        self.assertTrue(report["reversibility"]["snapshot_acknowledged"])
        self.assertTrue(report["reversibility"]["snapshot_inventory"]["available"])
        self.assertEqual(2, report["reversibility"]["snapshot_inventory"]["count"])
        self.assertIn("snapshots 2", render_volume_health_summary(report))

    def test_build_volume_health_report_warns_on_repeated_glusterd_lock_skips(self) -> None:
        volume_info = """Volume Name: gtest
Type: Replicate
Volume ID: 1234
Status: Started
Number of Bricks: 2
Transport-type: tcp
Bricks:
Brick1: host-a:/srv/gluster/brick-store/gtest
Brick2: host-b:/srv/gluster/brick-store/gtest
"""
        status_text = """Status of volume: gtest
Brick host-a:/srv/gluster/brick-store/gtest     49152     N/A        Y       1234
Brick host-b:/srv/gluster/brick-store/gtest     49153     N/A        Y       2345
"""

        def fake_run_remote(host, command, **kwargs):  # noqa: ANN001
            identity = _fake_host_identity_result(host, command)
            if identity is not None:
                return identity
            activity = _fake_heal_activity_result(command)
            if activity is not None:
                return activity
            if command == ["true"]:
                return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
            if command == ["systemctl", "is-active", "glusterd"]:
                return {"ok": True, "returncode": 0, "stdout": "active\n", "stderr": ""}
            if command[:2] == ["test", "-x"]:
                return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
            if command[:1] == ["tail"]:
                return {
                    "ok": True,
                    "returncode": 0,
                    "stdout": (
                        "1:[2026-05-16 12:42:00.006574 +0000] D [MSGID: 0] [afr-self-heal-entry.c:1289:afr_selfheal_entry] "
                        "0-gtest-replicate-0: 9d6de765-e14a-41f5-a2dd-f44bc68378b9: Skipping entry self-heal as only 3 sub-volumes could be locked in gtest-replicate-0:self-heal domain\n"
                        "2:[2026-05-16 12:42:12.009645 +0000] D [MSGID: 0] [afr-self-heal-entry.c:1289:afr_selfheal_entry] "
                        "0-gtest-replicate-0: 80f28d9b-e3fe-4b89-9634-0a0eacbbca23: Skipping entry self-heal as only 3 sub-volumes could be locked in gtest-replicate-0:self-heal domain\n"
                    ),
                    "stderr": "",
                }
            if command[:4] == ["findmnt", "-T", "/srv/gluster/brick-store/gtest", "-n"]:
                return {"ok": True, "returncode": 0, "stdout": "/srv/gluster/brick-store\n", "stderr": ""}
            if command[:1] == ["df"]:
                return {"ok": True, "returncode": 0, "stdout": "Filesystem 1B-blocks Used Available Use% Mounted on\n/dev/sda 100 10 90 10% /srv/gluster/brick-store/gtest\n", "stderr": ""}
            raise AssertionError(command)

        with patch("gluster_heal_tool.health.get_volume_info", return_value=volume_info), patch(
            "gluster_heal_tool.health.get_heal_settings",
            return_value={
                "cluster.self-heal-daemon": "on",
                "cluster.data-self-heal": "on",
                "cluster.metadata-self-heal": "on",
                "cluster.entry-self-heal": "on",
            },
        ), patch("gluster_heal_tool.health._volume_status_text", return_value=status_text), patch(
            "gluster_heal_tool.health._snapshot_inventory_text",
            return_value=(False, "", "snapshot inventory unavailable"),
        ), patch(
            "gluster_heal_tool.health._run_remote_check",
            side_effect=fake_run_remote,
        ), patch(
            "gluster_heal_tool.health._utc_now",
            return_value=datetime(2026, 5, 16, 12, 42, 30, tzinfo=timezone.utc),
        ):
            report = build_volume_health_report("gtest", ssh_user="repair", connect_timeout=5.0)

        warnings = " | ".join(report["summary"]["warnings"])
        self.assertIn("within the last 60 seconds", warnings)
        self.assertIn("restart glusterd", warnings)
        self.assertIn("host-a", warnings)

    def test_build_volume_health_report_flags_busy_glusterfsd_when_load_is_high(self) -> None:
        volume_info = """Volume Name: gtest\nType: Replicate\nVolume ID: 1234\nStatus: Started\nNumber of Bricks: 2\nTransport-type: tcp\nBricks:\nBrick1: host-a:/srv/gluster/brick-store/gtest\nBrick2: host-b:/srv/gluster/brick-store/gtest\n"""
        status_text = """Status of volume: gtest\nBrick host-a:/srv/gluster/brick-store/gtest     49152     N/A        Y       1234\nBrick host-b:/srv/gluster/brick-store/gtest     49153     N/A        Y       2345\n"""

        def fake_run_remote(host, command, **kwargs):  # noqa: ANN001
            identity = _fake_host_identity_result(host, command)
            if identity is not None:
                return identity
            if host == "host-a":
                activity = _fake_heal_activity_result(command, load1="6.80", cpu_count="8", glusterfsd_cpu="62.50")
            else:
                activity = _fake_heal_activity_result(command, load1="0.20", cpu_count="8")
            if activity is not None:
                return activity
            if command == ["true"]:
                return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
            if command == ["systemctl", "is-active", "glusterd"]:
                return {"ok": True, "returncode": 0, "stdout": "active\n", "stderr": ""}
            if command[:2] == ["test", "-x"]:
                return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
            if command[:1] == ["tail"]:
                return {"ok": True, "returncode": 0, "stdout": "no recent self-heal lock skip lines for volume gtest\n", "stderr": ""}
            if command[:4] == ["findmnt", "-T", "/srv/gluster/brick-store/gtest", "-n"]:
                return {"ok": True, "returncode": 0, "stdout": "/srv/gluster/brick-store\n", "stderr": ""}
            if command[:1] == ["df"]:
                return {"ok": True, "returncode": 0, "stdout": "Filesystem 1B-blocks Used Available Use% Mounted on\n/dev/sda 100 10 90 10% /srv/gluster/brick-store/gtest\n", "stderr": ""}
            raise AssertionError(command)

        with patch("gluster_heal_tool.health.get_volume_info", return_value=volume_info), patch(
            "gluster_heal_tool.health.get_heal_settings",
            return_value={
                "cluster.self-heal-daemon": "on",
                "cluster.data-self-heal": "on",
                "cluster.metadata-self-heal": "on",
                "cluster.entry-self-heal": "on",
            },
        ), patch("gluster_heal_tool.health._volume_status_text", return_value=status_text), patch(
            "gluster_heal_tool.health._snapshot_inventory_text",
            return_value=(False, "", "snapshot inventory unavailable"),
        ), patch(
            "gluster_heal_tool.health._run_remote_check",
            side_effect=fake_run_remote,
        ):
            report = build_volume_health_report("gtest", ssh_user="repair", connect_timeout=5.0)

        warnings = " | ".join(report["summary"]["warnings"])
        blockers = " | ".join(report["summary"]["blockers"])
        self.assertFalse(report["summary"]["ready"])
        self.assertIn("gluster heal in progress", warnings)
        self.assertIn("gluster heal in progress", blockers)
        self.assertIn("glusterfsd cpu", warnings)

    def test_build_volume_health_report_skips_glusterfsd_probe_when_load_is_low(self) -> None:
        volume_info = """Volume Name: gtest\nType: Replicate\nVolume ID: 1234\nStatus: Started\nNumber of Bricks: 2\nTransport-type: tcp\nBricks:\nBrick1: host-a:/srv/gluster/brick-store/gtest\nBrick2: host-b:/srv/gluster/brick-store/gtest\n"""
        status_text = """Status of volume: gtest\nBrick host-a:/srv/gluster/brick-store/gtest     49152     N/A        Y       1234\nBrick host-b:/srv/gluster/brick-store/gtest     49153     N/A        Y       2345\n"""

        def fake_run_remote(host, command, **kwargs):  # noqa: ANN001
            identity = _fake_host_identity_result(host, command)
            if identity is not None:
                return identity
            activity = _fake_heal_activity_result(command, load1="0.20", cpu_count="8")
            if activity is not None:
                return activity
            if command == ["true"]:
                return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
            if command == ["systemctl", "is-active", "glusterd"]:
                return {"ok": True, "returncode": 0, "stdout": "active\n", "stderr": ""}
            if command[:2] == ["test", "-x"]:
                return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
            if command[:1] == ["tail"]:
                return {"ok": True, "returncode": 0, "stdout": "no recent self-heal lock skip lines for volume gtest\n", "stderr": ""}
            if command[:4] == ["findmnt", "-T", "/srv/gluster/brick-store/gtest", "-n"]:
                return {"ok": True, "returncode": 0, "stdout": "/srv/gluster/brick-store\n", "stderr": ""}
            if command[:1] == ["df"]:
                return {"ok": True, "returncode": 0, "stdout": "Filesystem 1B-blocks Used Available Use% Mounted on\n/dev/sda 100 10 90 10% /srv/gluster/brick-store/gtest\n", "stderr": ""}
            if command == ["ps", "-C", "glusterfsd", "-o", "%cpu=", "--no-headers"]:
                raise AssertionError("glusterfsd probe should be skipped when load is low")
            raise AssertionError(command)

        with patch("gluster_heal_tool.health.get_volume_info", return_value=volume_info), patch(
            "gluster_heal_tool.health.get_heal_settings",
            return_value={
                "cluster.self-heal-daemon": "on",
                "cluster.data-self-heal": "on",
                "cluster.metadata-self-heal": "on",
                "cluster.entry-self-heal": "on",
            },
        ), patch("gluster_heal_tool.health._volume_status_text", return_value=status_text), patch(
            "gluster_heal_tool.health._snapshot_inventory_text",
            return_value=(False, "", "snapshot inventory unavailable"),
        ), patch(
            "gluster_heal_tool.health._run_remote_check",
            side_effect=fake_run_remote,
        ):
            report = build_volume_health_report("gtest", ssh_user="repair", connect_timeout=5.0)

        self.assertTrue(report["summary"]["ready"])
        heal_activity = next(check for check in report["checks"] if check.get("kind") == "heal_activity")
        self.assertFalse(heal_activity.get("inspected_glusterfsd"))
        self.assertEqual([], report["summary"]["blockers"])
    def test_build_volume_health_report_ignores_stale_glusterd_lock_skips(self) -> None:
        volume_info = """Volume Name: gtest
Type: Replicate
Volume ID: 1234
Status: Started
Number of Bricks: 2
Transport-type: tcp
Bricks:
Brick1: host-a:/srv/gluster/brick-store/gtest
Brick2: host-b:/srv/gluster/brick-store/gtest
"""
        status_text = """Status of volume: gtest
Brick host-a:/srv/gluster/brick-store/gtest     49152     N/A        Y       1234
Brick host-b:/srv/gluster/brick-store/gtest     49153     N/A        Y       2345
"""

        def fake_run_remote(host, command, **kwargs):  # noqa: ANN001
            identity = _fake_host_identity_result(host, command)
            if identity is not None:
                return identity
            activity = _fake_heal_activity_result(command)
            if activity is not None:
                return activity
            if command == ["true"]:
                return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
            if command == ["systemctl", "is-active", "glusterd"]:
                return {"ok": True, "returncode": 0, "stdout": "active\n", "stderr": ""}
            if command[:2] == ["test", "-x"]:
                return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
            if command[:1] == ["tail"]:
                return {
                    "ok": True,
                    "returncode": 0,
                    "stdout": (
                        "1:[2026-05-16 12:41:00.006574 +0000] D [MSGID: 0] [afr-self-heal-entry.c:1289:afr_selfheal_entry] "
                        "0-gtest-replicate-0: 9d6de765-e14a-41f5-a2dd-f44bc68378b9: Skipping entry self-heal as only 3 sub-volumes could be locked in gtest-replicate-0:self-heal domain\n"
                        "2:[2026-05-16 12:41:00.009645 +0000] D [MSGID: 0] [afr-self-heal-entry.c:1289:afr_selfheal_entry] "
                        "0-gtest-replicate-0: 80f28d9b-e3fe-4b89-9634-0a0eacbbca23: Skipping entry self-heal as only 3 sub-volumes could be locked in gtest-replicate-0:self-heal domain\n"
                    ),
                    "stderr": "",
                }
            if command[:4] == ["findmnt", "-T", "/srv/gluster/brick-store/gtest", "-n"]:
                return {"ok": True, "returncode": 0, "stdout": "/srv/gluster/brick-store\n", "stderr": ""}
            if command[:1] == ["df"]:
                return {"ok": True, "returncode": 0, "stdout": "Filesystem 1B-blocks Used Available Use% Mounted on\n/dev/sda 100 10 90 10% /srv/gluster/brick-store/gtest\n", "stderr": ""}
            raise AssertionError(command)

        with patch("gluster_heal_tool.health.get_volume_info", return_value=volume_info), patch(
            "gluster_heal_tool.health.get_heal_settings",
            return_value={
                "cluster.self-heal-daemon": "on",
                "cluster.data-self-heal": "on",
                "cluster.metadata-self-heal": "on",
                "cluster.entry-self-heal": "on",
            },
        ), patch("gluster_heal_tool.health._volume_status_text", return_value=status_text), patch(
            "gluster_heal_tool.health._snapshot_inventory_text",
            return_value=(False, "", "snapshot inventory unavailable"),
        ), patch(
            "gluster_heal_tool.health._run_remote_check",
            side_effect=fake_run_remote,
        ), patch(
            "gluster_heal_tool.health._utc_now",
            return_value=datetime(2026, 5, 16, 12, 42, 30, tzinfo=timezone.utc),
        ):
            report = build_volume_health_report("gtest", ssh_user="repair", connect_timeout=5.0)

        warnings = " | ".join(report["summary"]["warnings"])
        self.assertNotIn("restart glusterd", warnings)
        self.assertNotIn("self-heal lock skip", warnings)

    def test_health_check_warnings_reject_stale_or_missing_report(self) -> None:
        missing = health_check_warnings({})
        self.assertTrue(any("run health-check first" in message for message in missing))

        stale = {
            "volume": "gtest",
            "brick_path": "/srv/gluster/brick-store/gtest",
            "hosts": ["host-a"],
            "health_check": {
                "volume": "gtest",
                "brick_path": "/srv/gluster/brick-store/gtest",
                "hosts": ["host-a"],
                "checked_at": "2000-01-01T00:00:00+00:00",
                "summary": {"ready": True},
            },
        }
        stale_warnings = health_check_warnings(stale, max_age_seconds=60)
        self.assertTrue(any("stale" in message for message in stale_warnings))

    def test_status_warnings_surface_health_report_advisories(self) -> None:
        status = {
            "health_check": {
                "summary": {
                    "warnings": [
                        "recent glustershd log on node-a shows repeated self-heal lock skips; restart glusterd or the affected brick on node-a, then rerun health-check"
                    ]
                }
            }
        }

        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "status.json"
            path.write_text(json.dumps(status), encoding="utf-8")
            warnings = status_warnings(path)

        self.assertTrue(any("restart glusterd" in message for message in warnings))

    def test_health_check_parser_exposes_command_and_skip_flag(self) -> None:
        args = build_parser().parse_args(
            [
                "health-check",
                "--volume",
                "gtest",
            ]
        )
        self.assertEqual("health-check", args.cmd)
        self.assertEqual("gtest", args.volume)
        self.assertIsNone(args.health_out)

        run_args = build_parser().parse_args(
            [
                "apply-run",
                "--apply-in",
                "/tmp/apply.json",
                "--skip-health-check",
            ]
        )
        self.assertTrue(run_args.skip_health_check)


if __name__ == "__main__":
    unittest.main()
