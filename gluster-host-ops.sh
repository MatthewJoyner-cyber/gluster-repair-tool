#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail

die() {
  echo "ERROR: $*" >&2
  exit 2
}

mkdir_cmd() {
  [[ ${1:-} == "-p" ]] || die "mkdir requires '-p'"
  shift
  [[ ${1:-} == "--" ]] && shift
  [[ $# -eq 1 ]] || die "mkdir expects one path"
  exec mkdir -p -- "$1"
}

rm_cmd() {
  local flags="${1:-}"
  case "$flags" in
    -f|-rf|-fr) ;;
    *) die "rm requires '-f' or '-rf'" ;;
  esac
  shift
  [[ ${1:-} == "--" ]] && shift
  [[ $# -eq 1 ]] || die "rm expects one path"
  exec rm "$flags" -- "$1"
}

cp_cmd() {
  local flags="${1:-}"
  case "$flags" in
    -a|-fp|-pf) ;;
    *) die "cp requires '-a' or '-fp'" ;;
  esac
  shift
  [[ ${1:-} == "--" ]] && shift
  [[ $# -eq 2 ]] || die "cp expects source and target"
  exec cp "$flags" -- "$1" "$2"
}

mv_cmd() {
  [[ ${1:-} == "--" ]] && shift
  [[ $# -eq 2 ]] || die "mv expects source and target"
  exec mv -- "$1" "$2"
}

chmod_cmd() {
  local mode="${1:-}"
  [[ "$mode" =~ ^[0-7]{3,4}$ ]] || die "chmod expects an octal numeric mode"
  shift
  [[ ${1:-} == "--" ]] && shift
  [[ $# -eq 1 ]] || die "chmod expects one path"
  exec chmod "$mode" -- "$1"
}

chown_cmd() {
  local owner="${1:-}"
  [[ "$owner" =~ ^[0-9]+:[0-9]+$ ]] || die "chown expects numeric uid:gid"
  shift
  [[ ${1:-} == "--" ]] && shift
  [[ $# -eq 1 ]] || die "chown expects one path"
  exec chown "$owner" -- "$1"
}

setfattr_cmd() {
  [[ ${1:-} == "-n" ]] || die "setfattr requires '-n trusted.gfid'"
  local name="${2:-}"
  case "$name" in
    trusted.gfid|trusted.afr.*) ;;
    *) die "setfattr only allows trusted.gfid or trusted.afr.*" ;;
  esac
  [[ ${3:-} == "-v" ]] || die "setfattr requires '-v'"
  local value="${4:-}"
  case "$name" in
    trusted.gfid) [[ $value == 0s* ]] || die "setfattr value must be base64-encoded gfid" ;;
    trusted.afr.*) [[ $value == 0x* ]] || die "setfattr value must be hex for trusted.afr.*" ;;
  esac
  shift 4
  [[ ${1:-} == "--" ]] && shift
  [[ $# -eq 1 ]] || die "setfattr expects one path"
  exec setfattr -n "$name" -v "$value" -- "$1"
}

getfattr_cmd() {
  [[ ${1:-} == "-n" ]] || die "getfattr requires '-n trusted.gfid'"
  local name="${2:-}"
  [[ $name == trusted.gfid ]] || die "getfattr only allows trusted.gfid"
  [[ ${3:-} == "-e" ]] || die "getfattr requires '-e hex'"
  [[ ${4:-} == "hex" ]] || die "getfattr only allows '-e hex'"
  shift 4
  [[ ${1:-} == "--" ]] && shift
  [[ $# -eq 1 ]] || die "getfattr expects one path"
  exec getfattr -n trusted.gfid -e hex --absolute-names -- "$1"
}

link_gfid_cmd() {
  local kind="$1"
  shift
  [[ ${1:-} == "--backend-root" ]] || die "${kind} requires '--backend-root'"
  local backend_root="${2:-}"
  [[ ${3:-} == "--target" ]] || die "${kind} requires '--target'"
  local target="${4:-}"
  [[ $# -eq 4 ]] || die "${kind} expects one backend root and one target"
  [[ "$backend_root" =~ ^/[-A-Za-z0-9_./]+$ ]] || die "backend root is not safe"
  [[ "$target" =~ ^/[-A-Za-z0-9_./]+$ ]] || die "target path is not safe"
  backend_root="${backend_root%/}"
  [[ "$target" == "$backend_root/"* ]] || die "target is outside backend root"
  [[ -e "$target" && ! -L "$target" ]] || die "target must exist and not be a symlink"

  # --only-values bypasses getfattr's requested hex presentation on some
  # versions, yielding an encoded value rather than the GFID bytes. Parse the
  # named hex-form attribute line instead, so the link path is deterministic.
  local gfid_hex="" gfid_line
  while IFS= read -r gfid_line; do
    case "$gfid_line" in
      trusted.gfid=0x*)
        gfid_hex="${gfid_line#trusted.gfid=0x}"
        ;;
    esac
  done < <(getfattr -n trusted.gfid -e hex --absolute-names -- "$target")
  [[ "$gfid_hex" =~ ^[0-9A-Fa-f]{32}$ ]] || die "target trusted.gfid is not a 16-byte value"
  gfid_hex="${gfid_hex,,}"
  local gfid="${gfid_hex:0:8}-${gfid_hex:8:4}-${gfid_hex:12:4}-${gfid_hex:16:4}-${gfid_hex:20:12}"
  local link_path="${backend_root}/.glusterfs/${gfid_hex:0:2}/${gfid_hex:2:2}/${gfid}"
  mkdir -p -- "$(dirname -- "$link_path")"

  case "$kind" in
    link-file-gfid)
      [[ -f "$target" ]] || die "file GFID link requires a regular file"
      if [[ -e "$link_path" ]]; then
        [[ "$target" -ef "$link_path" ]] || rm -f -- "$link_path"
      fi
      [[ -e "$link_path" ]] || ln -- "$target" "$link_path"
      ;;
    link-directory-gfid)
      [[ -d "$target" ]] || die "directory GFID link requires a directory"
      if [[ -L "$link_path" ]]; then
        [[ "$(readlink -- "$link_path")" == "$target" ]] || rm -f -- "$link_path"
      elif [[ -e "$link_path" ]]; then
        rm -f -- "$link_path"
      fi
      [[ -L "$link_path" ]] || ln -s -- "$target" "$link_path"
      ;;
    *) die "unsupported GFID link kind: $kind" ;;
  esac
}

getfacl_cmd() {
  [[ ${1:-} == "-c" ]] || die "getfacl requires '-c --absolute-names'"
  [[ ${2:-} == "--absolute-names" ]] || die "getfacl requires '-c --absolute-names'"
  shift 2
  [[ ${1:-} == "--" ]] && shift
  [[ $# -eq 1 ]] || die "getfacl expects one path"
  exec getfacl -c --absolute-names -- "$1"
}

setfacl_cmd() {
  if [[ ${1:-} == "-k" ]]; then
    shift
    [[ ${1:-} == "--" ]] && shift
    [[ $# -eq 1 ]] || die "setfacl -k expects one path"
    exec setfacl -k -- "$1"
  fi
  [[ ${1:-} == "--set-file=-" ]] || die "setfacl requires '-k' or '--set-file=-'"
  shift
  [[ ${1:-} == "--" ]] && shift
  [[ $# -eq 1 ]] || die "setfacl expects one path"
  exec setfacl --set-file=- -- "$1"
}

rsync_server_cmd() {
  exec rsync "$@"
}

rsync_pull_cmd() {
  [[ $# -eq 3 ]] || die "rsync-pull expects source-host source-path target-path"
  local source_host="$1"
  local source_path="$2"
  local target_path="$3"
  exec rsync -a \
    -e "ssh -i /var/lib/gluster-repair/.ssh/gluster-repair-service -o IdentitiesOnly=yes -o BatchMode=yes -o UserKnownHostsFile=/var/lib/gluster-repair/.ssh/known_hosts -o StrictHostKeyChecking=yes" \
    --rsync-path "sudo -n /opt/gluster-repair/gluster-host-ops.sh rsync-server" \
    "gluster-repair@${source_host}:${source_path}" "$target_path"
}

brick_running_pid() {
  local volume="$1"
  local brick="$2"
  [[ "$volume" =~ ^[-A-Za-z0-9_.]+$ ]] || die "volume name is not safe"
  [[ "$brick" =~ ^/[-A-Za-z0-9_./]+$ ]] || die "brick path is not safe"
  local state_dir="/var/lib/glusterd/vols/${volume}/bricks"
  [[ -d "$state_dir" ]] || die "no Gluster brick state for volume ${volume}"
  local found=false
  local state
  local brick_id
  local pid_file
  local pid
  for state in "$state_dir"/*; do
    [[ -f "$state" ]] || continue
    grep -Fxq -- "path=${brick}" "$state" || continue
    found=true
    brick_id="${state##*/}"
    brick_id="${brick_id/:/}"
    pid_file="/var/run/gluster/vols/${volume}/${brick_id}.pid"
    [[ -s "$pid_file" ]] || continue
    read -r pid < "$pid_file"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
      printf '%s\n' "$pid"
      return 0
    fi
  done
  "$found" || die "brick is not present in Gluster state: ${brick}"
  return 1
}

brick_online_cmd() {
  [[ ${1:-} == "--volume" ]] || die "brick-online requires '--volume'"
  local volume="${2:-}"
  [[ ${3:-} == "--brick" ]] || die "brick-online requires '--brick'"
  local brick="${4:-}"
  local pid
  if pid="$(brick_running_pid "$volume" "$brick")"; then
    printf 'online %s\n' "$pid"
    return 0
  fi
  printf 'offline\n'
}

brick_down_cmd() {
  [[ ${1:-} == "--volume" ]] || die "brick-down requires '--volume'"
  local volume="${2:-}"
  [[ ${3:-} == "--brick" ]] || die "brick-down requires '--brick'"
  local brick="${4:-}"
  local pid
  if pid="$(brick_running_pid "$volume" "$brick")"; then
    kill -TERM "$pid"
  fi
}

brick_kick_cmd() {
  [[ ${1:-} == "--volume" ]] || die "brick-kick requires '--volume'"
  local volume="${2:-}"
  [[ -n "$volume" ]] || die "brick-kick expects a volume name"
  exec gluster volume start "$volume" force
}

[[ $# -gt 0 ]] || die "missing command"
case "$1" in
  mkdir) shift; mkdir_cmd "$@" ;;
  rm) shift; rm_cmd "$@" ;;
  cp) shift; cp_cmd "$@" ;;
  mv) shift; mv_cmd "$@" ;;
  chmod) shift; chmod_cmd "$@" ;;
  chown) shift; chown_cmd "$@" ;;
  setfattr) shift; setfattr_cmd "$@" ;;
  getfattr) shift; getfattr_cmd "$@" ;;
  link-file-gfid|link-directory-gfid) link_gfid_cmd "$1" "${@:2}" ;;
  getfacl) shift; getfacl_cmd "$@" ;;
  setfacl) shift; setfacl_cmd "$@" ;;
  rsync-server) shift; rsync_server_cmd "$@" ;;
  rsync-pull) shift; rsync_pull_cmd "$@" ;;
  brick-online) shift; brick_online_cmd "$@" ;;
  brick-down) shift; brick_down_cmd "$@" ;;
  brick-kick) shift; brick_kick_cmd "$@" ;;
  *) die "unsupported command: $1" ;;
esac
