#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  gluster-deploy-heal-tool.sh -v VOLUME [options]

Options:
  -v VOLUME          Gluster volume name to inspect via `gluster volume info`
  -r ROOT_DIR        Project root to deploy from
  -d INSTALL_DIR     Install root on the target hosts
                     (default: /opt/gluster-repair)
  -l LOGIN_USER      SSH login user on the target hosts (default: service user)
  -s SERVICE_USER    Service account on the target hosts
                     (default: gluster-repair)
  --preflight        Check SSH, helper, and install-path readiness without copying files
  --dry-run          Print the planned commands without changing hosts
  --setup-keys       If SSH key auth is missing, try to generate/copy a key
  -h, --help         Show help

Behavior:
  - Discovers brick hosts from `gluster volume info <VOLUME>`
  - In dry-run mode, prints the commands it would run and exits without
    touching remote hosts
  - Checks service-account SSH key auth to each host with BatchMode first
  - If keys are missing and `--setup-keys` is not set, prints exact commands
  - If keys are missing and `--setup-keys` is set, tries to run `ssh-copy-id`
EOF
}

VOLUME=""
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/opt/gluster-repair"
DRY_RUN=0
PREFLIGHT=0
SETUP_KEYS=0
SERVICE_USER="gluster-repair"
LOGIN_USER=""

DEFAULT_OPERATOR_USER="${SUDO_USER:-${USER:-}}"
if [[ -z "$DEFAULT_OPERATOR_USER" ]]; then
  DEFAULT_OPERATOR_USER="root"
fi
DEFAULT_OPERATOR_HOME="$(getent passwd "$DEFAULT_OPERATOR_USER" | awk -F: '{print $6; exit}')"
if [[ -z "$DEFAULT_OPERATOR_HOME" ]]; then
  DEFAULT_OPERATOR_HOME="${HOME:-/root}"
fi
LOCAL_PUBKEY_PATH="${DEFAULT_OPERATOR_HOME}/.ssh/id_ed25519.pub"
LOCAL_PRIVKEY_PATH="${DEFAULT_OPERATOR_HOME}/.ssh/id_ed25519"
SSH_TRANSPORT="ssh -i ${LOCAL_PRIVKEY_PATH} -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
SSH_IDENTITY_OPTS=(
  -i "${LOCAL_PRIVKEY_PATH}"
  -o IdentitiesOnly=yes
)
RSYNC_TIMEOUT="${GLUSTER_REPAIR_RSYNC_TIMEOUT:-120}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -v) VOLUME="$2"; shift 2 ;;
    -r) ROOT_DIR="$2"; shift 2 ;;
    -d) INSTALL_DIR="$2"; shift 2 ;;
    -l) LOGIN_USER="$2"; shift 2 ;;
    -s) SERVICE_USER="$2"; shift 2 ;;
    --preflight) PREFLIGHT=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --setup-keys) SETUP_KEYS=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$LOGIN_USER" ]] || LOGIN_USER="$SERVICE_USER"
[[ -n "$VOLUME" ]] || { echo "ERROR: -v VOLUME is required" >&2; usage >&2; exit 1; }
if [[ "$INSTALL_DIR" != "/" ]]; then
  INSTALL_DIR="${INSTALL_DIR%/}"
fi
HOST_OPS_PATH="${INSTALL_DIR}/gluster-host-ops.sh"
REMOTE_INSTALL_DIR="$(printf '%q' "$INSTALL_DIR")"
REMOTE_HOST_OPS_PATH="$(printf '%q' "$HOST_OPS_PATH")"
REMOTE_RESOLVER_PATH="$(printf '%q' "$INSTALL_DIR/gluster-resolve-gfid-plus.sh")"

mapfile -t HOSTS < <(
  gluster volume info "$VOLUME" |
    awk -F: '
      $1 ~ /^Brick[0-9]+/ {
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", $2);
        if (!seen[$2]++) print $2;
      }
    '
)

VOLTYPE="$(gluster volume info "$VOLUME" | awk -F: '$1=="Type" {gsub(/^[[:space:]]+|[[:space:]]+$/, "", $2); print $2; exit}')"
if [[ "$VOLTYPE" != "Replicate" ]]; then
  echo "ERROR: volume $VOLUME is type '$VOLTYPE'; only pure Replicate volumes are supported by this tool." >&2
  exit 1
