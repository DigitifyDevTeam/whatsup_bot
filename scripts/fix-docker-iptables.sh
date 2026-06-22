#!/usr/bin/env bash
# Repairs missing Docker iptables chains (DOCKER-FORWARD / "No chain/target/match by that name").
# Run on the server: bash scripts/fix-docker-iptables.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Need root to fix iptables — re-running with sudo..."
  exec sudo bash "$0" "$@"
fi

echo "=== Step 1: kernel bridge netfilter ==="
modprobe br_netfilter 2>/dev/null || true
sysctl -w net.bridge.bridge-nf-call-iptables=1 2>/dev/null || true

echo "=== Step 2: iptables backend (legacy fixes most Docker hosts) ==="
if command -v update-alternatives >/dev/null && [ -x /usr/sbin/iptables-legacy ]; then
  update-alternatives --set iptables /usr/sbin/iptables-legacy || true
  update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy || true
fi

echo "=== Step 3: prune stale Docker networks ==="
docker network prune -f || true

echo "=== Step 4: restart Docker (recreates DOCKER / DOCKER-FORWARD chains) ==="
systemctl restart docker
sleep 5

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker failed to start. Check: journalctl -u docker -n 50 --no-pager"
  exit 1
fi

echo ""
echo "Docker iptables repair complete."
echo "Start the bot stack:"
echo "  cd ~/public_html/whatsup_bot && docker compose up -d --build"
