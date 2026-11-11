#!/bin/bash
# sync-audiobook-to-nas.sh — 将有声书成品 mp3（有声书/成品/第*章*.mp3）增量上传到 NAS 共享目录
# 所有主机 IP、账号、共享名均从 .story/.env.local 读取；未配置或网络不可达时静默退出 0。
# 规则：只增改不删；仅当同名 .json 清单不早于 .mp3 时才视为合成完毕；手动重传加 --force。

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOOK_ROOT="${STORY_BOOK_ROOT:-${STORY_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}}}"

if [ -f "$BOOK_ROOT/.story/.env.local" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$BOOK_ROOT/.story/.env.local" 2>/dev/null || true
  set +a
fi

BOOK="$(basename "$BOOK_ROOT")"
SRC="$BOOK_ROOT/有声书/成品"
[ -d "$SRC" ] || SRC="$BOOK_ROOT/$BOOK/有声书/成品"
[ -d "$SRC" ] || exit 0

NAS_DIRECT_ROOT="${STORY_AUDIOBOOK_NAS_ROOT:-}"
HOST="${STORY_AUDIOBOOK_NAS_HOST:-}"
NAS_USER="${STORY_AUDIOBOOK_NAS_USER:-}"
SHARE="${STORY_AUDIOBOOK_NAS_SHARE:-}"
MOUNT_POINT="${STORY_AUDIOBOOK_NAS_MOUNT:-${AUDIOBOOK_NAS_MOUNT:-$HOME/.story-nas-audiobook}}"
NAS_DIR="${STORY_AUDIOBOOK_NAS_DIR:-${AUDIOBOOK_NAS_DIR:-$BOOK}}"
STATE="$BOOK_ROOT/.story/.nas-audiobook-sync.stamp"

if [ -z "$NAS_DIRECT_ROOT" ] && [ -z "$SHARE" ]; then
  exit 0
fi

PYBIN=""
for candidate in python3 python py; do
  if "$candidate" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)" >/dev/null 2>&1; then
    PYBIN="$candidate"
    break
  fi
done
[ -n "$PYBIN" ] || exit 0

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

file_size_mtime() {
  "$PYBIN" -I -c 'import os, sys; st = os.stat(sys.argv[1]); print(f"{st.st_size}\t{int(st.st_mtime)}")' "$1"
}

ready=""
for f in "$SRC"/第*章*.mp3; do
  [ -f "$f" ] || continue
  j="${f%.mp3}.json"
  [ -f "$j" ] && [ ! "$j" -ot "$f" ] || continue
  meta="$(file_size_mtime "$f")" || continue
  ready+="$(basename "$f")	$meta"$'\n'
done
[ -n "$ready" ] || exit 0

if [ "$FORCE" = 0 ] && [ -f "$STATE" ] && [ "$(cat "$STATE" 2>/dev/null)" = "$(printf '%s' "$ready")" ]; then
  exit 0
fi

ROOT=""
if [ -n "$NAS_DIRECT_ROOT" ] && [ -d "$NAS_DIRECT_ROOT" ]; then
  ROOT="$NAS_DIRECT_ROOT"
elif [ -n "$SHARE" ]; then
  share_enc=$("$PYBIN" -I -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$SHARE")
  find_mount() {
    mount | awk -v a="/$share_enc on " -v b="/$SHARE on " '
      index($0, a) || index($0, b) { sub(/^[^ ]+ on /, ""); sub(/ \(smbfs.*$/, ""); print; exit }'
  }
  ROOT="$(find_mount)"
  if [ -z "$ROOT" ]; then
    if [ -z "$HOST" ] || [ -z "$NAS_USER" ]; then
      exit 0
    fi
    if command -v nc >/dev/null 2>&1 && ! nc -z -G 2 "$HOST" 445 >/dev/null 2>&1; then
      echo "[audiobook-sync] NAS 连不上（${HOST}），有声书本轮未同步" >&2
      exit 0
    fi
    mkdir -p "$MOUNT_POINT"
    if [ -n "$(ls -A "$MOUNT_POINT" 2>/dev/null)" ]; then
      echo "[audiobook-sync] 挂载点 ${MOUNT_POINT} 非空，跳过自动挂载" >&2
      exit 0
    fi
    if command -v mount_smbfs >/dev/null 2>&1; then
      mount_smbfs -N "//$NAS_USER@$HOST/$share_enc" "$MOUNT_POINT" 2>/dev/null || true
    fi
    ROOT="$(find_mount)"
    if [ -z "$ROOT" ]; then
      echo "[audiobook-sync] 共享「${SHARE}」挂载失败，有声书本轮未同步" >&2
      exit 0
    fi
  fi
fi

[ -n "$ROOT" ] || exit 0
DEST="$ROOT/$NAS_DIR"
mkdir -p "$DEST" || { echo "[audiobook-sync] 无法创建 ${DEST}" >&2; exit 0; }

first_run=0
[ -f "$STATE" ] || first_run=1
new_state=""; uploaded=(); kept=0; failed=0

while IFS=$'\t' read -r name size mtime; do
  [ -n "$name" ] || continue
  line="$name	$size	$mtime"
  n=$(echo "$name" | sed -E 's/^第0*([0-9]+)章.*/\1/')
  target="$DEST/${BOOK}_第$(printf '%02d' "$n")章.mp3"
  if [ "$FORCE" = 0 ] && [ -f "$STATE" ] && grep -qxF "$line" "$STATE"; then
    new_state+="$line"$'\n'; continue
  fi
  if [ "$FORCE" = 0 ] && [ "$first_run" = 1 ] && [ -f "$target" ]; then
    new_state+="$line"$'\n'; kept=$((kept + 1)); continue
  fi
  target_size=""
  if cat "$SRC/$name" > "$target.part" && mv -f "$target.part" "$target"; then
    target_size=$("$PYBIN" -I -c 'import os, sys; print(os.stat(sys.argv[1]).st_size)' "$target" 2>/dev/null || echo "")
  fi
  if [ "$target_size" = "$size" ]; then
    new_state+="$line"$'\n'; uploaded+=("$(basename "$target")")
  else
    failed=$((failed + 1)); rm -f "$target.part"
  fi
done <<< "$ready"

mkdir -p "$(dirname "$STATE")"
printf '%s' "$new_state" > "$STATE"

[ ${#uploaded[@]} -eq 0 ] && [ "$kept" -eq 0 ] && [ "$failed" -eq 0 ] && exit 0
msg="[audiobook-sync] 有声书 → ${NAS_DIR}：上传 ${#uploaded[@]} 章"
if [ ${#uploaded[@]} -gt 0 ]; then
  list=""; for u in "${uploaded[@]}"; do list+="${list:+、}${u}"; done
  msg+="（${list}）"
fi
[ "$kept" -gt 0 ] && msg+="，NAS 已有未覆盖 ${kept} 章"
[ "$failed" -gt 0 ] && msg+="，失败 ${failed} 章（下轮重试）"
echo "$msg" >&2
exit 0
