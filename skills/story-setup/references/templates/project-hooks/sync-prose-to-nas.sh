#!/bin/bash
# sync-prose-to-nas.sh — 把已接纳正文（正文/*.md）转为逐章 TXT + 全本 EPUB3 并镜像同步到 NAS 目录
# 所有个人路径与作者名从 .story/.env.local 读取；未配置 STORY_NAS_ROOT 或未挂载时静默退出 0，绝不阻断写作。
# 手动强制全量同步：bash .story/hooks/sync-prose-to-nas.sh --force

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOOK_ROOT="${STORY_BOOK_ROOT:-${STORY_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}}}"

if [ -f "$BOOK_ROOT/.story/.env.local" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$BOOK_ROOT/.story/.env.local" 2>/dev/null || true
  set +a
fi

NAS_ROOT="${STORY_NAS_ROOT:-}"
[ -n "$NAS_ROOT" ] || exit 0

BOOK_DIR_NAME="${STORY_NAS_BOOK_DIR:-$(basename "$BOOK_ROOT")}"
SRC="$BOOK_ROOT/正文"
[ -d "$SRC" ] || SRC="$BOOK_ROOT/$BOOK_DIR_NAME/正文"
[ -d "$SRC" ] || exit 0

NAS_SUBDIR="${STORY_NAS_PROSE_SUBDIR:-小说/$BOOK_DIR_NAME}"
DEST="$NAS_ROOT/$NAS_SUBDIR"
STAMP="$BOOK_ROOT/.story/.nas-prose-sync.stamp"
FORMAT_VERSION="txt-v1+epub-v2"

PYBIN=""
for candidate in python3 python py; do
  if "$candidate" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)" >/dev/null 2>&1; then
    PYBIN="$candidate"
    break
  fi
done
[ -n "$PYBIN" ] || exit 0

fingerprint=$("$PYBIN" -I - "$SRC" "$FORMAT_VERSION" "${STORY_AUTHOR:-}" <<'PY'
import hashlib, pathlib, sys
src = pathlib.Path(sys.argv[1])
h = hashlib.sha256()
h.update((sys.argv[2] + "\0" + sys.argv[3] + "\n").encode("utf-8"))
for p in sorted(src.glob("*.md")):
    if p.is_file():
        h.update(p.name.encode("utf-8") + b"\0" + hashlib.sha256(p.read_bytes()).digest() + b"\n")
print(h.hexdigest())
PY
) || exit 0

if [ "${1:-}" != "--force" ] && [ -f "$STAMP" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$fingerprint" ]; then
  exit 0
fi

NAS_PARENT="$(dirname "$DEST")"
if [ ! -d "$NAS_ROOT" ] || { [ ! -d "$NAS_PARENT" ] && [ ! -w "$NAS_ROOT" ]; }; then
  echo "[nas-sync] NAS 未挂载（${NAS_ROOT}），正文本轮未同步" >&2
  exit 0
fi

mkdir -p "$DEST" || { echo "[nas-sync] 无法创建 ${DEST}" >&2; exit 0; }

tmp_dir=$(mktemp -d -t story-nas-prose.XXXXXX) || exit 0
trap 'rm -rf "$tmp_dir"' EXIT

if ! "$PYBIN" -I "$SCRIPT_DIR/export_book.py" \
  --project "$BOOK_ROOT" \
  --out-dir "$tmp_dir/book" \
  --formats epub \
  --chapter-txt-dir "$tmp_dir/chapters" \
  --book-title "$BOOK_DIR_NAME" \
  --author "${STORY_AUTHOR:-}" >/dev/null; then
  echo "[nas-sync] 本地导出 TXT/EPUB 失败" >&2
  exit 0
fi

copied=0; removed=0; failed=0
for f in "$tmp_dir/chapters"/*.txt; do
  [ -f "$f" ] || continue
  name="$(basename "$f")"
  if ! cmp -s "$f" "$DEST/$name" 2>/dev/null; then
    # 只写内容流：避免 SMB 卷拒绝扩展属性
    if cat "$f" > "$DEST/$name.tmp" && mv -f "$DEST/$name.tmp" "$DEST/$name"; then
      copied=$((copied + 1))
    else
      failed=$((failed + 1)); rm -f "$DEST/$name.tmp"
    fi
  fi
done

epub_src="$tmp_dir/book/$BOOK_DIR_NAME.epub"
if [ -f "$epub_src" ] && cat "$epub_src" > "$DEST/$BOOK_DIR_NAME.epub.tmp" && mv -f "$DEST/$BOOK_DIR_NAME.epub.tmp" "$DEST/$BOOK_DIR_NAME.epub"; then
  epub="已更新全本 $BOOK_DIR_NAME.epub"
else
  failed=$((failed + 1)); rm -f "$DEST/$BOOK_DIR_NAME.epub.tmp"; epub="全本 EPUB 同步失败"
fi

for g in "$DEST"/*.txt "$DEST"/*.md; do
  [ -f "$g" ] || continue
  base="$(basename "$g")"; base="${base%.*}"
  if [ "${g##*.}" = "md" ] || [ ! -f "$SRC/$base.md" ]; then
    rm -f "$g" && removed=$((removed + 1))
  fi
done

if [ "$failed" -eq 0 ]; then
  mkdir -p "$(dirname "$STAMP")"
  printf '%s\n' "$fingerprint" > "$STAMP"
fi

echo "[nas-sync] 正文已同步到 ${NAS_SUBDIR}：更新 ${copied} 章，移除 ${removed} 个文件，失败 ${failed} 项；${epub}" >&2
exit 0
