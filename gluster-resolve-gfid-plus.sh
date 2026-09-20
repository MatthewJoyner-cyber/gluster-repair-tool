#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail

script_name=$(basename "$0")
usage() {
  cat <<USAGE
Resolve a Gluster GFID, heal entry, or plain heal path to a backend path.

Usage:
  $script_name -v VOLUME -g GFID [options]
  $script_name -v VOLUME -e '<gfid:...>/child' [options]
  $script_name -v VOLUME -e '/plain/path/from/volume/root' [options]

Options:
  -v VOLUME        Gluster volume name, e.g. gtest
  -g GFID          GFID to resolve
  -e ENTRY         Heal entry, e.g. '<gfid:UUID>' or '<gfid:UUID>/name' or '/path'
  -b BRICK_PATH    Explicit local brick path override
  -m MOUNTPOINT    Optional Gluster client mountpoint for mounted path output
  -s               Also stat the resolved backend path and mounted path
  -V               Verbose resolver tracing to stderr
  -k               Print machine-readable KEY=VALUE output
  -x               Enable shell xtrace (set -x)
  -h               Show this help
USAGE
}

VOL=""
GFID=""
ENTRY=""
BRICK_PATH=""
MOUNTPOINT=""
DO_STAT=0
VERBOSE=0
KEYMODE=0
TRACE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    -v) VOL="$2"; shift 2 ;;
    -g) GFID="$2"; shift 2 ;;
    -e) ENTRY="$2"; shift 2 ;;
    -b) BRICK_PATH="$2"; shift 2 ;;
    -m) MOUNTPOINT="$2"; shift 2 ;;
    -s) DO_STAT=1; shift ;;
    -V) VERBOSE=1; shift ;;
    -k) KEYMODE=1; shift ;;
    -x) TRACE=1; shift ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ $TRACE -eq 1 ]]; then
  export PS4='+ ${BASH_SOURCE##*/}:${LINENO}: '
  set -x
fi

vlog(){ [[ $VERBOSE -eq 1 ]] && echo "[resolver] $*" >&2 || true; }

[[ -n "$VOL" ]] || { echo "Error: must supply -v" >&2; exit 1; }
[[ -n "$GFID" || -n "$ENTRY" ]] || { echo "Error: must supply -g or -e" >&2; exit 1; }

gluster v list | grep -qx "$VOL" || { echo "Error: volume $VOL not found on this server." >&2; exit 1; }

if [[ -z "$BRICK_PATH" ]]; then
  host_short=$(hostname -s)
  host_full=$(hostname)
  BRICK_PATH=$(gluster v info "$VOL" | awk -F: -v h1="$host_short" -v h2="$host_full" '$1 ~ /^Brick[0-9]+/ && ($2==h1 || $2==h2) {print $NF; exit}') || true
  [[ -n "$BRICK_PATH" ]] || { echo "Error: unable to find local brick for $VOL" >&2; exit 1; }
fi

if [[ -z "$MOUNTPOINT" ]]; then
  MOUNTPOINT=$(df | awk -v v="/$VOL" '$1 ~ (":" v "$") {print $NF; exit}') || true
fi

