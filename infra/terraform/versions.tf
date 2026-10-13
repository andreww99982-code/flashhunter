# Версии провайдеров и backend. Состояние хранится в S3 с блокировкой через DynamoDB:
# два инженера не могут одновременно применить разные изменения инфраструктуры.
terraform {
  required_version = ">= 1.9"

  required_providers {
    aws        = { source = "hashicorp/aws", version = "~> 5.70" }
    cloudflare = { source = "cloudflare/cloudflare", version = "~> 4.44" }
    random     = { source = "hashicorp/random", version = "~> 3.6" }
  }

  backend "s3" {
    bucket         = "flashhunter-tfstate"
    key            = "prod/terraform.tfstate"
    region         = "eu-west-1"          # ближе к основным RPC/EU-биржам — ниже задержка
    dynamodb_table = "flashhunter-tfstate-lock"
    encrypt        = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "flashhunter"
      Environment = var.environment
      ManagedBy   = "terraform"
      # Владелец ресурсов: без тега инцидентные вызовы некому адресовать
      Owner = var.owner_email
    }
  }
}