fi

[[ ${#HOSTS[@]} -gt 0 ]] || { echo "ERROR: no brick hosts found for volume $VOLUME" >&2; exit 1; }

TOOL_FILES=(
  gluster-host-ops.sh
  gluster-log-ops.sh
  gluster-heal-tool.py
  gluster-manager.py
  gluster-worker.py
  gluster-resolve-gfid-plus.sh
)

shopt -s nullglob
PACKAGE_MODULES=( "${ROOT_DIR}/gluster_heal_tool/"*.py )
TOOL_SOURCES=()
for file in "${TOOL_FILES[@]}"; do
  TOOL_SOURCES+=( "${ROOT_DIR}/${file}" )
done

echo "==> refreshing health-check and brick-layout cache for ${VOLUME}"
if python3 "${ROOT_DIR}/gluster-manager.py" health-check \
  --volume "${VOLUME}" >/dev/null; then
  echo "    health-check cache refresh: ok"
else
  echo "WARNING: health-check cache refresh failed; run health-check after deploy to seed the brick-layout cache." >&2
fi

check_local_inputs() {
  local missing=0
  for file in "${TOOL_FILES[@]}"; do
    if [[ ! -f "${ROOT_DIR}/${file}" ]]; then
      echo "ERROR: required deploy file not found: ${ROOT_DIR}/${file}" >&2
      missing=1
    fi
  done
  if [[ ${#PACKAGE_MODULES[@]} -eq 0 ]]; then
    echo "ERROR: no package modules found under ${ROOT_DIR}/gluster_heal_tool/" >&2
    missing=1
  fi
  if [[ $missing -ne 0 ]]; then
    exit 1
  fi
}

check_local_inputs

print_dry_run_ssh() {
  local host="$1"
  local remote_cmd="$2"
  printf 'DRY-RUN: ssh -i %q -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new %q %q\n' \
    "$LOCAL_PRIVKEY_PATH" "${LOGIN_USER}@${host}" "$remote_cmd"
}

print_dry_run_rsync() {
  local dest="$1"
  shift
  printf 'DRY-RUN: rsync -q -a -e %q --rsync-path=%q --timeout=%q' \
    "$SSH_TRANSPORT" "sudo -n ${HOST_OPS_PATH} rsync-server" "$RSYNC_TIMEOUT"
  for src in "$@"; do
    printf ' %q' "$src"
  done
  printf ' %q\n' "$dest"
}

print_dry_run_plan() {
  echo "==> dry-run deploying into ${INSTALL_DIR} (rsync timeout ${RSYNC_TIMEOUT}s)"
  if [[ $SETUP_KEYS -eq 1 ]]; then
    if [[ -f "$LOCAL_PUBKEY_PATH" ]]; then
      printf 'DRY-RUN: would try ssh-copy-id -i %q %q if SSH auth is missing\n' \
        "$LOCAL_PUBKEY_PATH" "${LOGIN_USER}@<host>"
    else
      printf 'DRY-RUN: would generate a local key pair at %q before trying ssh-copy-id\n' \
        "$LOCAL_PRIVKEY_PATH"
    fi
  fi
  for host in "${HOSTS[@]}"; do
    echo "==> ${host}"
    echo "DRY-RUN: would check SSH key auth for ${LOGIN_USER}@${host}"
    if [[ $SETUP_KEYS -eq 1 ]]; then
      if [[ -f "$LOCAL_PUBKEY_PATH" ]]; then
        printf 'DRY-RUN: would run ssh-copy-id -i %q %q if needed\n' \
          "$LOCAL_PUBKEY_PATH" "${LOGIN_USER}@${host}"
      else
        printf 'DRY-RUN: would generate a local key pair at %q and then run ssh-copy-id -i %q %q\n' \
          "$LOCAL_PRIVKEY_PATH" "$LOCAL_PUBKEY_PATH" "${LOGIN_USER}@${host}"
      fi
    fi
    print_dry_run_ssh "$host" "sudo -n ${HOST_OPS_PATH} mkdir -p -- ${INSTALL_DIR}"
    print_dry_run_ssh "$host" "sudo -n ${HOST_OPS_PATH} mkdir -p -- ${INSTALL_DIR}/gluster_heal_tool"
    print_dry_run_rsync "${LOGIN_USER}@${host}:${INSTALL_DIR}/" "${TOOL_SOURCES[@]}"
    print_dry_run_rsync "${LOGIN_USER}@${host}:${INSTALL_DIR}/gluster_heal_tool/" \
      "${PACKAGE_MODULES[@]}"
    echo "    completed in dry-run mode"
  done
}

if [[ $DRY_RUN -eq 1 ]]; then
  print_dry_run_plan
  exit 0
fi

print_preflight_header() {
  echo "==> preflight checking ${VOLUME}"
  echo "    login user: ${LOGIN_USER}"
  echo "    service user: ${SERVICE_USER}"
  echo "    install dir: ${INSTALL_DIR}"
}

ssh_ready() {
  local host="$1"
  ssh "${SSH_IDENTITY_OPTS[@]}" -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new "${LOGIN_USER}@${host}" true >/dev/null 2>&1
}

ensure_local_key() {
  if [[ -f "$LOCAL_PUBKEY_PATH" ]]; then
    return 0
  fi
  mkdir -p "$(dirname "$LOCAL_PRIVKEY_PATH")"
  chmod 700 "$(dirname "$LOCAL_PRIVKEY_PATH")"
  ssh-keygen -q -t ed25519 -N "" -f "$LOCAL_PRIVKEY_PATH"
}

print_key_help() {
  local host="$1"
  cat <<EOF
SSH key auth is not set up for ${LOGIN_USER}@${host}.

Try:
  ssh -i "${LOCAL_PRIVKEY_PATH}" -o IdentitiesOnly=yes ${LOGIN_USER}@${host}

Or from this host:
  ssh-copy-id -i "${LOCAL_PUBKEY_PATH}" ${LOGIN_USER}@${host}
EOF
}

check_or_setup_keys() {
  local host="$1"
  if ssh_ready "$host"; then
    return 0
  fi
  if [[ $SETUP_KEYS -eq 1 ]]; then
    ensure_local_key
    if command -v ssh-copy-id >/dev/null 2>&1; then
      echo "Attempting ssh-copy-id for ${host}"
      ssh-copy-id -i "${LOCAL_PUBKEY_PATH}" -o IdentitiesOnly=yes -o IdentityFile="${LOCAL_PRIVKEY_PATH}" "${LOGIN_USER}@${host}" || true
      if ssh_ready "$host"; then
        return 0
      fi
    fi
  fi
  ensure_local_key
  print_key_help "$host"
  return 1
}

check_remote_prereqs() {
  local host="$1"
  local probe_path="/tmp/gluster-repair-host-ops-probe-${host}-$$"
  local remote_probe_path
  remote_probe_path="$(printf '%q' "$probe_path")"
  local remote_cmd="sudo -n ${REMOTE_HOST_OPS_PATH} mkdir -p -- ${remote_probe_path} && sudo -n ${REMOTE_HOST_OPS_PATH} rm -rf -- ${remote_probe_path} && sudo -n ${REMOTE_RESOLVER_PATH} -h >/dev/null"
  if ssh "${SSH_IDENTITY_OPTS[@]}" -o BatchMode=yes -o StrictHostKeyChecking=accept-new "${LOGIN_USER}@${host}" \
    "$remote_cmd" >/dev/null 2>&1; then
    return 0
  fi
  echo "ERROR: ${host} is missing a usable host-ops/resolver helper or sudoers wiring; run gluster-bootstrap-volume.sh first." >&2
  return 1
}

check_remote_install_path() {
  local host="$1"
  local probe_path="${INSTALL_DIR}/.deploy-preflight-${host}-$$"
  local remote_probe_path
  remote_probe_path="$(printf '%q' "$probe_path")"
  local remote_cmd="sudo -n ${REMOTE_HOST_OPS_PATH} mkdir -p -- ${REMOTE_INSTALL_DIR} && sudo -n ${REMOTE_HOST_OPS_PATH} mkdir -p -- ${remote_probe_path} && sudo -n ${REMOTE_HOST_OPS_PATH} rm -rf -- ${remote_probe_path}"
  if ssh "${SSH_IDENTITY_OPTS[@]}" -o BatchMode=yes -o StrictHostKeyChecking=accept-new "${LOGIN_USER}@${host}" \
    "$remote_cmd" >/dev/null 2>&1; then
    return 0
  fi
  echo "ERROR: ${host} cannot prepare install dir ${INSTALL_DIR}; check permissions and prefix selection." >&2
  return 1
}

run_preflight() {
  local missing=0
  print_preflight_header
  for host in "${HOSTS[@]}"; do
    echo "==> ${host}"
    if check_or_setup_keys "$host"; then
      echo "    ssh key auth: ok"
    else
      echo "    ssh key auth: failed"
      missing=1
      continue
    fi
    if check_remote_prereqs "$host"; then
      echo "    host-ops + sudoers: ok"
    else
      echo "    host-ops + sudoers: failed"
      missing=1
      continue
    fi
    if check_remote_install_path "$host"; then
      echo "    install prefix: ok"
    else
      echo "    install prefix: failed"
      missing=1
    fi
  done
  if [[ $missing -ne 0 ]]; then
    echo "ERROR: deploy preflight failed; fix the hosts before copying files." >&2
    return 1
  fi
  echo "==> preflight ready"
}

if [[ $PREFLIGHT -eq 1 ]]; then
  run_preflight
  exit 0
fi

missing_keys=0
for host in "${HOSTS[@]}"; do
  if ! check_or_setup_keys "$host"; then
    missing_keys=1
  fi
done

if [[ $missing_keys -ne 0 ]]; then
  echo "ERROR: SSH key auth is not ready for all hosts; deployment aborted." >&2
  exit 1
fi

missing_remote_prereqs=0
for host in "${HOSTS[@]}"; do
  if ! check_remote_prereqs "$host"; then
    missing_remote_prereqs=1
  fi
done

if [[ $missing_remote_prereqs -ne 0 ]]; then
  exit 1
fi

missing_install_path=0
for host in "${HOSTS[@]}"; do
  if ! check_remote_install_path "$host"; then
    missing_install_path=1
  fi
done

if [[ $missing_install_path -ne 0 ]]; then
  exit 1
fi

echo "==> deploying into ${INSTALL_DIR} (rsync timeout ${RSYNC_TIMEOUT}s)"
for host in "${HOSTS[@]}"; do
  host_start="$SECONDS"
  echo "==> ${host}"
  ssh "${SSH_IDENTITY_OPTS[@]}" -o BatchMode=yes -o StrictHostKeyChecking=accept-new "${LOGIN_USER}@${host}" \
    "sudo -n ${REMOTE_HOST_OPS_PATH} mkdir -p -- ${REMOTE_INSTALL_DIR}"
  ssh "${SSH_IDENTITY_OPTS[@]}" -o BatchMode=yes -o StrictHostKeyChecking=accept-new "${LOGIN_USER}@${host}" \
    "sudo -n ${REMOTE_HOST_OPS_PATH} mkdir -p -- ${REMOTE_INSTALL_DIR}/gluster_heal_tool"
  rsync -q -a -e "${SSH_TRANSPORT}" --rsync-path="sudo -n ${REMOTE_HOST_OPS_PATH} rsync-server" \
    --timeout="${RSYNC_TIMEOUT}" \
    "${TOOL_SOURCES[@]}" \
    "${LOGIN_USER}@${host}:${INSTALL_DIR}/"
  # Copy every package module, including new library splits such as executor.py.
  rsync -q -a -e "${SSH_TRANSPORT}" --rsync-path="sudo -n ${REMOTE_HOST_OPS_PATH} rsync-server" \
    --timeout="${RSYNC_TIMEOUT}" \
    "${PACKAGE_MODULES[@]}" \
    "${LOGIN_USER}@${host}:${INSTALL_DIR}/gluster_heal_tool/"
  host_elapsed=$(( SECONDS - host_start ))
  echo "    completed in ${host_elapsed}s"
done