CHILD_REL=""
PLAIN_PATH=""
entry_re='^<gfid:([0-9a-f-]+)>(/(.*))?$'
if [[ -n "$ENTRY" ]]; then
  if [[ "$ENTRY" =~ $entry_re ]]; then
    GFID="${BASH_REMATCH[1]}"
    CHILD_REL="${BASH_REMATCH[3]:-}"
  elif [[ "$ENTRY" == /* ]]; then
    PLAIN_PATH="$ENTRY"
  else
    echo "ERROR: invalid heal entry format: $ENTRY" >&2
    exit 1
  fi
fi

normalize_path(){ sed 's#//*#/#g' <<< "$1"; }

resolve_gfid_paths() {
  local g="$1"
  local p1="${g:0:2}" p2="${g:2:2}"
  local parent="$BRICK_PATH/.glusterfs/$p1/$p2"
  local path="$parent/$g"
  vlog "resolveGFIDPath gfid=$g parent=$parent path=$path"
  printf '%s\t%s' "$parent" "$path"
}

path_type() {
  local path="$1"
  if [[ -L "$path" ]]; then
    printf 'symlink'
  elif [[ -d "$path" ]]; then
    printf 'dir'
  elif [[ -f "$path" ]]; then
    printf 'file'
  elif [[ -e "$path" ]]; then
    printf 'other'
  fi
}

trusted_gfid_from_backend() {
  local backend="$1" hex
  [[ -e "$backend" ]] || return 1
  hex=$(getfattr -d -m . -e hex -- "$backend" 2>/dev/null | awk -F= '/trusted.gfid=0x/ {print $2; exit}') || true
  [[ -n "$hex" ]] || return 1
  hex=${hex#0x}
  [[ ${#hex} -ge 32 ]] || return 1
  printf '%s-%s-%s-%s-%s' "${hex:0:8}" "${hex:8:4}" "${hex:12:4}" "${hex:16:4}" "${hex:20:12}"
}

resolve_gfid_backend() {
  local g="$1"
  local pair parent gpath dirPath dirBase gfid2path parent_gfid filename parent_dir
  pair=$(resolve_gfid_paths "$g")
  parent=${pair%%$'\t'*}
  gpath=${pair#*$'\t'}
  [[ -e "$gpath" || -L "$gpath" ]] || return 1

  if [[ -L "$gpath" ]]; then
    gfid2path=$(getfattr -h -d -m . -e text "$gpath" 2>/dev/null | tr -d '"' | awk -F= '/trusted.gfid2path\./ {print $2; exit}') || true
    if [[ -n "$gfid2path" ]]; then
      parent_gfid=${gfid2path%%/*}
      filename=${gfid2path#*/}
      [[ -n "$parent_gfid" && -n "$filename" && "$parent_gfid" != "$gfid2path" ]] || return 1
      parent_dir=$(resolve_gfid_backend "$parent_gfid")
      [[ -n "$parent_dir" ]] || return 1
      printf '%s/%s' "$parent_dir" "$filename"
      return 0
    fi
    dirPath="$parent/$(readlink "$gpath")"
    vlog "dir gfid $g readlink=$(readlink "$gpath")"
    dirBase="$(readlink -f "$(dirname "$dirPath")")/$(basename "$dirPath")"
    vlog "dir gfid $g dirPath=$dirPath dirBase=$dirBase"
    printf '%s' "$(normalize_path "$dirBase")"
    return 0
  fi

  gfid2path=$(getfattr -d -m . -e text "$gpath" 2>/dev/null | tr -d '"' | awk -F= '/trusted.gfid2path\./ {print $2; exit}') || true
  [[ -n "$gfid2path" ]] || return 1
  parent_gfid=${gfid2path%%/*}
  filename=${gfid2path#*/}
  [[ -n "$parent_gfid" && -n "$filename" && "$parent_gfid" != "$gfid2path" ]] || return 1
  parent_dir=$(resolve_gfid_backend "$parent_gfid")
  printf '%s/%s' "$parent_dir" "$filename"
}

file_gfid_from_backend() {
  local backend="$1" uuid p1 p2
  [[ -e "$backend" ]] || return 1
  uuid=$(trusted_gfid_from_backend "$backend") || return 1
  p1="${uuid:0:2}"; p2="${uuid:2:2}"
  printf '%s\t%s' "$uuid" "$BRICK_PATH/.glusterfs/$p1/$p2/$uuid"
}

backend_gfid_path_from_backend() {
  local backend="$1" uuid p1 p2
  [[ -e "$backend" || -L "$backend" ]] || return 1
  uuid=$(trusted_gfid_from_backend "$backend") || return 1
  p1="${uuid:0:2}"; p2="${uuid:2:2}"
  printf '%s\t%s' "$uuid" "$BRICK_PATH/.glusterfs/$p1/$p2/$uuid"
}

GFID_PATH=""
BACKEND=""
TYPE="unknown"
GFID_EXISTS=0
FILE_GFID=""
FILE_GFID_PATH=""
RELPATH=""
DEPTH=""
MOUNTED=""
CANONICAL_GFID_PATH=""
BACKEND_LEXISTS=0
BACKEND_EXISTS=0
BACKEND_LSTAT_TYPE=""
BACKEND_TRUSTED_GFID=""
BACKEND_GFID_PATH=""
BACKEND_GFID_PATH_CHECKED=0
BACKEND_GFID_PATH_LEXISTS=0
BACKEND_GFID_PATH_EXISTS=0
BACKEND_GFID_PATH_LSTAT_TYPE=""
GFID_PATH_LEXISTS=0
GFID_PATH_EXISTS=0
GFID_PATH_LSTAT_TYPE=""

