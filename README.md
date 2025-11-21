## Geth node infra + load generator

![Project Logo](./assets/nodeops-logo.png)

## Table of contents

- [Geth node infra + load generator](#geth-node-infra--load-generator)
- [Table of contents](#table-of-contents)
- [Introduction](#introduction)
  - [Directory overview](#directory-overview)
  - [State and backend](#state-and-backend)
- [Usage](#usage)
  - [Prerequisites](#prerequisites)
  - [Configuration](#configuration)
  - [Create backend resources](#create-backend-resources)
  - [Deploy the EKS cluster (Terraform)](#deploy-the-eks-cluster-terraform)
  - [Connect kubectl to the cluster](#connect-kubectl-to-the-cluster)
  - [Deploy Helm charts](#deploy-helm-charts)
  - [Build and publish the load generator image](#build-and-publish-the-load-generator-image)
  - [Deploy full stack locally](#deploy-full-stack-locally)
  - [Run the load generator locally](#run-the-load-generator-locally)
- [How to](#how-to)
  - [Deploy the full stack using GitHub Actions](#deploy-the-full-stack-using-github-actions)
  - [Add a new Helm chart to the pipeline](#add-a-new-helm-chart-to-the-pipeline)
- [Other resources](#other-resources)
- [License](#license)

## Introduction

This repository contains a self‑contained dev stack for running an Ethereum (Geth) node on AWS EKS, 
driving it with a configurable Python load generator, and observing everything via Prometheus + Grafana.
The Geth node uses persistent storage backed by AWS EBS volumes, so chain data survives pod restarts
and node upgrades.

Infrastructure is provisioned with Terraform, workloads are deployed with Helm, and GitHub Actions 
pipelines handle CI/CD:

- Terraform creates a simple EKS cluster on the default VPC.
- Helm charts deploy:
  - A Geth node (`charts/geth-node`) with persistent storage via AWS EBS + a Kubernetes PVC.
  - A Python load generator (`charts/load-generator`) that sends configurable transaction load and exports Prometheus metrics.
  - An observability stack (`charts/observability`) with Prometheus + Grafana scraping both node and workload.
- GitHub Actions automate:
  - Terraform plan/apply for infra.
  - Helm deployments per environment and chart.
  - Build & publish of the load generator Docker image.
  - Linting for Python and YAML.

### Directory overview

```text
.
├── .github/
│   └── workflows/                # GitHub Actions workflows
│       ├── infra-deploy.yml      # Terraform plan/apply for EKS infra
│       ├── helm-deploy.yml       # Helm deploy: geth-node, load-generator, observability
│       ├── build-load-generator.yml  # Build & push load-generator Docker image
│       └── linter.yml            # Python + YAML linting
├── charts/
│   ├── geth-node/                # Helm chart for the Geth node + prefund job
│   ├── load-generator/           # Helm chart for Python workload (Prometheus metrics)
│   └── observability/            # Helm chart for Prometheus + Grafana
├── load-generator-image/
│   ├── Dockerfile.workload       # Dockerfile for the Python load generator
│   ├── requirements.txt
│   └── workload.py               # Web3-based load generator with Prometheus metrics
├── terraform/
│   ├── main.tf                   # EKS cluster, IRSA, addons, outputs
│   └── variables.tf              # region, cluster_name, environment
├── .yamllint.yml                 # yamllint configuration (ignores Helm templates)
├── .gitignore                    # Ignore Terraform state, .terraform, etc.
└── README.md                     
```

### State and backend

Terraform remote state is stored in S3 with DynamoDB for state locking (configured in `terraform/main.tf`):

```hcl
terraform {
  backend "s3" {
    bucket         = "geth-node-infra-tf-state-eu-west-1"
    key            = "terraform/geth-node-infra.tfstate"
    region         = "eu-west-1"
    dynamodb_table = "geth-node-infra-tf-locks"
    encrypt        = true
  }
}
```

So you’ll have:

```text
s3://geth-node-infra-tf-state-eu-west-1/terraform/geth-node-infra.tfstate
```

DynamoDB table `geth-node-infra-tf-locks` is used to coordinate concurrent Terraform runs.

## Usage

The following steps describe how to bring up the infra, deploy charts, and run load.

> To keep this short: each section focuses on **what**, **why**, and **how** at a high level.

### Prerequisites

- **Terraform**: ≥ 1.6.0 (workflows currently use 1.9.8).
- **AWS CLI**: configured with access to the target AWS account.
- **kubectl**: compatible with the EKS version in `terraform/main.tf`.
- **Helm**: v3.
- **Docker**: to build and run the workload image locally (optional).
- GitHub:
  - `AWS_REGION` secret (e.g. `eu-west-1`).
  - `AWS_ROLE_NAME` secret: IAM role ARN assumed by GitHub OIDC for infra & Helm deploys.
  - `DOCKER_USERNAME` / `DOCKER_SECRET` for pushing the load-generator image.

### Configuration

Key tunables:

- `terraform/variables.tf`:
  - `region` – AWS region (cluster region).
  - `cluster_name` – name of the EKS cluster.
  - `environment` – tag/environment name (e.g. `dev`).

- `charts/geth-node/values.yaml`:
  - Node service ports, storage, and prefund job settings (account, amount, etc.).

- `charts/load-generator/values.yaml`:
  - `image.repository`/`tag` – load-generator image (built by CI).
  - `geth.url` – in-cluster Geth URL (e.g. `http://geth-node-geth-node.default.svc.cluster.local:8545`).
  - `workload.*` – TPS, concurrency, duration, metrics port.

- `charts/observability/values.yaml`:
  - Prometheus/Grafana settings (storage, service type, dashboards).

### Create backend resources

**What**: S3 bucket + DynamoDB table for Terraform backend.  
**Why**: Central, durable state with locking.  
**How**: Manually via AWS Console or Terraform elsewhere.

Minimal shape:

- S3 bucket:
  - Name: `geth-node-infra-tf-state-eu-west-1` (or adjust `main.tf` accordingly).
  - Versioning + encryption enabled.
- DynamoDB table:
  - Name: `geth-node-infra-tf-locks`.
  - Partition key: `LockID` (String).

If these already exist, no action is needed.

### Deploy the EKS cluster (Terraform)

You can apply infra either via GitHub Actions or locally.

**GitHub Actions – `infra-deploy.yml`**

- Trigger **Terraform Deploy** workflow:
  - For a PR: it will run `terraform plan` only.
  - Manual run (`workflow_dispatch`):
    - `environment`: `dev` / `stage` / `prod`.
    - `apply`: `"true"` to run `terraform apply` after a successful plan.
- The workflow:
  - Assumes `AWS_ROLE_NAME` via OIDC.
  - Runs `terraform init`, `fmt`, `validate`, `plan`, and optionally `apply` in `./terraform`.

**Local**

```bash
cd terraform
terraform init       # uses the remote S3 backend
terraform plan
terraform apply
```

### Connect kubectl to the cluster

The Terraform module exposes a helper output:

```hcl
output "eks_connect" {
  value = "aws eks --region ${var.region} update-kubeconfig --name ${module.eks.cluster_name}"
}
```

**Local:**

```bash
cd terraform
EKS_CONNECT_CMD=$(terraform output -raw eks_connect)
echo "$EKS_CONNECT_CMD"
eval "$EKS_CONNECT_CMD"

kubectl get nodes
```

GitHub Actions (`helm-deploy.yml`) does the same automatically before running Helm:

```bash
EKS_CONNECT_CMD=$(terraform output -raw eks_connect)
eval "$EKS_CONNECT_CMD"
```

### Deploy Helm charts

You have one shared Helm workflow that can deploy:

- `geth-node`
- `load-generator`
- `observability`
- or `all` of them.

**Via GitHub Actions – `helm-deploy.yml`**

1. Go to **Actions → Helm Deploy → Run workflow**.
2. Inputs:
   - `environment`: `dev` / `stage` / `prod`.
   - `chart`: `geth-node`, `load-generator`, `observability`, or `all`.
3. The workflow:
   - Assumes the AWS role with OIDC.
   - Uses Terraform remote state to configure `kubectl` for the EKS cluster.
   - Runs `helm upgrade --install` for the selected charts:
     - `helm upgrade --install geth-node ./charts/geth-node`
     - `helm upgrade --install load-generator ./charts/load-generator`
     - `helm upgrade --install observability ./charts/observability`

**Locally**

```bash
cd /path/to/geth-node-presto

# Geth node
helm upgrade --install geth-node ./charts/geth-node

# Load generator
helm upgrade --install load-generator ./charts/load-generator

# Observability (Prometheus + Grafana)
helm upgrade --install observability ./charts/observability
```

### Build and publish the load generator image

**Via GitHub Actions – `build-load-generator.yml`**

- Trigger **Build and Push Load Generator Image**:
  - On pushes to `infra-setup` that touch `load-generator-image/**`, or
  - Manually via **Run workflow**.
- The workflow:
  - Logs into Docker Hub using `DOCKER_USERNAME` / `DOCKER_SECRET`.
  - Builds `load-generator-image/Dockerfile.workload`.
  - Pushes `docker.io/adamkkk89/geth-workload:latest` (configurable via `WORKLOAD_IMAGE`).

Make sure `charts/load-generator/values.yaml` references the same image:

```yaml
image:
  repository: adamkkk89/geth-workload
  tag: latest
```

### Deploy full stack locally

If you want to stand up the entire stack (infra + all charts) from your laptop without going through GitHub Actions, use the helper script:

```bash
cd /path/to/geth-node-presto
chmod +x scripts/deploy.sh  # first time only
./scripts/deploy.sh
```

This will:

- Run `terraform init` and `terraform apply` in `terraform/`.
- Use the `eks_connect` Terraform output to configure `kubectl`.
- Deploy the `geth-node`, `load-generator`, and `observability` Helm charts.
- Print the current pods with `kubectl get pods -A` so you can quickly confirm everything is running.

### Run the load generator locally

You can also run the workload against the cluster from your laptop.

1. Port-forward the node’s JSON-RPC service:

```bash
kubectl port-forward -n default svc/geth-node-geth-node 8545:8545
```

2. Build and run the Docker image locally:

```bash
cd /path/to/geth-node-presto

docker build -f load-generator-image/Dockerfile.workload \
  -t geth-load-generator:latest \
  load-generator-image

docker run --rm --name geth-load-generator \
  -e GETH_URL="http://host.docker.internal:8545" \
  -e TARGET_TPS="20" \
  -e CONCURRENCY="5" \
  -e DURATION_SECONDS="120" \
  geth-load-generator:latest
```

The workload will:

- Connect to the node.
- Log chain ID and head block.
- Log balance for the prefunded account.
- Generate transactions at approximately `TARGET_TPS` with `CONCURRENCY` workers.
- Expose Prometheus metrics on `METRICS_PORT` (default 8000).

## How to

### Deploy the full stack using GitHub Actions

Typical flow for a new environment:

1. **Infra**: run **Terraform Deploy** (`infra-deploy.yml`) with:
   - `environment = dev` (or `stage` / `prod`),
   - `apply = "true"`.
2. **Workloads**: run **Helm Deploy** (`helm-deploy.yml`) with:
   - `environment = dev`,
   - `chart = all` (deploy Geth, load-generator, and observability in one go).
3. **Verify**:
   - `kubectl get pods -A` – check all pods are healthy.
   - Access Grafana (via the observability chart’s service) and verify:
     - Geth node metrics are ingested.
     - Load-generator metrics show TPS, latency, and error rate.

### Add a new Helm chart to the pipeline

1. Create a new chart under `charts/<your-chart>/` with `Chart.yaml`, `values.yaml`, and templates.
2. Update `helm-deploy.yml`:
   - Add `<your-chart>` to the `chart` input options.
   - Add a deploy step:

```yaml
- name: Deploy <your-chart> chart
  if: github.event.inputs.chart == '<your-chart>' || github.event.inputs.chart == 'all'
  working-directory: .
  run: |
    helm upgrade --install <your-chart> ./charts/<your-chart>
```

3. Optionally extend `.github/workflows/linter.yml` to include new `values.yaml`/`Chart.yaml` if needed.
4. Run the **Helm Deploy** workflow selecting your chart or `all`.


## Other resources

- Terraform EKS module docs: `terraform-aws-modules/eks/aws`.
- Helm docs: `https://helm.sh/docs/`.
- Prometheus & Grafana docs for metric scraping and dashboards.
- Web3.py docs for JSON-RPC and Ethereum interactions.

## License

See the `LICENSE` file in this repository (or your organization’s standard licensing terms).