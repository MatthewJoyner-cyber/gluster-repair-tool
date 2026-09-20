#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  gluster-bootstrap-volume.sh -v VOLUME [options]

Options:
  -v VOLUME          Gluster volume name used to discover brick hosts
  -r ROOT_DIR        Project root to bootstrap from
  -l LOGIN_USER      SSH login user on the target hosts (default: invoking user)
  -i LOGIN_PRIVKEY   SSH private key used for LOGIN_USER on this machine
                     (default: invoking user's ~/.ssh/id_ed25519)
  -s SERVICE_USER    Service account to create/use on the target hosts
                     (default: gluster-repair)
  -m SERVICE_HOME    Home directory for the service account
                     (default: /var/lib/gluster-repair)
  -d INSTALL_DIR     Install root on the target hosts (default: /opt/gluster-repair)
  -p PUBKEY_PATH     Local public key to install for the service account
                     (default: invoking user's ~/.ssh/id_ed25519.pub)
  --preflight        Check bootstrap readiness without changing hosts
  -h, --help         Show help

Behavior:
  - Discovers brick hosts from `gluster volume info <VOLUME>`
  - Bootstraps each host in turn using gluster-bootstrap-host.sh
  - Installs controller-verified peer host keys for brick-to-brick service transfers
  - Reuses accounts and service keys; updates tool files and sudoers
  - Supports only the default service user, home, and install directory
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

VOLUME=""
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOGIN_USER="$DEFAULT_OPERATOR_USER"
LOGIN_PRIVKEY_PATH="${DEFAULT_OPERATOR_HOME}/.ssh/id_ed25519"
SERVICE_USER="gluster-repair"
SERVICE_HOME="/var/lib/gluster-repair"
INSTALL_DIR="/opt/gluster-repair"
PUBKEY_PATH="${DEFAULT_OPERATOR_HOME}/.ssh/id_ed25519.pub"
PREFLIGHT=0
KNOWN_HOSTS_PATH="${DEFAULT_OPERATOR_HOME}/.ssh/known_hosts"
SERVICE_KNOWN_HOSTS_PATH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -v) VOLUME="$2"; shift 2 ;;
    -r) ROOT_DIR="$2"; shift 2 ;;
    -l) LOGIN_USER="$2"; shift 2 ;;
    -i) LOGIN_PRIVKEY_PATH="$2"; shift 2 ;;
    -s) SERVICE_USER="$2"; shift 2 ;;
    -m) SERVICE_HOME="$2"; shift 2 ;;
    -d) INSTALL_DIR="$2"; shift 2 ;;
    -p) PUBKEY_PATH="$2"; shift 2 ;;
    --preflight) PREFLIGHT=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$VOLUME" ]] || { echo "ERROR: -v VOLUME is required" >&2; usage >&2; exit 1; }
[[ -n "$LOGIN_USER" ]] || LOGIN_USER="$DEFAULT_OPERATOR_USER"
if [[ "$SERVICE_USER" != gluster-repair || "$SERVICE_HOME" != /var/lib/gluster-repair || "$INSTALL_DIR" != /opt/gluster-repair ]]; then
  echo "ERROR: unsupported layout; use gluster-repair, /var/lib/gluster-repair and /opt/gluster-repair" >&2
  exit 1
fi
if [[ ! -f "$LOGIN_PRIVKEY_PATH" ]]; then
  echo "ERROR: private key not found: $LOGIN_PRIVKEY_PATH" >&2
  exit 1
fi

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
  echo "ERROR: volume $VOLUME is type '$VOLTYPE'; only pure Replicate volumes are supported by this bootstrap." >&2
  exit 1
fi

[[ ${#HOSTS[@]} -gt 0 ]] || { echo "ERROR: no brick hosts found for volume $VOLUME" >&2; exit 1; }

read_service_known_hosts() {
  if [[ ! -f "$KNOWN_HOSTS_PATH" ]]; then
    echo "ERROR: controller known_hosts file not found: $KNOWN_HOSTS_PATH" >&2
    exit 1
  fi
  local records host
  SERVICE_KNOWN_HOSTS_CONTENT=""
  for host in "${HOSTS[@]}"; do
    if ! records="$(ssh-keygen -F "$host" -f "$KNOWN_HOSTS_PATH" | awk '!/^#/ && NF')" || [[ -z "$records" ]]; then
      echo "ERROR: no verified SSH host key found for brick host $host in $KNOWN_HOSTS_PATH" >&2
      exit 1
    fi
    SERVICE_KNOWN_HOSTS_CONTENT+="${records}"$'\n'
  done
}

read_service_known_hosts

if [[ $PREFLIGHT -eq 1 ]]; then
  for host in "${HOSTS[@]}"; do
    echo "==> Preflight ${host}"
    "${ROOT_DIR}/gluster-bootstrap-host.sh" \
      -H "$host" \
      -l "$LOGIN_USER" \
      -i "$LOGIN_PRIVKEY_PATH" \
      -s "$SERVICE_USER" \
      -m "$SERVICE_HOME" \
      -d "$INSTALL_DIR" \
      -r "$ROOT_DIR" \
      -p "$PUBKEY_PATH" \
      -k "$KNOWN_HOSTS_PATH" \
      --preflight
  done
  exit 0
fi

SERVICE_KNOWN_HOSTS_PATH="$(mktemp /tmp/gluster-repair-known-hosts.XXXXXX)"
trap 'rm -f "$SERVICE_KNOWN_HOSTS_PATH"' EXIT
printf '%s' "$SERVICE_KNOWN_HOSTS_CONTENT" > "$SERVICE_KNOWN_HOSTS_PATH"
sort -u "$SERVICE_KNOWN_HOSTS_PATH" -o "$SERVICE_KNOWN_HOSTS_PATH"

for host in "${HOSTS[@]}"; do
  echo "==> Bootstrapping ${host}"
  "${ROOT_DIR}/gluster-bootstrap-host.sh" \
    -H "$host" \
    -l "$LOGIN_USER" \
    -i "$LOGIN_PRIVKEY_PATH" \
    -s "$SERVICE_USER" \
    -m "$SERVICE_HOME" \
    -d "$INSTALL_DIR" \
    -r "$ROOT_DIR" \
    -p "$PUBKEY_PATH" \
    -k "$SERVICE_KNOWN_HOSTS_PATH"
done
