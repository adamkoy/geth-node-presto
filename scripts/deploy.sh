#!/usr/bin/env bash
set -euo pipefail

###
# Local end-to-end helper for the dev stack.
#
# Usage:
#   ./scripts/deploy.sh          # deploy/upgrade infra + all charts
#   ./scripts/deploy.sh destroy  # uninstall charts and destroy infra
#
# Prerequisites:
#   - AWS credentials configured for the target account
#   - terraform, kubectl, and helm installed and on PATH
###

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

deploy_stack() {
  echo ">>> Step 1: Terraform init + apply (infra)"
  cd "${ROOT_DIR}/terraform"
  terraform init -input=false
  terraform apply -input=false -auto-approve

  echo ">>> Step 2: Configure kubectl using Terraform eks_connect output"
  EKS_CONNECT_CMD="$(terraform output -raw eks_connect)"
  echo "Running: ${EKS_CONNECT_CMD}"
  eval "${EKS_CONNECT_CMD}"

  echo ">>> Step 3: Deploy Helm charts (geth-node, load-generator, observability)"
  cd "${ROOT_DIR}"

  helm upgrade --install geth-node ./charts/geth-node
  helm upgrade --install load-generator ./charts/load-generator
  helm upgrade --install observability ./charts/observability

  echo ">>> Done. Current pods:"
  kubectl get pods -A
}

destroy_stack() {
  echo ">>> Destroying Helm releases (observability, load-generator, geth-node)"
  cd "${ROOT_DIR}"
  # Ignore errors if a release does not exist
  helm uninstall observability || true
  helm uninstall load-generator || true
  helm uninstall geth-node || true

  echo ">>> Destroying Terraform-managed infra"
  cd "${ROOT_DIR}/terraform"
  terraform destroy -auto-approve
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


