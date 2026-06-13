#!/usr/bin/env bash
# Publish aGiT to PyPI and open a release pull request.
#
# What it does, in order:
#   1. Verify the working tree is on `main`, clean, and in sync with origin/main.
#   2. Run the full local gate (scripts/check.sh) unless --skip-check.
#   3. Compute the next version, starting from the higher of the committed
#      version and the latest version already on PyPI (so a publish never
#      collides with an existing release). By default the patch component is
#      bumped (X.Y.Z -> X.Y.Z+1); --minor bumps the minor and resets the patch
#      to 0 (X.Y.Z -> X.Y+1.0); --major bumps the major and resets minor and
#      patch to 0 (X.Y.Z -> X+1.0.0).
#   4. Write that version to pyproject.toml and agit/__init__.py.
#   5. Build the sdist + wheel (uv build) and upload them to PyPI (uv publish).
#   6. Commit "Release vX.Y.Z" on a `release/vX.Y.Z` branch, push it, and open a
#      pull request into main with `gh`.
#
# Branch protection forbids pushing the release commit straight to `main`, so
# the version bump lands through that PR instead of a direct push. The PyPI
# upload happens *before* the PR is opened, so the version is claimed as soon as
# the script runs; the PR only records the matching bump in the repo. The
# version computation takes the max of the local and PyPI versions, so a
# not-yet-merged bump never causes a collision on the next run. Merge the PR
# (then tag — see the printed instructions) to finish the release.
#
# Distribution name: `agit-ai` (the plain `agit` name on PyPI belongs to an
# unrelated project). The import package and installed command stay `agit`, so
# users `pip install agit-ai` and then run `agit`.
#
# Authentication:
#   * PyPI:   uv publish reads a token from $UV_PUBLISH_TOKEN (or pass --token).
#             The token's username is implicitly `__token__`. The FIRST upload
#             of a brand-new project needs an account-scoped token; once the
#             project exists you can switch to a project-scoped token.
#   * GitHub: the PR is created with `gh`, which must be installed and
#             authenticated (`gh auth login`).
#
# Usage:
#   UV_PUBLISH_TOKEN=pypi-... ./scripts/publish.sh
#   ./scripts/publish.sh --token pypi-...        # token on the CLI instead
#   ./scripts/publish.sh --minor                 # bump minor, reset patch to 0
#   ./scripts/publish.sh --major                 # bump major, reset minor+patch
#   ./scripts/publish.sh --test                  # upload to TestPyPI (no PR)
#   ./scripts/publish.sh --dry-run               # build only; no upload, no PR
#   ./scripts/publish.sh --skip-check            # skip the test/lint/type gate
set -euo pipefail

cd "$(dirname "$0")/.."

DIST_NAME="agit-ai"   # PyPI distribution name (import name stays `agit`)
RELEASE_BRANCH="main"

RUN_CHECK=1
DRY_RUN=0
REPOSITORY="pypi"
TOKEN="${UV_PUBLISH_TOKEN:-}"
BUMP="patch"   # which version component to advance: major | minor | patch

while [ $# -gt 0 ]; do
  case "$1" in
    --skip-check) RUN_CHECK=0 ;;
    --dry-run) DRY_RUN=1 ;;
    --test) REPOSITORY="testpypi" ;;
    --token) shift; TOKEN="${1:-}" ;;
    --major) BUMP="major" ;;
    --minor) BUMP="minor" ;;
    --patch) BUMP="patch" ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
    *) echo "error: unknown argument '$1'" >&2; exit 2 ;;
  esac
  shift
done

# Whether this run will open a release PR (only a real PyPI publish does; a dry
# run builds nothing to record, and a --test upload is just a rehearsal).
OPEN_PR=0
if [ "$DRY_RUN" = 0 ] && [ "$REPOSITORY" = "pypi" ]; then
  OPEN_PR=1
fi

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

if [ "$OPEN_PR" = 1 ]; then
  command -v gh >/dev/null 2>&1 || die "the GitHub CLI 'gh' is required to open the release PR (install it, or use --dry-run / --test)"
  gh auth status >/dev/null 2>&1 || die "'gh' is not authenticated; run 'gh auth login'"
fi

# --- 2. quality gate ---------------------------------------------------------

