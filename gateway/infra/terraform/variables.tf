variable "region" {
  type        = string
  default     = "us-east-1"
  description = "AWS region for the gateway."
}

variable "azs" {
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b"]
  description = "Availability zones for the module's subnets, Aurora and the ALB. At least two, in var.region."

  validation {
    condition     = length(var.azs) >= 2
    error_message = "The module needs at least two availability zones."
  }
}

variable "env" {
  type        = string
  description = "Environment name; also the suffix on every resource the module creates."

  validation {
    condition     = contains(["staging", "prod"], var.env)
    error_message = "env must be staging or prod."
  }
}

variable "gateway_hostname" {
  type        = string
  description = "Public hostname for the gateway, for example api.gw.opencompany.sh. It must sit inside the route53_zone_id zone."
}

variable "route53_zone_id" {
  type        = string
  description = "Route 53 hosted zone that holds gateway_hostname: gw.opencompany.sh, delegated from Cloudflare (see README)."
}

variable "gateway_min_tasks" {
  type        = number
  default     = 2
  description = "Minimum gateway tasks. Two keeps a task in each availability zone."
}

variable "gateway_max_tasks" {
  type        = number
  default     = 10
  description = "Maximum gateway tasks under CPU autoscaling. Bound it by the database connection budget."
}

variable "secret_env_names" {
  type        = list(string)
  description = "Environment variables the gateway reads from Secrets Manager. Terraform creates an empty secret for each; set the values before the first full apply."
  default = [
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_AGENT_API_KEY",
    "GROQ_API_KEY",
    "CEREBRAS_API_KEY",
    "XAI_API_KEY",
    "MOONSHOT_API_KEY",
    "MISTRAL_API_KEY",
    "SLACK_WEBHOOK_URL",
  ]
}
