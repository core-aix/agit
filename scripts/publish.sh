#!/usr/bin/env bash
# Publish aGiT to PyPI from a clean `main`.
#
# What it does, in order:
#   1. Verify the working tree is on `main`, clean, and in sync with origin/main.
#   2. Run the full local gate (scripts/check.sh) unless --skip-check.
#   3. Compute the next version: one patch (0.0.1) above the higher of the
#      committed version and the latest version already on PyPI — so every
#      publish increments and never collides with an existing release.
#   4. Write that version to pyproject.toml and agit/__init__.py.
#   5. Build the sdist + wheel (uv build) and upload them (uv publish).
#   6. Commit "Release vX.Y.Z", tag it, and push the commit and tag.
#
# Distribution name: `agit-cli` (the plain `agit` name on PyPI belongs to an
# unrelated project). The import package and installed command stay `agit`, so
# users `pip install agit-cli` and then run `agit`.
#
# Authentication: uv publish reads a PyPI API token from $UV_PUBLISH_TOKEN (or
# pass --token to this script). The token's username is implicitly `__token__`.
# The FIRST upload of a brand-new project needs an account-scoped token; once
# the project exists you can switch to a project-scoped token.
#
# Usage:
#   UV_PUBLISH_TOKEN=pypi-... ./scripts/publish.sh
#   ./scripts/publish.sh --token pypi-...        # token on the CLI instead
#   ./scripts/publish.sh --test                  # upload to TestPyPI
#   ./scripts/publish.sh --dry-run               # build only; no upload, no push
#   ./scripts/publish.sh --skip-check            # skip the test/lint/type gate
set -euo pipefail

cd "$(dirname "$0")/.."

DIST_NAME="agit-cli"   # PyPI distribution name (import name stays `agit`)
RELEASE_BRANCH="main"

RUN_CHECK=1
DRY_RUN=0
REPOSITORY="pypi"
TOKEN="${UV_PUBLISH_TOKEN:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --skip-check) RUN_CHECK=0 ;;
    --dry-run) DRY_RUN=1 ;;
    --test) REPOSITORY="testpypi" ;;
    --token) shift; TOKEN="${1:-}" ;;
    -h|--help) sed -n '2,33p' "$0"; exit 0 ;;
    *) echo "error: unknown argument '$1'" >&2; exit 2 ;;
  esac
  shift
done

step() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# --- 1. preconditions --------------------------------------------------------

step "Checking the working tree"
branch="$(git rev-parse --abbrev-ref HEAD)"
[ "$branch" = "$RELEASE_BRANCH" ] || die "must publish from '$RELEASE_BRANCH' (on '$branch')"
[ -z "$(git status --porcelain)" ] || die "working tree is not clean; commit or stash first"

git fetch --quiet origin "$RELEASE_BRANCH"
local_head="$(git rev-parse HEAD)"
remote_head="$(git rev-parse "origin/$RELEASE_BRANCH")"
[ "$local_head" = "$remote_head" ] || die "local '$RELEASE_BRANCH' is not in sync with origin/$RELEASE_BRANCH (pull/push first)"

if [ "$DRY_RUN" = 0 ] && [ "$REPOSITORY" = "pypi" ] && [ -z "$TOKEN" ]; then
  die "no PyPI token: set UV_PUBLISH_TOKEN or pass --token (or use --dry-run / --test)"
fi

# --- 2. quality gate ---------------------------------------------------------

if [ "$RUN_CHECK" = 1 ]; then
  step "Running the full check gate (scripts/check.sh)"
  ./scripts/check.sh
else
  step "Skipping the check gate (--skip-check)"
fi

# --- 3. compute the next version --------------------------------------------

step "Computing the next version"
NEXT_VERSION="$(python3 - "$DIST_NAME" <<'PY'
import json, re, sys, pathlib, urllib.request

dist = sys.argv[1]

def parse(v):
    out = []
    for chunk in v.strip().split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])

local = "0.0.0"
m = re.search(r'^version = "([^"]+)"', pathlib.Path("pyproject.toml").read_text(), re.M)
if m:
    local = m.group(1)

published = "0.0.0"
try:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{dist}/json", timeout=15) as resp:
        published = json.load(resp)["info"]["version"]
except Exception:
    pass  # project not published yet, or offline; fall back to the local version

base = max(parse(local), parse(published))
print("%d.%d.%d" % (base[0], base[1], base[2] + 1))
PY
)"
[ -n "$NEXT_VERSION" ] || die "could not compute the next version"
echo "Next version: $NEXT_VERSION"

if git rev-parse -q --verify "refs/tags/v$NEXT_VERSION" >/dev/null; then
  die "tag v$NEXT_VERSION already exists"
fi

# Restore the version files if we bail out before committing the release.
COMMITTED=0
cleanup() {
  if [ "$COMMITTED" = 0 ]; then
    git checkout -- pyproject.toml agit/__init__.py 2>/dev/null || true
  fi
}
trap cleanup EXIT

# --- 4. write the version ----------------------------------------------------

step "Setting version to $NEXT_VERSION"
python3 - "$NEXT_VERSION" <<'PY'
import re, sys, pathlib

version = sys.argv[1]
pp = pathlib.Path("pyproject.toml")
pp.write_text(re.sub(r'^version = "[^"]+"', f'version = "{version}"', pp.read_text(), count=1, flags=re.M))
ip = pathlib.Path("agit/__init__.py")
ip.write_text(re.sub(r'^__version__ = "[^"]+"', f'__version__ = "{version}"', ip.read_text(), count=1, flags=re.M))
PY

# --- 5. build + upload -------------------------------------------------------

step "Building sdist + wheel"
rm -rf dist
uv build

if [ "$DRY_RUN" = 1 ]; then
  step "Dry run — built artifacts (not uploaded):"
  ls -1 dist
  echo "(version files will be reverted; nothing committed)"
  exit 0
fi

step "Uploading to $REPOSITORY"
publish_args=()
[ -n "$TOKEN" ] && publish_args+=(--token "$TOKEN")
if [ "$REPOSITORY" = "testpypi" ]; then
  publish_args+=(--publish-url "https://test.pypi.org/legacy/")
fi
uv publish ${publish_args[@]+"${publish_args[@]}"} dist/*

# --- 6. record the release ---------------------------------------------------

step "Committing and tagging the release"
git add pyproject.toml agit/__init__.py
git commit -m "Release v$NEXT_VERSION"
git tag -a "v$NEXT_VERSION" -m "Release v$NEXT_VERSION"
COMMITTED=1
git push origin "$RELEASE_BRANCH"
git push origin "v$NEXT_VERSION"

printf '\nPublished %s %s to %s.\n' "$DIST_NAME" "$NEXT_VERSION" "$REPOSITORY"
