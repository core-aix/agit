#!/usr/bin/env bash
# aGiT demo (#53): set up a brand-new repository, let an agent write a small
# program through aGiT, and have aGiT commit every step with the full
# interaction trace and metadata in the commit messages.
#
#   scripts/demo.sh                          # uses claude
#   scripts/demo.sh --backend opencode
#   scripts/demo.sh --backend claude --model haiku
#
# The demo drives aGiT's scripted JSON mode (`agit --prompt ...`), which runs
# each prompt headlessly (`claude -p` / `opencode run`) and commits after each
# one. The demo repository is left behind so you can inspect the history or
# keep going interactively.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: scripts/demo.sh [--backend claude|opencode] [--model MODEL] [--dir PATH]

Options:
  --backend NAME   agent backend to demo: claude (default) or opencode
  --model MODEL    model to use, forwarded to the backend CLI (optional)
  --dir PATH       where to create the demo repository (default: a fresh
                   directory under $TMPDIR)
  -h, --help       show this help
EOF
}

BACKEND="claude"
MODEL=""
DEMO_DIR=""

while [ $# -gt 0 ]; do
    case "$1" in
        --backend) BACKEND="${2:?--backend needs a value}"; shift 2 ;;
        --model) MODEL="${2:?--model needs a value}"; shift 2 ;;
        --dir) DEMO_DIR="${2:?--dir needs a value}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
    esac
done

case "$BACKEND" in
    claude|opencode) ;;
    *) echo "Unknown backend: $BACKEND (use claude or opencode)" >&2; exit 1 ;;
esac

if ! command -v "$BACKEND" >/dev/null 2>&1; then
    echo "Backend '$BACKEND' is not installed (no '$BACKEND' on PATH)." >&2
    exit 1
fi

# Prefer the checkout this script lives in (so the demo always runs the local
# code); fall back to an installed `agit`.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if command -v uv >/dev/null 2>&1 && [ -f "$ROOT/pyproject.toml" ]; then
    AGIT=(uv run --project "$ROOT" agit)
elif command -v agit >/dev/null 2>&1; then
    AGIT=(agit)
else
    echo "Neither uv (to run the checkout) nor an installed 'agit' was found." >&2
    exit 1
fi

if [ -z "$DEMO_DIR" ]; then
    DEMO_DIR="$(mktemp -d "${TMPDIR:-/tmp}/agit-demo.XXXXXX")"
else
    mkdir -p "$DEMO_DIR"
fi

echo "==> Setting up a fresh repository in $DEMO_DIR"
git init -q "$DEMO_DIR"
# Commits need an identity; provide one locally if git has none configured.
git -C "$DEMO_DIR" config user.name >/dev/null 2>&1 || {
    git -C "$DEMO_DIR" config user.name "aGiT Demo"
    git -C "$DEMO_DIR" config user.email "demo@agit.invalid"
}

# Keep the demo self-contained: aGiT's global config (default backend,
# summarizer scratch space) lives in a throwaway directory, not in ~/.agit.
# It must sit OUTSIDE the demo repository or it would show up as untracked
# user changes there.
AGIT_HOME="$(mktemp -d "${TMPDIR:-/tmp}/agit-demo-home.XXXXXX")"
export AGIT_CONFIG_DIR="$AGIT_HOME"
trap 'rm -rf "$AGIT_HOME"' EXIT

# Headless Claude needs permission to edit files; OpenCode's run mode edits by
# default. The flag is passed through aGiT verbatim to the backend CLI.
BACKEND_ARGS=()
if [ "$BACKEND" = "claude" ]; then
    BACKEND_ARGS+=(--permission-mode acceptEdits)
fi
if [ -n "$MODEL" ]; then
    BACKEND_ARGS+=(--model "$MODEL")
fi

PROMPT_1="Create a file fizzbuzz.py with a function fizzbuzz(n) that returns \
'Fizz' for multiples of 3, 'Buzz' for multiples of 5, 'FizzBuzz' for multiples \
of both, and str(n) otherwise. Add a __main__ block that prints fizzbuzz(1) \
through fizzbuzz(20), one per line. Create only this file and do not run any \
shell commands."
PROMPT_2="Create a file test_fizzbuzz.py with unittest test cases for the \
fizzbuzz function in fizzbuzz.py, covering a multiple of 3, a multiple of 5, \
a multiple of 15, and a plain number. Create only this file and do not run \
any shell commands."

echo "==> Running the $BACKEND agent through aGiT (each prompt becomes a commit)"
"${AGIT[@]}" --repo "$DEMO_DIR" --backend "$BACKEND" --new-session \
    --prompt "$PROMPT_1" \
    --prompt "$PROMPT_2" \
    --prompt ":status" \
    ${BACKEND_ARGS[@]+"${BACKEND_ARGS[@]}"}

echo
echo "==> Commit history aGiT created"
git -C "$DEMO_DIR" log --oneline

echo
echo "==> Full message of the latest <aGiT> commit (prompts, trace, metadata)"
git -C "$DEMO_DIR" log -1 --format=%B

if command -v python3 >/dev/null 2>&1 && [ -f "$DEMO_DIR/fizzbuzz.py" ]; then
    echo "==> Running the generated program"
    python3 "$DEMO_DIR/fizzbuzz.py" || echo "(the generated program failed to run)"
    if [ -f "$DEMO_DIR/test_fizzbuzz.py" ]; then
        echo
        echo "==> Running the generated tests"
        (cd "$DEMO_DIR" && python3 -m unittest -v) || echo "(the generated tests failed)"
    fi
fi

echo
echo "Demo repository kept at: $DEMO_DIR"
echo "Explore it (git log shows the full interaction history), or continue"
echo "interactively with:  ${AGIT[*]} --repo $DEMO_DIR"
