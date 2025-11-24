#!/usr/bin/env bash
set -euo pipefail

###
# Local end-to-end helper for running the stack on a Kind cluster.
#
# What this script does:
#   - Creates a Kind cluster (default name: geth-dev) if it does not exist.
#   - Deploys all Helm charts: geth-node, load-generator, observability.
#   - Prints handy commands to:
#       * Port-forward Geth JSON-RPC and verify 6-second blocks + persistence.
#       * Port-forward Grafana and open the prebuilt dashboard.
#
# Usage:
#   ./scripts/deploy-kind.sh          # create Kind cluster (if needed) + deploy all charts
#   ./scripts/deploy-kind.sh destroy  # uninstall charts and delete Kind cluster
#
# Prerequisites:
#   - kind installed and on PATH
#   - kubectl and helm installed and on PATH
###

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KIND_CLUSTER_NAME="${KIND_CLUSTER_NAME:-geth-dev}"

deploy_stack() {
  echo ">>> Ensuring Kind cluster '${KIND_CLUSTER_NAME}' exists"
  if ! kind get clusters | grep -q "^${KIND_CLUSTER_NAME}\$"; then
    kind create cluster --name "${KIND_CLUSTER_NAME}"
  else
    echo "Kind cluster '${KIND_CLUSTER_NAME}' already exists, reusing it."
  fi

  echo ">>> Deploying Helm charts to Kind (geth-node, load-generator, observability)"
  cd "${ROOT_DIR}"

  # Use the default StorageClass in Kind by leaving storageClass empty.
  helm upgrade --install geth-node ./charts/geth-node \
    --set persistence.storageClass=""

  echo ">>> Pulling load-generator image from public registry and loading into Kind"
  if ! docker pull adamkkk89/geth-workload:latest; then
    echo "ERROR: Failed to pull image 'adamkkk89/geth-workload:latest' from Docker Hub." >&2
    echo "Make sure the image exists and is public, or adjust charts/load-generator/values.yaml." >&2
    exit 1
  fi
  kind load docker-image adamkkk89/geth-workload:latest --name "${KIND_CLUSTER_NAME}"

  helm upgrade --install load-generator ./charts/load-generator
  helm upgrade --install observability ./charts/observability

  echo ">>> Done. Current pods:"
  kubectl get pods -A

  cat <<EOF

Next steps (Kind):

1) Port-forward Geth JSON-RPC and verify 6-second blocks:

   kubectl port-forward -n default svc/geth-node-geth-node 8545:8545

   # In another terminal:
   curl -s -X POST http://localhost:8545 \\
     -H 'Content-Type: application/json' \\
     --data '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}' | jq '.result'
   # wait ~6 seconds, then run again to see the block number increase

2) Check prefunded account balance (should be 100 ETH):

   curl -s -X POST http://localhost:8545 \\
     -H 'Content-Type: application/json' \\
     --data '{"jsonrpc":"2.0","method":"eth_getBalance","params":["0x62358b29b9e3e70ff51D88766e41a339D3e8FFff","latest"],"id":1}' | jq '.result'

3) Port-forward Grafana and open the dashboard:

   kubectl port-forward -n monitoring svc/observability-observability-grafana 3000:3000
   # then browse to: http://localhost:3000 (default creds: admin / admin)

EOF
}

destroy_stack() {
  echo ">>> Uninstalling Helm releases from Kind (observability, load-generator, geth-node)"
  cd "${ROOT_DIR}"
  helm uninstall observability || true
  helm uninstall load-generator || true
  helm uninstall geth-node || true

  echo ">>> Deleting Kind cluster '${KIND_CLUSTER_NAME}'"
  kind delete cluster --name "${KIND_CLUSTER_NAME}" || true
}

MODE="${1:-deploy}"

case "${MODE}" in
  deploy)
    deploy_stack
    ;;
  destroy)
    destroy_stack
    ;;
  *)
    echo "Usage: $0 [deploy|destroy]" >&2
    exit 1
    ;;
esac