if [[ -n "$PLAIN_PATH" ]]; then
  BACKEND=$(normalize_path "$BRICK_PATH$PLAIN_PATH")
  [[ -e "$BACKEND" ]] && GFID_EXISTS=1 || GFID_EXISTS=0
  if [[ -d "$BACKEND" ]]; then TYPE="dir"; elif [[ -f "$BACKEND" ]]; then TYPE="file"; elif [[ -e "$BACKEND" ]]; then TYPE="other"; else TYPE="missing"; fi
else
  pair=$(resolve_gfid_paths "$GFID")
  GFID_PATH=${pair#*$'\t'}
  CANONICAL_GFID_PATH="$GFID_PATH"
  if [[ -e "$GFID_PATH" || -L "$GFID_PATH" ]]; then GFID_EXISTS=1; fi
  if backend=$(resolve_gfid_backend "$GFID"); then
    BACKEND="$backend"
    [[ -n "$CHILD_REL" ]] && BACKEND="$BACKEND/$CHILD_REL"
    BACKEND=$(normalize_path "$BACKEND")
    if [[ -e "$BACKEND" ]]; then
      if [[ -d "$BACKEND" ]]; then TYPE="dir"; elif [[ -f "$BACKEND" ]]; then TYPE="file"; else TYPE="other"; fi
    elif [[ -L "$GFID_PATH" ]]; then
      TYPE="dirgfid"
    else
      TYPE="missing"
    fi
  fi
  if [[ -z "$CHILD_REL" && -n "$BACKEND" && -e "$BACKEND" ]]; then
    for bucket in xattrop dirty; do
      index_path="$BRICK_PATH/.glusterfs/indices/$bucket/$GFID"
      if [[ -e "$index_path" || -L "$index_path" ]]; then
        GFID_PATH="$index_path"
        break
      fi
    done
  fi
fi

if [[ -n "$BACKEND" ]]; then
  [[ -e "$BACKEND" || -L "$BACKEND" ]] && BACKEND_LEXISTS=1 || BACKEND_LEXISTS=0
  [[ -e "$BACKEND" ]] && BACKEND_EXISTS=1 || BACKEND_EXISTS=0
  BACKEND_LSTAT_TYPE=$(path_type "$BACKEND")
  BACKEND_TRUSTED_GFID=$(trusted_gfid_from_backend "$BACKEND" || true)
  if bgpair=$(backend_gfid_path_from_backend "$BACKEND"); then
    BACKEND_GFID_PATH=${bgpair#*$'\t'}
    BACKEND_GFID_PATH_CHECKED=1
    [[ -e "$BACKEND_GFID_PATH" || -L "$BACKEND_GFID_PATH" ]] && BACKEND_GFID_PATH_LEXISTS=1 || BACKEND_GFID_PATH_LEXISTS=0
    [[ -e "$BACKEND_GFID_PATH" ]] && BACKEND_GFID_PATH_EXISTS=1 || BACKEND_GFID_PATH_EXISTS=0
    BACKEND_GFID_PATH_LSTAT_TYPE=$(path_type "$BACKEND_GFID_PATH")
  fi
fi

if [[ -n "$GFID_PATH" ]]; then
  [[ -e "$GFID_PATH" || -L "$GFID_PATH" ]] && GFID_PATH_LEXISTS=1 || GFID_PATH_LEXISTS=0
  [[ -e "$GFID_PATH" ]] && GFID_PATH_EXISTS=1 || GFID_PATH_EXISTS=0
  GFID_PATH_LSTAT_TYPE=$(path_type "$GFID_PATH")
fi

if [[ -n "$BACKEND" && -f "$BACKEND" ]]; then
  fgpair=""
  if fgpair=$(file_gfid_from_backend "$BACKEND"); then
    FILE_GFID=${fgpair%%$'\t'*}
    FILE_GFID_PATH=${fgpair#*$'\t'}
  fi
fi

if [[ -n "$BACKEND" ]]; then
  rel="${BACKEND#$BRICK_PATH/}"
  if [[ "$rel" != "$BACKEND" ]]; then
    RELPATH="$rel"
    DEPTH=$(awk -F/ '{print NF}' <<< "$rel")
    [[ -n "$MOUNTPOINT" ]] && MOUNTED="$MOUNTPOINT/$rel"
  fi
fi

if [[ $KEYMODE -eq 1 ]]; then
  printf 'GFID=%s\n' "$GFID"
  printf 'CHILD_REL=%s\n' "$CHILD_REL"
  printf 'GFID_PATH=%s\n' "$GFID_PATH"
  printf 'CANONICAL_GFID_PATH=%s\n' "$CANONICAL_GFID_PATH"
  printf 'BACKEND=%s\n' "$BACKEND"
  printf 'TYPE=%s\n' "$TYPE"
  printf 'GFID_EXISTS=%s\n' "$GFID_EXISTS"
  printf 'BACKEND_LEXISTS=%s\n' "$BACKEND_LEXISTS"
  printf 'BACKEND_EXISTS=%s\n' "$BACKEND_EXISTS"
  printf 'BACKEND_LSTAT_TYPE=%s\n' "$BACKEND_LSTAT_TYPE"
  printf 'BACKEND_TRUSTED_GFID=%s\n' "$BACKEND_TRUSTED_GFID"
  printf 'BACKEND_GFID_PATH=%s\n' "$BACKEND_GFID_PATH"
  printf 'BACKEND_GFID_PATH_CHECKED=%s\n' "$BACKEND_GFID_PATH_CHECKED"
  printf 'BACKEND_GFID_PATH_LEXISTS=%s\n' "$BACKEND_GFID_PATH_LEXISTS"
  printf 'BACKEND_GFID_PATH_EXISTS=%s\n' "$BACKEND_GFID_PATH_EXISTS"
  printf 'BACKEND_GFID_PATH_LSTAT_TYPE=%s\n' "$BACKEND_GFID_PATH_LSTAT_TYPE"
  printf 'GFID_PATH_LEXISTS=%s\n' "$GFID_PATH_LEXISTS"
  printf 'GFID_PATH_EXISTS=%s\n' "$GFID_PATH_EXISTS"
  printf 'GFID_PATH_LSTAT_TYPE=%s\n' "$GFID_PATH_LSTAT_TYPE"
  printf 'FILE_GFID=%s\n' "$FILE_GFID"
  printf 'FILE_GFID_PATH=%s\n' "$FILE_GFID_PATH"
  printf 'RELPATH=%s\n' "$RELPATH"
  printf 'MOUNTED=%s\n' "$MOUNTED"
  printf 'DEPTH=%s\n' "$DEPTH"
else
  echo "GFID=        $GFID"
  echo "CHILD_REL=   $CHILD_REL"
  echo "GFID_PATH=   $GFID_PATH"
  echo "CANONICAL_GFID_PATH=$CANONICAL_GFID_PATH"
  echo "BACKEND=     $BACKEND"
  echo "TYPE=        $TYPE"
  echo "GFID_EXISTS= $GFID_EXISTS"
  echo "BACKEND_LEXISTS=$BACKEND_LEXISTS"
  echo "BACKEND_EXISTS=$BACKEND_EXISTS"
  echo "BACKEND_LSTAT_TYPE=$BACKEND_LSTAT_TYPE"
  echo "BACKEND_TRUSTED_GFID=$BACKEND_TRUSTED_GFID"
  echo "GFID_PATH_LEXISTS=$GFID_PATH_LEXISTS"
  echo "GFID_PATH_EXISTS=$GFID_PATH_EXISTS"
  echo "GFID_PATH_LSTAT_TYPE=$GFID_PATH_LSTAT_TYPE"
  echo "FILE_GFID=   $FILE_GFID"
  echo "FILE_GFID_PATH=$FILE_GFID_PATH"
  echo "RELPATH=     $RELPATH"
  echo "MOUNTED=     $MOUNTED"
  echo "DEPTH=       $DEPTH"
fi

if [[ $DO_STAT -eq 1 ]]; then
  [[ -n "$BACKEND" && -e "$BACKEND" ]] && { echo "backend stat:"; stat -- "$BACKEND"; }
  [[ -n "$MOUNTED" && -e "$MOUNTED" ]] && { echo "mounted stat:"; stat -- "$MOUNTED"; }
fi
