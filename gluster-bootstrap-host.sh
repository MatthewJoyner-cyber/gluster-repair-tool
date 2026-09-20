#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  gluster-bootstrap-host.sh -H HOST [options]

Options:
  -H HOST             Target brick host to bootstrap
  -l LOGIN_USER       SSH login user on the target host (default: invoking user)
  -i LOGIN_PRIVKEY    SSH private key used for LOGIN_USER on this machine
                      (default: invoking user's ~/.ssh/id_ed25519)
  -s SERVICE_USER     Service account to create/use on the target host
                      (default: gluster-repair)
  -m SERVICE_HOME     Home directory for the service account
                      (default: /var/lib/gluster-repair)
  -d INSTALL_DIR      Install root on the target host (default: /opt/gluster-repair)
  -r ROOT_DIR         Local project root to install from (default: script directory)
  -p PUBKEY_PATH      Local public key to install for the service account
                      (default: invoking user's ~/.ssh/id_ed25519.pub)
  -k KNOWN_HOSTS_PATH  Verified SSH host keys to install for service-account
                      brick-to-brick transfers (optional)
  --preflight         Check SSH and sudo readiness without changing the host
  --brick-ops-only    Install a lab-only sudoers drop-in that allows only
                      brick-down and brick-kick through the host-ops helper
                      and skips the broader worker/log sudoers entries
  -h, --help          Show help

Behavior:
  - Creates the service account and its authorized_keys if missing.
  - Installs supplied verified peer host keys as the service account's known_hosts.
  - Installs or updates the tool and verifies all six installed entry points.
  - Writes a narrow sudoers drop-in for the worker, host-ops, and log helpers,
    or a brick-maintenance-only drop-in when --brick-ops-only is used.
  - Reuses existing accounts and service keys; updates tool files and sudoers.
  - Only the default service user, home, and install directory are supported.
EOF
}

DEFAULT_OPERATOR_USER="${SUDO_USER:-${USER:-}}"
if [[ -z "$DEFAULT_OPERATOR_USER" ]]; then
  DEFAULT_OPERATOR_USER="root"
fi
DEFAULT_OPERATOR_HOME="$(getent passwd "$DEFAULT_OPERATOR_USER" | awk -F: '{print $6; exit}')"
if [[ -z "$DEFAULT_OPERATOR_HOME" ]]; then
  DEFAULT_OPERATOR_HOME="${HOME:-/root}"
fi

SERVICE_KEY_DIR="${DEFAULT_OPERATOR_HOME}/.ssh/gluster-repair-service"
SERVICE_KEY_NAME="gluster-repair-service"
SERVICE_PRIVKEY_PATH="${SERVICE_KEY_DIR}/${SERVICE_KEY_NAME}"
SERVICE_PUBKEY_PATH="${SERVICE_PRIVKEY_PATH}.pub"

HOST=""
LOGIN_USER="$DEFAULT_OPERATOR_USER"
SERVICE_USER="gluster-repair"
SERVICE_HOME="/var/lib/gluster-repair"
INSTALL_DIR="/opt/gluster-repair"
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOGIN_PRIVKEY_PATH="${DEFAULT_OPERATOR_HOME}/.ssh/id_ed25519"
PUBKEY_PATH="${DEFAULT_OPERATOR_HOME}/.ssh/id_ed25519.pub"
SERVICE_KNOWN_HOSTS_PATH=""
BRICK_OPS_ONLY=0
PREFLIGHT=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -H) HOST="$2"; shift 2 ;;
    -l) LOGIN_USER="$2"; shift 2 ;;
    -i) LOGIN_PRIVKEY_PATH="$2"; shift 2 ;;
    -s) SERVICE_USER="$2"; shift 2 ;;
    -m) SERVICE_HOME="$2"; shift 2 ;;
    -d) INSTALL_DIR="$2"; shift 2 ;;
    -r) ROOT_DIR="$2"; shift 2 ;;
    -p) PUBKEY_PATH="$2"; shift 2 ;;
    -k) SERVICE_KNOWN_HOSTS_PATH="$2"; shift 2 ;;
    --preflight) PREFLIGHT=1; shift ;;
    --brick-ops-only) BRICK_OPS_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$HOST" ]] || { echo "ERROR: -H HOST is required" >&2; usage >&2; exit 1; }
