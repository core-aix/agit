#!/usr/bin/env bash
# Single local gate that mirrors CI (.github/workflows/ci.yml), in the same order:
#   1. ruff lint        2. ruff format --check
#   3. mypy vs the committed baseline (fails only on NEW errors)
#   4. tests + coverage
#
# Definition of "done" for a change: this script exits 0. Run it before you
# commit or push. It is also wired as a pre-push hook (see .pre-commit-config.yaml
# and the Contributing section of the README), so a push that would break CI is
# caught locally first.
#
# Regenerate the mypy baseline after an intentional burndown with:
#   uv run mypy | uv run mypy-baseline sync
set -euo pipefail

cd "$(dirname "$0")/.."

step() { printf '\n==> %s\n' "$*"; }

step "ruff check"
uv run ruff check agit/ tests/

step "ruff format --check"
uv run ruff format --check agit/ tests/

step "mypy (new errors only)"
# mypy exits non-zero because the committed baseline still carries known errors;
# the authoritative result is mypy-baseline filter's exit (non-zero only on NEW
# errors). Turn pipefail off for just this pipeline so the filter governs, exactly
# as CI runs it (its default shell has no pipefail).
set +o pipefail
uv run mypy | uv run mypy-baseline filter || { status=$?; set -o pipefail; exit "$status"; }
set -o pipefail

step "tests + coverage"
uv run coverage run -m pytest -q
uv run coverage report

printf '\nAll checks passed.\n'
