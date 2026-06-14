#!/usr/bin/env bash
# Run this once on the deployment host to set up the self-hosted GitHub Actions runner.
# Usage: bash scripts/setup-runner.sh <GITHUB_RUNNER_TOKEN>
# Get the token from: https://github.com/k1n6b0b/WhosAtMyFeeder/settings/actions/runners/new

set -euo pipefail

RUNNER_TOKEN="${1:?Usage: $0 <GITHUB_RUNNER_TOKEN>}"
REPO="https://github.com/k1n6b0b/WhosAtMyFeeder"
RUNNER_DIR="$HOME/actions-runner"
RUNNER_VERSION="2.322.0"
RUNNER_ARCH="linux-x64"
RUNNER_USER="$(whoami)"

mkdir -p "$RUNNER_DIR"
cd "$RUNNER_DIR"

echo "==> Downloading runner..."
curl -sSLo runner.tar.gz \
  "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-${RUNNER_ARCH}-${RUNNER_VERSION}.tar.gz"
tar xzf runner.tar.gz
rm runner.tar.gz

echo "==> Installing dependencies..."
sudo ./bin/installdependencies.sh

echo "==> Installing gitleaks..."
GITLEAKS_VERSION="8.30.0"
curl -sSLo gitleaks.tar.gz \
  "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"
tar xzf gitleaks.tar.gz gitleaks
sudo mv gitleaks /usr/local/bin/gitleaks
rm gitleaks.tar.gz

echo "==> Installing trivy..."
curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | sudo sh -s -- -b /usr/local/bin

echo "==> Configuring runner..."
./config.sh \
  --url "$REPO" \
  --token "$RUNNER_TOKEN" \
  --name "$(hostname)" \
  --labels "self-hosted" \
  --unattended

echo "==> Installing as systemd service..."
sudo ./svc.sh install "$RUNNER_USER"
sudo ./svc.sh start

echo ""
echo "Done. Runner status:"
sudo ./svc.sh status
