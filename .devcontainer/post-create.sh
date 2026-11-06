#!/usr/bin/env bash
# Tools for editing/validating the config locally. hadolint, actionlint and trivy run
# as containers via `make` (Docker is provided by the docker-outside-of-docker feature).
set -euo pipefail
sudo apt-get update -qq
sudo apt-get install -y -qq --no-install-recommends haproxy openssl jq curl make shellcheck
# Only the binary is needed (e.g. `haproxy -c`), not the system service.
sudo systemctl disable --now haproxy >/dev/null 2>&1 || true
echo "Ready. Try: make help"
