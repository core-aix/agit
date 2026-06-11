.PHONY: check install-hooks

# Run the full local gate (mirrors CI): ruff, mypy, tests + coverage.
check:
	./scripts/check.sh

# Install the opt-in git hooks: ruff/format on commit, full check on push.
install-hooks:
	uv run pre-commit install
	uv run pre-commit install --hook-type pre-push