[[ -n "$LOGIN_USER" ]] || LOGIN_USER="$DEFAULT_OPERATOR_USER"
if [[ "$SERVICE_USER" != gluster-repair || "$SERVICE_HOME" != /var/lib/gluster-repair || "$INSTALL_DIR" != /opt/gluster-repair ]]; then
  echo "ERROR: unsupported layout; use gluster-repair, /var/lib/gluster-repair and /opt/gluster-repair" >&2
  exit 1
fi
if [[ ! "$HOST" =~ ^[a-zA-Z0-9][a-zA-Z0-9._:-]*$ || ! "$LOGIN_USER" =~ ^[a-zA-Z_][a-zA-Z0-9_.-]*\$?$ ]]; then
  echo "ERROR: invalid host or login user" >&2
  exit 1
fi
if [[ ! -f "$LOGIN_PRIVKEY_PATH" ]]; then
  echo "ERROR: private key not found: $LOGIN_PRIVKEY_PATH" >&2
  exit 1
fi
if [[ -n "$SERVICE_KNOWN_HOSTS_PATH" && ! -f "$SERVICE_KNOWN_HOSTS_PATH" ]]; then
  echo "ERROR: service known_hosts file not found: $SERVICE_KNOWN_HOSTS_PATH" >&2
  exit 1
fi

ensure_pubkey() {
  if [[ -f "$PUBKEY_PATH" ]]; then
    return 0
  fi
  echo "ERROR: public key not found: $PUBKEY_PATH" >&2
  exit 1
}

ensure_pubkey

ensure_service_keypair() {
  if [[ -f "$SERVICE_PRIVKEY_PATH" && -f "$SERVICE_PUBKEY_PATH" ]]; then
    return 0
  fi
  if [[ -e "$SERVICE_PRIVKEY_PATH" || -e "$SERVICE_PUBKEY_PATH" ]]; then
    echo "ERROR: partial service SSH keypair found in ${SERVICE_KEY_DIR}; remove the partial pair and rerun bootstrap." >&2
    exit 1
  fi
  mkdir -p "$SERVICE_KEY_DIR"
  chmod 700 "$SERVICE_KEY_DIR"
  ssh-keygen -q -t ed25519 -N "" -f "$SERVICE_PRIVKEY_PATH"
}

TOOL_FILES=(
  gluster-bootstrap-install.sh
  gluster-host-ops.sh
  gluster-log-ops.sh
  gluster-heal-tool.py
  gluster-manager.py
  gluster-worker.py
  gluster-resolve-gfid-plus.sh
)

shopt -s nullglob
PACKAGE_MODULES=( "${ROOT_DIR}/gluster_heal_tool/"*.py )

