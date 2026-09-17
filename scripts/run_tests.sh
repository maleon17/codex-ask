#!/usr/bin/env bash
set -euo pipefail
uv run --no-project --with pytest --with cryptography python -m pytest -q -p no:cacheprovider tests
