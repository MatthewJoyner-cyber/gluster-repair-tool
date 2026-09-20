# SPDX-License-Identifier: GPL-2.0-only
"""Regression tests for the staged brick-side host operations helper."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase


class HostOpsTests(TestCase):
    def test_link_file_gfid_parses_named_hex_getfattr_output(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        helper = repo_root / "gluster-host-ops.sh"
        gfid_hex = "90000000000040008000000000000001"
        gfid = "90000000-0000-4000-8000-000000000001"

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "brick"
            target = root / "alpha" / "payload.txt"
            target.parent.mkdir(parents=True)
            target.write_text("payload\n", encoding="utf-8")
            stub_dir = Path(temp_dir) / "bin"
            stub_dir.mkdir()
            getfattr = stub_dir / "getfattr"
            getfattr.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' '# file: ignored' 'trusted.gfid=0x90000000000040008000000000000001'\n",
                encoding="utf-8",
            )
            getfattr.chmod(0o755)
            env = os.environ | {"PATH": f"{stub_dir}:{os.environ['PATH']}"}

            result = subprocess.run(
                [str(helper), "link-file-gfid", "--backend-root", str(root), "--target", str(target)],
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            link_path = root / ".glusterfs" / gfid_hex[:2] / gfid_hex[2:4] / gfid
            self.assertTrue(link_path.exists())
            self.assertEqual(target.stat().st_ino, link_path.stat().st_ino)