check_local_inputs() {
  local missing=0
  for file in "${TOOL_FILES[@]}"; do
    if [[ ! -f "${ROOT_DIR}/${file}" ]]; then
      echo "ERROR: required bootstrap file not found: ${ROOT_DIR}/${file}" >&2
      missing=1
    fi
  done
  if [[ ${#PACKAGE_MODULES[@]} -eq 0 || ! -f "${ROOT_DIR}/gluster_heal_tool/__init__.py" ]]; then
    echo "ERROR: incomplete package under ${ROOT_DIR}/gluster_heal_tool/" >&2
    missing=1
  fi
  if [[ $missing -ne 0 ]]; then
    exit 1
  fi
}

check_local_inputs

if [[ $PREFLIGHT -eq 1 ]]; then
  echo "==> bootstrap preflight ${HOST}"
  echo "    login user: ${LOGIN_USER}"
  echo "    login private key: ${LOGIN_PRIVKEY_PATH}"
  echo "    service user: ${SERVICE_USER}"
  echo "    service home: ${SERVICE_HOME}"
  echo "    install dir: ${INSTALL_DIR}"
  echo "    shared service keypair: ${SERVICE_PRIVKEY_PATH}"
  if ! ssh -i "$LOGIN_PRIVKEY_PATH" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes -o UpdateHostKeys=no "${LOGIN_USER}@${HOST}" "true" >/dev/null 2>&1; then
    echo "ERROR: cannot reach ${HOST} as ${LOGIN_USER}" >&2
    exit 1
  fi
  if [[ "$LOGIN_USER" != "root" ]]; then
    if ! ssh -i "$LOGIN_PRIVKEY_PATH" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes -o UpdateHostKeys=no "${LOGIN_USER}@${HOST}" "sudo -n true" >/dev/null 2>&1; then
      echo "ERROR: sudo is not ready on ${HOST} for ${LOGIN_USER}" >&2
      exit 1
    fi
  fi
  echo "==> bootstrap preflight ready"
  exit 0
fi

ensure_service_keypair

SSH_OPTS=(
  -i "$LOGIN_PRIVKEY_PATH"
  -o IdentitiesOnly=yes
  -o BatchMode=yes
  -o ConnectTimeout=10
  -o StrictHostKeyChecking=accept-new
)

remote_stage_dir="$(
  ssh "${SSH_OPTS[@]}" "${LOGIN_USER}@${HOST}" \
    "mktemp -d /tmp/gluster-bootstrap-${SERVICE_USER}.XXXXXX"
)"
remote_bootstrap_cmd() {
  local remote_cmd=(
    bash
    -s
    --
    "$SERVICE_USER"
    "$SERVICE_HOME"
    "$INSTALL_DIR"
    "$remote_stage_dir"
    "$BRICK_OPS_ONLY"
  )
  if [[ "$LOGIN_USER" != "root" ]]; then
    remote_cmd=(
      sudo
      -n
      bash
      -s
      --
      "$SERVICE_USER"
      "$SERVICE_HOME"
      "$INSTALL_DIR"
      "$remote_stage_dir"
      "$BRICK_OPS_ONLY"
    )
  fi
  ssh -T "${SSH_OPTS[@]}" "${LOGIN_USER}@${HOST}" "${remote_cmd[@]}" <<'REMOTE'
set -euo pipefail
SERVICE_USER="$1"
SERVICE_HOME="$2"
INSTALL_DIR="$3"
STAGE_DIR="$4"
BRICK_OPS_ONLY="$5"

source "$STAGE_DIR/files/gluster-bootstrap-install.sh"
verify_tool_tree "$STAGE_DIR/files"

existing_entry="$(getent passwd "$SERVICE_USER" || true)"
if [[ -n "$existing_entry" ]]; then
  current_home="$(printf '%s\n' "$existing_entry" | awk -F: '{print $6}')"
  if [[ "$current_home" != "$SERVICE_HOME" ]]; then
    echo "ERROR: unsupported existing service account home: $current_home" >&2
    exit 1
  fi
fi

if [[ -z "$existing_entry" ]]; then
  useradd -m -d "$SERVICE_HOME" -s /bin/sh -U "$SERVICE_USER"
fi

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$SERVICE_HOME/.ssh"
install -d -o root -g root -m 1777 /tmp/gluster-repair

if [[ -f "$STAGE_DIR/authorized_keys.pub" ]]; then
  if [[ ! -f "$SERVICE_HOME/.ssh/authorized_keys" ]] || ! grep -qxFf "$STAGE_DIR/authorized_keys.pub" "$SERVICE_HOME/.ssh/authorized_keys"; then
    cat "$STAGE_DIR/authorized_keys.pub" >> "$SERVICE_HOME/.ssh/authorized_keys"
  fi
  chown "$SERVICE_USER:$SERVICE_USER" "$SERVICE_HOME/.ssh/authorized_keys"
  chmod 0600 "$SERVICE_HOME/.ssh/authorized_keys"
fi

if [[ -f "$STAGE_DIR/service_gluster_repair_service.pub" ]]; then
  if [[ ! -f "$SERVICE_HOME/.ssh/authorized_keys" ]] || ! grep -qxFf "$STAGE_DIR/service_gluster_repair_service.pub" "$SERVICE_HOME/.ssh/authorized_keys"; then
    cat "$STAGE_DIR/service_gluster_repair_service.pub" >> "$SERVICE_HOME/.ssh/authorized_keys"
  fi
  chown "$SERVICE_USER:$SERVICE_USER" "$SERVICE_HOME/.ssh/authorized_keys"
  chmod 0600 "$SERVICE_HOME/.ssh/authorized_keys"
  install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0644 "$STAGE_DIR/service_gluster_repair_service.pub" "$SERVICE_HOME/.ssh/gluster-repair-service.pub"
fi
if [[ -f "$STAGE_DIR/service_gluster_repair_service" ]]; then
  install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0600 "$STAGE_DIR/service_gluster_repair_service" "$SERVICE_HOME/.ssh/gluster-repair-service"
fi
if [[ -f "$STAGE_DIR/service_known_hosts" ]]; then
  install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0600 "$STAGE_DIR/service_known_hosts" "$SERVICE_HOME/.ssh/known_hosts"
fi

install_tool_tree "$STAGE_DIR/files" "$INSTALL_DIR"

chown -R root:root "$INSTALL_DIR"
chmod +x "$INSTALL_DIR/gluster-host-ops.sh" "$INSTALL_DIR/gluster-log-ops.sh" "$INSTALL_DIR/gluster-heal-tool.py" "$INSTALL_DIR/gluster-manager.py" "$INSTALL_DIR/gluster-worker.py" "$INSTALL_DIR/gluster-resolve-gfid-plus.sh"
verify_tool_tree "$INSTALL_DIR"

sudoers_tmp="$(mktemp /tmp/gluster-repair-sudoers.XXXXXX)"
trap 'rm -f "$sudoers_tmp"' EXIT
write_bootstrap_sudoers "$SERVICE_USER" "$INSTALL_DIR" "$BRICK_OPS_ONLY" "$sudoers_tmp"
chmod 0440 "$sudoers_tmp"
visudo -cf "$sudoers_tmp" >/dev/null
install -o root -g root -m 0440 "$sudoers_tmp" /etc/sudoers.d/gluster-repair-worker
rm -f "$sudoers_tmp"

rm -rf "$STAGE_DIR"
REMOTE
}

stage_files() {
  ssh "${SSH_OPTS[@]}" "${LOGIN_USER}@${HOST}" "mkdir -p '$remote_stage_dir/files/gluster_heal_tool'"
  for file in "${TOOL_FILES[@]}"; do
    scp "${SSH_OPTS[@]}" -q \
      "${ROOT_DIR}/${file}" "${LOGIN_USER}@${HOST}:${remote_stage_dir}/files/"
  done
  scp "${SSH_OPTS[@]}" -q \
    "${PACKAGE_MODULES[@]}" "${LOGIN_USER}@${HOST}:${remote_stage_dir}/files/gluster_heal_tool/"
  scp "${SSH_OPTS[@]}" -q \
    "$PUBKEY_PATH" "${LOGIN_USER}@${HOST}:${remote_stage_dir}/authorized_keys.pub"
  scp "${SSH_OPTS[@]}" -q \
    "$SERVICE_PUBKEY_PATH" "${LOGIN_USER}@${HOST}:${remote_stage_dir}/service_gluster_repair_service.pub"
  scp "${SSH_OPTS[@]}" -q \
    "$SERVICE_PRIVKEY_PATH" "${LOGIN_USER}@${HOST}:${remote_stage_dir}/service_gluster_repair_service"
  if [[ -n "$SERVICE_KNOWN_HOSTS_PATH" ]]; then
    scp "${SSH_OPTS[@]}" -q \
      "$SERVICE_KNOWN_HOSTS_PATH" "${LOGIN_USER}@${HOST}:${remote_stage_dir}/service_known_hosts"
  fi
}

cleanup_stage() {
  ssh "${SSH_OPTS[@]}" "${LOGIN_USER}@${HOST}" "rm -rf '$remote_stage_dir'" >/dev/null 2>&1 || true
}

trap cleanup_stage EXIT

echo "==> Bootstrapping ${HOST}"
stage_files
remote_bootstrap_cmd
echo "==> ${HOST} ready"
