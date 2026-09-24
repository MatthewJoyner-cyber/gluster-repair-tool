# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Run generated repair/helper argv with only low-level operations stubbed."""
import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gluster_heal_tool.apply import build_apply_results
from gluster_heal_tool import apply_planning_utils as commands
from gluster_heal_tool.executor import _execute_step
from gluster_heal_tool.remote_ops import rsync_brick_pull_command


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "gluster-host-ops.sh"
GFID = "12345678-1234-1234-1234-123456789abc"


class HostOpsContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.trace = self.root / "trace.jsonl"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("GLUSTER_", "XDG_", "PYTHON"))}
        self.env.update(HOME=str(self.root), PATH=f"{self.bin}:{os.environ['PATH']}",
                        CONTRACT_TRACE=str(self.trace), CONTRACT_HELPER=str(HELPER))
        for name in ("mkdir", "cp", "mv", "rm", "chmod", "chown", "stat", "setfattr",
                     "getfattr", "getfacl", "setfacl", "ln", "rsync"):
            self.stub(name, """data = sys.stdin.read() if name == 'setfacl' and '--set-file=-' in args else ''
with open(os.environ['CONTRACT_TRACE'], 'a') as stream:
    stream.write(json.dumps([name, args, data]) + '\\n')
if name == os.environ.get('CONTRACT_FAIL'):
    print('injected command failure', file=sys.stderr)
    raise SystemExit(17)
if name == 'getfattr': print('trusted.gfid=0x12345678123412341234123456789abc')
if name == 'getfacl': print('user::rwx\\ngroup::r-x\\nother::---')
""")
        # No SSH or sudo process can escape this transport boundary.
        self.stub("ssh", """import shlex
remote = shlex.split(args[-1])
if remote[:3] != ['sudo', '-n', '/opt/gluster-repair/gluster-host-ops.sh']:
    raise SystemExit('unexpected remote command')
os.execv('/bin/bash', ['bash', os.environ['CONTRACT_HELPER'], *remote[3:]])
""")

    def stub(self, name, body):
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\nimport json,os,sys\nname={name!r}; args=sys.argv[1:]\n" + body)
        path.chmod(0o755)

    def calls(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []

    def run_command(self, command):
        # Legacy login-shell previews must not reset PATH and bypass the stubs.
        if command[:2] == ["bash", "-lc"]:
            command = ["bash", "-c", command[-1]]
        return subprocess.run(command, cwd=self.root, env=self.env, text=True, capture_output=True)

    def test_generated_file_and_directory_commands_preserve_operands(self):
        source = str(self.root / "source ' ; $(literal)")
        target = str(self.root / "target with spaces")
        cases = [
            (commands._ssh_mkdir_preview("host-a", target), ["mkdir", ["-p", "--", target]]),
            (commands._ssh_cp_preview("host-a", source, target), ["cp", ["-a", "--", source, target]]),
            (commands._ssh_cp_preview("host-a", target, source), ["cp", ["-a", "--", target, source]]),
            (commands._ssh_mv_preview("host-a", source, target), ["mv", ["--", source, target]]),
            (commands._ssh_mv_preview("host-a", target, source), ["mv", ["--", target, source]]),
            (commands._ssh_rm_preview("host-a", target), ["rm", ["-f", "--", target]]),
            (commands._ssh_rmr_preview("host-a", target), ["rm", ["-rf", "--", target]]),
            (commands._ssh_chmod_numeric_preview("host-a", 0o2750, target), ["chmod", ["2750", "--", target]]),
            (commands._ssh_chown_numeric_preview("host-a", 1234, 2345, target), ["chown", ["1234:2345", "--", target]]),
            (commands._ssh_stat_preview("host-a", target), ["stat", ["-c", "%F", "--", target]]),
            (commands._ssh_getfattr_preview("host-a", target), ["getfattr", ["-n", "trusted.gfid", "-e", "hex", "--absolute-names", "--", target]]),
        ]
        for command, expected in cases:
            with self.subTest(operation=expected):
                self.trace.unlink(missing_ok=True)
                result = self.run_command(command)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual([expected + [""]], self.calls())

    def test_generated_mdata_preview_and_executor_reach_setfattr(self):
        result = build_apply_results({"actions": [{
            "action_id": "mdata-fixture", "action_type": "repair_directory_metadata",
            "logical_path": "fixture/directory", "repair_strategy": "attach_directory_mdata",
            "directory_canonical_mdata_hex": "0x01020304",
            "directory_canonical_backend": "/brick/fixture/directory",
            "directory_mdata_mismatch_hosts": ["host-b"],
        }]}, execution_mode="dry-run", backup_mode="required", batch=True)[0]
        step = next(step for step in result.steps if step.step_type == "attach_directory_mdata")
        preview = self.run_command(step.command_preview)
        self.assertEqual(0, preview.returncode, preview.stderr)
        with patch("gluster_heal_tool.executor._run_command", side_effect=self.run_command):
            status, code, message = _execute_step(step)
        self.assertEqual(("ok", 0), (status, code), message)
        self.assertEqual(self.calls()[0], self.calls()[1])
        self.assertEqual(["-n", "trusted.glusterfs.mdata", "-v", "0x01020304", "--", step.target_path], self.calls()[0][1])

    def test_generated_gfid_and_afr_values(self):
        target = str(self.root / "target")
        result = self.run_command(commands._ssh_setfattr_preview("host-a", target, GFID))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("0s" + base64.b64encode(bytes.fromhex(GFID.replace("-", ""))).decode(), self.calls()[0][1][3])
        result = self.run_command(["bash", str(HELPER), "setfattr", "-n", "trusted.afr.example-client-0",
                                   "-v", "0x000000000000000100000000", "--", target])
        self.assertEqual(0, result.returncode, result.stderr)

    def test_malformed_xattrs_never_reach_low_level_command(self):
        cases = [(name, value) for name in ("trusted.glusterfs.mdata", "trusted.afr.example-client-0")
                 for value in ("0x", "0x1", "0xZZ", "0x00\n", "0x00 11", "0sAAAA")]
        cases += [("trusted.gfid", value) for value in ("0s", "0snot-base64", "0sAAAA", "0x" + "00" * 16)]
        cases += [("trusted.gfid", "0s" + "A" * 21 + "B=="),
                  ("trusted.glusterfs.mdata.extra", "0x00"), ("user.fixture", "0x00"), ("trusted.afr.", "0x00")]
        for name, value in cases:
            with self.subTest(name=name, value=value):
                self.trace.unlink(missing_ok=True)
                result = self.run_command(["bash", str(HELPER), "setfattr", "-n", name, "-v", value, "--", str(self.root / "target")])
                self.assertNotEqual(0, result.returncode)
                self.assertEqual([], self.calls())

    def test_helper_failure_reaches_executor(self):
        from gluster_heal_tool.models import ApplyStep
        self.env["CONTRACT_FAIL"] = "setfattr"
        step = ApplyStep(step_id="fixture", step_type="attach_directory_mdata", host="host-a",
                         source_path="0x0102", target_path="/brick/fixture",
                         command_preview=commands._ssh_set_mdata_preview("host-a", "/brick/fixture", "0x0102"))
        with patch("gluster_heal_tool.executor._run_command", side_effect=self.run_command):
            status, code, message = _execute_step(step)
        self.assertEqual(("failed", 17), (status, code))
        self.assertIn("injected command failure", message)

    def test_generated_acl_commands_and_source_failure(self):
        command = commands._ssh_acl_reference_preview("host-a", "/brick/source", "host-b", "/brick/target", clear_default=True)
        result = self.run_command(command)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(["setfacl", ["-k", "--", "/brick/target"], ""], self.calls())
        self.assertIn(["setfacl", ["--set-file=-", "--", "/brick/target"], "user::rwx\ngroup::r-x\nother::---\n"], self.calls())
        self.env["CONTRACT_FAIL"] = "getfacl"
        result = self.run_command(command)
        self.assertNotEqual(0, result.returncode)

    def test_acl_clear_failure_stops_the_transfer(self):
        self.env["CONTRACT_FAIL"] = "setfacl"
        command = commands._ssh_acl_reference_preview("host-a", "/brick/source", "host-b", "/brick/target", clear_default=True)
        result = self.run_command(command)
        self.assertEqual(17, result.returncode, result.stderr)
        self.assertEqual([["setfacl", ["-k", "--", "/brick/target"], ""]], self.calls())

    def test_planned_cleanup_and_revert_commands_reach_helper(self):
        from gluster_heal_tool.apply_planning_review import _plan_cleanup_dead_file_refs
        # Exercise command construction only; this fixture is not repair eligibility evidence.
        source = str(self.root / "residue")
        result = _plan_cleanup_dead_file_refs({
            "action_id": "cleanup-fixture", "logical_path": "fixture/residue",
            "action_type": "cleanup_dead_file_refs", "repair_strategy": "delete_arbiter_only_residue",
            "stale_backends_by_host": {"host-a": [source]},
        }, execution_mode="dry-run", backup_root=str(self.root / "backups"), backup_mode="required", batch=True)
        self.assertTrue(result.revert_steps)
        for step in [*result.steps, *result.revert_steps]:
            with self.subTest(step=step.step_type):
                self.assertTrue(step.command_preview)
                completed = self.run_command(step.command_preview)
                self.assertEqual(0, completed.returncode, completed.stderr)
        reverted = self.calls()[-1]
        self.assertEqual("cp", reverted[0])
        self.assertEqual(source, reverted[1][-1])
        self.assertEqual(result.revert_steps[-1].source_path, reverted[1][-2])

    def test_restricted_stat_and_xattr_arity(self):
        cases = [["stat", "-c", "%n", "--", "/fixture"],
                 ["stat", "-c", "%F", "--", "/fixture", "/extra"],
                 ["setfattr", "-n", "trusted.glusterfs.mdata", "-v", "0x00", "--", "/fixture", "/extra"],
                 ["setfattr", "-x", "trusted.glusterfs.mdata", "--", "/fixture"]]
        for command in cases:
            with self.subTest(command=command):
                result = self.run_command(["bash", str(HELPER), *command])
                self.assertNotEqual(0, result.returncode)
                self.assertEqual([], self.calls())

    def test_generated_gfid_links(self):
        for directory in (False, True):
            with self.subTest(directory=directory):
                self.trace.unlink(missing_ok=True)
                target = self.root / ("directory" if directory else "file")
                target.mkdir() if directory else target.write_text("fixture")
                command = commands._ssh_relink_gfid_preview("host-a", str(self.root), str(target), directory=directory)
                result = self.run_command(command)
                self.assertEqual(0, result.returncode, result.stderr)
                handle = str(self.root / ".glusterfs/12/34" / GFID)
                expected = ["-s", "--", str(target), handle] if directory else ["--", str(target), handle]
                self.assertIn(["ln", expected, ""], self.calls())

    def test_generated_brick_pull_and_server_dispatch(self):
        command = rsync_brick_pull_command("host-b", "host-a", "/brick/source file", "/brick/target file")
        result = self.run_command(command)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["gluster-repair@host-a:/brick/source file", "/brick/target file"], self.calls()[0][1][-2:])
        server = ["--server", "--sender", "-logDtpre.iLsfxCIvu", ".", "/brick/source"]
        result = self.run_command(["bash", str(HELPER), "rsync-server", *server])
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["rsync", server, ""], self.calls()[-1])


if __name__ == "__main__":
    unittest.main()
