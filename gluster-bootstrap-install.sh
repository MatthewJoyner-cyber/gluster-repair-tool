#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Sourced by bootstrap and installation tests; no work is done at source time.

verify_tool_tree() (
  set -euo pipefail
  local root
  root="$(cd "$1" && pwd)"
  local entry
  for entry in gluster-manager.py gluster-heal-tool.py gluster-worker.py \
      gluster-host-ops.sh gluster-log-ops.sh gluster-resolve-gfid-plus.sh \
      gluster_heal_tool/__init__.py; do
    [[ -f "$root/$entry" ]] || { echo "ERROR: incomplete tool tree: $root/$entry" >&2; exit 1; }
  done
  # Exclude the checkout, user site packages and Python environment overrides.
  cd /
  python3 -EsB - "$root" <<'PY'
import ast
from pathlib import Path
import sys

if sys.version_info < (3, 12):
    raise SystemExit("ERROR: bootstrap requires Python 3.12 or newer")
root = Path(sys.argv[1]).resolve()
for source in (root / "gluster_heal_tool").glob("*.py"):
    ast.parse(source.read_bytes(), filename=str(source))
sys.path.insert(0, str(root))
import gluster_heal_tool
if Path(gluster_heal_tool.__file__).resolve() != root / "gluster_heal_tool/__init__.py":
    raise SystemExit("ERROR: imported package is outside the installation")
PY
  for entry in gluster-manager.py gluster-heal-tool.py gluster-worker.py; do
    python3 -EsB "$root/$entry" --help >/dev/null
  done
  for entry in gluster-host-ops.sh gluster-log-ops.sh gluster-resolve-gfid-plus.sh; do
    bash "$root/$entry" --help >/dev/null
  done
)

install_tool_tree() (
  set -euo pipefail
  local source="$1" destination="$2" entry
  # An invalid source must fail before even creating the destination.
  verify_tool_tree "$source"
  install -d -m 0755 "$destination/gluster_heal_tool"
  for entry in gluster-manager.py gluster-heal-tool.py gluster-worker.py \
      gluster-host-ops.sh gluster-log-ops.sh gluster-resolve-gfid-plus.sh; do
    install -m 0755 "$source/$entry" "$destination/$entry"
  done
  for entry in "$source/gluster_heal_tool/"*.py; do
    install -m 0644 "$entry" "$destination/gluster_heal_tool/"
  done
  verify_tool_tree "$destination"
)

write_bootstrap_sudoers() (
  set -euo pipefail
  local service_user="$1" install_dir="$2" brick_only="$3" output="$4"
  [[ "$service_user" == gluster-repair && "$install_dir" == /opt/gluster-repair && "$brick_only" =~ ^[01]$ ]] || {
    echo "ERROR: unsupported bootstrap sudoers layout" >&2; exit 1;
  }
  if [[ "$brick_only" == 1 ]]; then
    cat > "$output" <<EOF
Defaults:$service_user !requiretty
$service_user ALL=(root) NOPASSWD: $install_dir/gluster-host-ops.sh brick-down --volume * --brick *
$service_user ALL=(root) NOPASSWD: $install_dir/gluster-host-ops.sh brick-kick --volume *
EOF
  else
    cat > "$output" <<EOF
Defaults:$service_user !requiretty
$service_user ALL=(root) NOPASSWD: $install_dir/gluster-host-ops.sh *
$service_user ALL=(root) NOPASSWD: $install_dir/gluster-log-ops.sh *
$service_user ALL=(root) NOPASSWD: $install_dir/gluster-worker.py *
$service_user ALL=(root) NOPASSWD: $install_dir/gluster-resolve-gfid-plus.sh *
EOF
  fi
)
