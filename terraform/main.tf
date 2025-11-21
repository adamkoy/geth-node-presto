terraform {
  backend "s3" {
    bucket         = "geth-node-infra-tf-state-eu-west-1"
    key            = "terraform/geth-node-infra.tfstate"
    region         = "eu-west-1"
    dynamodb_table = "geth-node-infra-tf-locks"
    encrypt        = true
  }

  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

########################
# Use default VPC (easy dev)
########################

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

########################
# EKS dev cluster
########################

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.31"

  cluster_name    = var.cluster_name
  cluster_version = "1.31"

  # Dev → keep it simple: public endpoint allowed
  cluster_endpoint_private_access = true
  cluster_endpoint_public_access  = true

  enable_irsa                              = true
  enable_cluster_creator_admin_permissions = true
  vpc_id                                   = data.aws_vpc.default.id
  subnet_ids                               = data.aws_subnets.default.ids

  eks_managed_node_groups = {
    geth-dev = {
      description = "Single small node group for geth dev"

      instance_types = ["t3.medium"]
      min_size       = 1
      max_size       = 1
      desired_size   = 1

      capacity_type = "ON_DEMAND"

      labels = {
        role = "geth-dev"
      }
    }
  }

  cluster_addons = {
    coredns    = { most_recent = true }
    kube-proxy = { most_recent = true }
    vpc-cni    = { most_recent = true }

    aws-ebs-csi-driver = {
      most_recent              = true
      service_account_role_arn = module.ebs_csi_irsa.iam_role_arn
    }
  }

  tags = {
    Environment = var.environment
    Terraform   = "true"
  }
}

########################
# IRSA role for EBS CSI
########################

module "ebs_csi_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.47"

  role_name_prefix      = "${var.cluster_name}-ebs-csi"
  attach_ebs_csi_policy = true

  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["kube-system:ebs-csi-controller-sa"]
    }
  }
}

########################
# Outputs
########################

output "eks_connect" {
  value = "aws eks --region ${var.region} update-kubeconfig --name ${module.eks.cluster_name}"
}

output "cluster_name" {
  value = module.eks.cluster_name
}
