#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="${SCRIPT_DIR}"
if [[ $# -gt 0 && "${1#-}" == "$1" ]]; then
  SRC_DIR="$1"
  shift
fi
exec "${SRC_DIR}/gluster-deploy-heal-tool.sh" -r "${SRC_DIR}" "$@"