if [ "$RUN_CHECK" = 1 ]; then
  step "Running the full check gate (scripts/check.sh)"
  ./scripts/check.sh
else
  step "Skipping the check gate (--skip-check)"
fi

# --- 3. compute the next version --------------------------------------------

step "Computing the next version ($BUMP bump)"
NEXT_VERSION="$(python3 - "$DIST_NAME" "$BUMP" <<'PY'
import json, re, sys, pathlib, urllib.request

dist = sys.argv[1]
level = sys.argv[2]

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

maj, minr, pat = max(parse(local), parse(published))
if level == "major":
    maj, minr, pat = maj + 1, 0, 0
elif level == "minor":
    minr, pat = minr + 1, 0
else:  # patch
    pat += 1
print("%d.%d.%d" % (maj, minr, pat))
PY
)"
[ -n "$NEXT_VERSION" ] || die "could not compute the next version"
echo "Next version: $NEXT_VERSION"

RELEASE_PR_BRANCH="release/v$NEXT_VERSION"

if git rev-parse -q --verify "refs/tags/v$NEXT_VERSION" >/dev/null; then
  die "tag v$NEXT_VERSION already exists"
fi
if [ "$OPEN_PR" = 1 ]; then
  if git rev-parse -q --verify "refs/heads/$RELEASE_PR_BRANCH" >/dev/null; then
    die "branch $RELEASE_PR_BRANCH already exists locally (delete it or bump past this version)"
  fi
  if git ls-remote --exit-code --heads origin "$RELEASE_PR_BRANCH" >/dev/null 2>&1; then
    die "branch $RELEASE_PR_BRANCH already exists on origin (a release PR may be open already)"
  fi
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
  echo "(version files will be reverted; nothing committed or pushed)"
  exit 0
fi

step "Uploading to $REPOSITORY"
publish_args=()
[ -n "$TOKEN" ] && publish_args+=(--token "$TOKEN")
if [ "$REPOSITORY" = "testpypi" ]; then
  publish_args+=(--publish-url "https://test.pypi.org/legacy/")
fi
uv publish ${publish_args[@]+"${publish_args[@]}"} dist/*

if [ "$OPEN_PR" = 0 ]; then
  step "Uploaded to $REPOSITORY (rehearsal) — not opening a release PR"
  echo "(version files will be reverted; nothing committed or pushed)"
  exit 0
fi

# --- 6. open the release pull request ---------------------------------------
#
# Branch protection blocks pushing the bump straight to main, so we commit it on
# a short-lived release branch and open a PR. The package is already on PyPI at
# this point; merging the PR just records the matching version in the repo.

step "Opening a release pull request"
git switch -c "$RELEASE_PR_BRANCH"
git add pyproject.toml agit/__init__.py
git commit -m "Release v$NEXT_VERSION"
COMMITTED=1  # changes are committed on the release branch; nothing to restore
git push -u origin "$RELEASE_PR_BRANCH"

pr_body="$(cat <<EOF
Release $DIST_NAME v$NEXT_VERSION.

Published to PyPI by scripts/publish.sh; this PR records the matching version
bump in pyproject.toml and agit/__init__.py. After merging, tag the release:

    git switch $RELEASE_BRANCH && git pull
    git tag -a v$NEXT_VERSION -m "Release v$NEXT_VERSION"
    git push origin v$NEXT_VERSION
EOF
)"

pr_url="$(gh pr create \
  --base "$RELEASE_BRANCH" \
  --head "$RELEASE_PR_BRANCH" \
  --title "Release v$NEXT_VERSION" \
  --body "$pr_body")"

# Leave the user back on main; the release branch lives on until the PR merges.
git switch "$RELEASE_BRANCH" >/dev/null 2>&1 || true

printf '\nPublished %s %s to %s.\n' "$DIST_NAME" "$NEXT_VERSION" "$REPOSITORY"
printf 'Opened release PR: %s\n' "$pr_url"
printf '\nAfter the PR is merged, tag the release:\n'
printf '    git switch %s && git pull\n' "$RELEASE_BRANCH"
printf '    git tag -a v%s -m "Release v%s"\n' "$NEXT_VERSION" "$NEXT_VERSION"
printf '    git push origin v%s\n' "$NEXT_VERSION"
