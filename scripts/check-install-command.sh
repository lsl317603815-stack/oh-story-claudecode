#!/usr/bin/env bash
# check-install-command.sh — run the install command we actually publish.
#
# Every other install check in this repo feeds `skills add` a local path, so
# none of them exercise the command users are told to run. That gap shipped a
# README command that failed outright for every user, and nothing caught it.
#
# The failure was not the command: the `skills` CLI does support release-asset
# URLs. It refuses an archive that contains a symbolic link — its error,
# "Archive links are not supported", is about links *inside* the archive. Our
# package carried one (`.agents/skills -> ../skills`, a repo-local discovery
# path for Codex and Reasonix), so every published archive was unusable.
#
# So this checks two things: the built archive contains no symlink entry, and
# the command printed in README actually installs from it.
#
# Two modes:
#   (default)  offline — build a package from this tree and run the documented
#              command shape against it.
#   --live     additionally run the README command verbatim against the
#              published release. Needs network and an existing release, so it
#              is scheduled/manual only. Note what it proves: the block points
#              at releases/latest, so it validates the PREVIOUS release, never
#              the candidate being built. It catches the drift we actually hit.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
README="$REPO_ROOT/README.md"
LIVE=0
[ "${1:-}" = "--live" ] && LIVE=1

echo "Install command check"
echo "====================="

BLOCK="$(python3 - "$README" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
m = re.search(
    r"<!-- canonical-install:begin -->\s*```bash\n(.*?)```\s*<!-- canonical-install:end -->",
    text,
    re.S,
)
if not m:
    raise SystemExit("README.md: canonical-install block not found")
sys.stdout.write(m.group(1))
PY
)"
[ -n "$BLOCK" ] || { echo "FAIL: empty canonical install block"; exit 1; }
printf '%s' "$BLOCK" | grep -q 'skills add' \
  || { echo "FAIL: canonical block does not install anything"; exit 1; }
echo "  OK extracted canonical install block from README.md"

command -v npx >/dev/null 2>&1 || { echo "SKIP: npx unavailable"; exit 0; }
command -v unzip >/dev/null 2>&1 || { echo "SKIP: unzip unavailable"; exit 0; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
python3 "$REPO_ROOT/scripts/build-package.py" dev --output-dir "$STAGE/dist" >/dev/null
ZIP="$(find "$STAGE/dist" -maxdepth 1 -name 'oh-story-*.zip' -print -quit)"
[ -n "$ZIP" ] || { echo "FAIL: no package built"; exit 1; }

# The invariant that broke installs for every user of v0.7.6 through v0.10.0.
LINKS="$(unzip -Z "$ZIP" 2>/dev/null | grep -cE '^l' || true)"
if [ "${LINKS:-0}" -ne 0 ]; then
  echo "FAIL: the archive contains $LINKS symlink entry/entries:"
  unzip -Z "$ZIP" 2>/dev/null | grep -E '^l' | sed 's/^/      /'
  echo "      The skills CLI rejects any archive holding a symlink"
  echo "      ('Archive links are not supported'), so installing from the"
  echo "      published asset URL would fail for every user."
  exit 1
fi
echo "  OK built archive contains no symlink entries"

# Prove the documented command shape installs, using the tree's own package so
# this works offline and before the matching release exists.
unzip -q "$ZIP" -d "$STAGE/x"
mkdir -p "$STAGE/project"
( cd "$STAGE/project" && npx --yes skills@1.5.22 add "$STAGE"/x/oh-story-* -y >/dev/null 2>&1 )
COUNT="$(find "$STAGE/project/.agents/skills" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')"
EXPECTED="$(find "$REPO_ROOT/skills" -maxdepth 2 -name SKILL.md | wc -l | tr -d ' ')"
[ "$COUNT" = "$EXPECTED" ] \
  || { echo "FAIL: package installed $COUNT skills, expected $EXPECTED"; exit 1; }
echo "  OK package installs all $COUNT skills"

if [ "$LIVE" = "1" ]; then
  echo "  Running the README command verbatim against the published release"
  LIVE_DIR="$(mktemp -d)"
  ( cd "$LIVE_DIR" && bash -euo pipefail -c "${BLOCK//-y -g/-y}" >/dev/null 2>&1 ) \
    || { echo "FAIL: the published install command failed against the live release"; exit 1; }
  LIVE_COUNT="$(find "$LIVE_DIR/.agents/skills" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')"
  [ "${LIVE_COUNT:-0}" -gt 0 ] \
    || { echo "FAIL: live install produced no skills"; exit 1; }
  echo "  OK live install from the published release produced $LIVE_COUNT skills"
  rm -rf "$LIVE_DIR"
fi

echo
echo "OK: install command check passed"
