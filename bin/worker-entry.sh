#!/usr/bin/env bash
# Thin wrapper. All logic lives in control/worker_entry.py.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "${REPO_ROOT}/control/worker_entry.py" "$1"
