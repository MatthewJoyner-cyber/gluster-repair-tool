# SPDX-License-Identifier: GPL-2.0-only
"""Default install and service-account paths."""
from __future__ import annotations

from pathlib import Path

DEFAULT_INSTALL_DIR = Path("/opt/gluster-repair")
DEFAULT_SERVICE_USER = "gluster-repair"
DEFAULT_SERVICE_HOME = Path("/var/lib/gluster-repair")
DEFAULT_SERVICE_KEY_NAME = "gluster-repair-service"
DEFAULT_SERVICE_KEY_PATH = DEFAULT_SERVICE_HOME / ".ssh" / DEFAULT_SERVICE_KEY_NAME
DEFAULT_HOST_OPS_PATH = DEFAULT_INSTALL_DIR / "gluster-host-ops.sh"
DEFAULT_LOG_OPS_PATH = DEFAULT_INSTALL_DIR / "gluster-log-ops.sh"
DEFAULT_RESOLVER_PATH = DEFAULT_INSTALL_DIR / "gluster-resolve-gfid-plus.sh"
DEFAULT_WORKER_PATH = DEFAULT_INSTALL_DIR / "gluster-worker.py"
DEFAULT_MANAGER_PATH = DEFAULT_INSTALL_DIR / "gluster-manager.py"
DEFAULT_HEAL_TOOL_PATH = DEFAULT_INSTALL_DIR / "gluster-heal-tool.py"
