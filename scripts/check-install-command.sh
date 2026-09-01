#!/usr/bin/env bash
# check-install-command.sh — run the install command we actually publish.
#
# Every other install check in this repo feeds `skills add` a local path, so
# none of them exercise the command users are told to run. That gap shipped a
# README command the CLI rejects outright (`Archive links are not supported`)
# and nothing caught it. This check closes that gap by extracting the canonical
# block from README.md and executing it, so the command cannot drift from the
# documentation without turning the gate red.
#
# Two modes:
#   (default)  offline — assert the published command has the shape that works
#              with the current `skills` CLI, then prove that shape installs by
#              running it against a locally built package.
#   --live     additionally run the README block verbatim against the published
#              release. Needs network and an existing release, so it is opt-in
#              (scheduled / manual runs), never a local prerequisite.
#              Note what this does and does not prove: the block points at
#              releases/latest, so a live run validates the PREVIOUS release,
#              never the candidate being built. It catches the failure mode we
#              actually hit — the CLI changing under a command we had stopped
#              exercising — not a defect in the candidate's own artifact.

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
echo "  OK extracted canonical install block from README.md"

# The CLI takes owner/repo, a repository URL, or a local path. Handing it an
# archive link fails; the block must therefore download and unpack first.
if printf '%s' "$BLOCK" | grep -Eq 'skills add[^|&]*https?://'; then
  echo "FAIL: the documented command passes a URL straight to 'skills add'."
  echo "      The CLI rejects archive links; download and unpack first."
  exit 1
fi
for needle in 'oh-story-release.zip' 'SHA256SUMS' 'unzip' 'skills add'; do
  printf '%s' "$BLOCK" | grep -q -- "$needle" \
    || { echo "FAIL: canonical block is missing '$needle'"; exit 1; }
done
echo "  OK block downloads, checksums, unpacks, then installs from a local path"

# The agent-facing skills carry their own copy of the command (an installed
# skill cannot read README.md), so the same mistake can reappear there. Reject
# the broken form anywhere in the tree, not just in the canonical block.
OFFENDERS="$(git -C "$REPO_ROOT" grep -nE 'skills add[^|&`]*https?://' \
  -- ':!CHANGELOG.md' ':!scripts/check-install-command.sh' 2>/dev/null || true)"
if [ -n "$OFFENDERS" ]; then
  echo "FAIL: these hand an archive/repo URL straight to 'skills add':"
  printf '%s\n' "$OFFENDERS" | sed 's/^/      /'
  echo "      The CLI rejects archive links; download and unpack first."
  exit 1
fi
echo "  OK no file passes a URL directly to 'skills add'"

# Those copies must also stay complete: a copy that downloads the asset but
# skips the checksum still installs something, just unverified.
INCOMPLETE=""
while IFS= read -r f; do
  [ -n "$f" ] || continue
  # Prose instructions count too, as long as they still tell the reader to
  # verify a checksum and unpack before installing.
  grep -q 'SHA256' "$REPO_ROOT/$f" \
    && grep -qE 'unzip|Expand-Archive|解包' "$REPO_ROOT/$f" \
    || INCOMPLETE="$INCOMPLETE $f"
done <<EOF
$(git -C "$REPO_ROOT" grep -lE 'skills add' -- ':!CHANGELOG.md' ':!scripts/*' ':!*.yml' 2>/dev/null || true)
EOF
if [ -n "$INCOMPLETE" ]; then
  echo "FAIL: these document 'skills add' without a checksum + unpack step:$INCOMPLETE"
  exit 1
fi
echo "  OK every documented install copy verifies a checksum and unpacks"

# Prove that shape actually installs, using a package built from this tree so
# the check works offline and before the matching release exists.
command -v npx >/dev/null 2>&1 || { echo "SKIP: npx unavailable"; exit 0; }
command -v unzip >/dev/null 2>&1 || { echo "SKIP: unzip unavailable"; exit 0; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
python3 "$REPO_ROOT/scripts/build-package.py" dev --output-dir "$STAGE/dist" >/dev/null
ZIP="$(find "$STAGE/dist" -maxdepth 1 -name 'oh-story-*.zip' -print -quit)"
[ -n "$ZIP" ] || { echo "FAIL: no package built"; exit 1; }

cp "$ZIP" "$STAGE/oh-story-release.zip"
( cd "$STAGE" && shasum -a 256 oh-story-release.zip > SHA256SUMS \
  && shasum -a 256 --ignore-missing -c SHA256SUMS >/dev/null )
unzip -q "$STAGE/oh-story-release.zip" -d "$STAGE/x"
mkdir -p "$STAGE/project"
( cd "$STAGE/project" && npx --yes skills@1.5.22 add "$STAGE"/x/oh-story-* -y >/dev/null 2>&1 )

COUNT="$(find "$STAGE/project/.agents/skills" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')"
EXPECTED="$(find "$REPO_ROOT/skills" -maxdepth 2 -name SKILL.md | wc -l | tr -d ' ')"
[ "$COUNT" = "$EXPECTED" ] \
  || { echo "FAIL: documented install shape produced $COUNT skills, expected $EXPECTED"; exit 1; }
echo "  OK documented shape installs all $COUNT skills from an unpacked archive"

if [ "$LIVE" = "1" ]; then
  echo "  Running the README block verbatim against the published release"
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
