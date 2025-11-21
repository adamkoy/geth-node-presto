variable "region" {
  type        = string
  default     = "eu-west-1"
  description = "AWS region for the dev cluster"
}

variable "cluster_name" {
  type        = string
  default     = "geth-dev-eks"
  description = "EKS cluster name"
}

variable "environment" {
  type        = string
  default     = "dev"
  description = "Environment tag"
}

