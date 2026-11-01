# Drex's functions on a ClickHouse Cloud service: one Python executable UDF per function, from the zips that
# ../build_bundles.py writes, attached to the service. The key is inside the zips, never in Terraform's state.
#
#   NACE_API_KEY=... python3 ../build_bundles.py --pool-size 2
#   terraform init && terraform apply -var service_id=<service id>
#
# Function names are unique within a ClickHouse Cloud organization, so one organization has one set of these
# functions, with one key; attach them to each service that needs them.

terraform {
  required_providers {
    clickhouse = {
      source  = "ClickHouse/clickhouse"
      version = ">= 3.24.0"
    }
  }
}

variable "service_id" {
  type        = string
  description = "The ClickHouse Cloud service to attach the functions to."
}

variable "bundles_dir" {
  type        = string
  default     = "../dist"
  description = "Where build_bundles.py wrote the zips, relative to this module."
}

variable "pool_size" {
  type        = number
  default     = 2
  description = "Processes per function and replica. Build the zips with the same --pool-size, which sets each process's share of 8 requests in flight."
}

locals {
  # The same signatures as config/drex_function.xml
  functions = {
    noul = {
      return_type = "Nullable(Float64)"
      arguments = [
        { name = "input", type = "Nullable(String)" },
        { name = "instructions", type = "Nullable(String)" },
        { name = "criteria", type = "Nullable(String)" },
        { name = "model", type = "Nullable(String)" },
      ]
    }
    choice = {
      return_type = "Tuple(choice Nullable(String), probabilities Array(Tuple(value String, probability Float64)), confidence Nullable(Float64))"
      arguments = [
        { name = "input", type = "Nullable(String)" },
        { name = "instructions", type = "Nullable(String)" },
        { name = "labels", type = "Array(String)" },
        { name = "model", type = "Nullable(String)" },
      ]
    }
    score = {
      return_type = "Tuple(score Nullable(Float64), probabilities Array(Tuple(index UInt32, value String, probability Float64)), confidence Nullable(Float64))"
      arguments = [
        { name = "input", type = "Nullable(String)" },
        { name = "instructions", type = "Nullable(String)" },
        { name = "levels", type = "Array(String)" },
        { name = "model", type = "Nullable(String)" },
      ]
    }
    decide = {
      return_type = "Nullable(String)"
      arguments = [
        { name = "state", type = "Nullable(String)" },
        { name = "questions", type = "Nullable(String)" },
        { name = "model", type = "Nullable(String)" },
      ]
    }
    decide_image = {
      return_type = "Nullable(String)"
      arguments = [
        { name = "state", type = "Nullable(String)" },
        { name = "images", type = "Array(String)" },
        { name = "questions", type = "Nullable(String)" },
        { name = "model", type = "Nullable(String)" },
      ]
    }
  }
}

resource "clickhouse_udf" "drex" {
  for_each = local.functions

  function_name = "drex_${each.key}"
  runtime       = "python3.11"
  type          = "executable_pool"
  # Outbound HTTPS to Drex
  sandbox_type = "netenable"
  return_type  = each.value.return_type
  arguments    = each.value.arguments

  format            = "RowBinary"
  send_chunk_header = true
  pool_size         = var.pool_size
  # A request takes at most about 180 s (5 attempts within 120 s, each up to 60 s); rows are written as they finish
  command_read_timeout = 200000
  # How long a query waits for a free process of the pool
  max_command_execution_time = 3600

  source_archive_path = "${path.module}/${var.bundles_dir}/drex_${each.key}.zip"
  source_archive_hash = filebase64sha256("${path.module}/${var.bundles_dir}/drex_${each.key}.zip")
}

resource "clickhouse_udf_attachment" "drex" {
  for_each = clickhouse_udf.drex

  function_name = each.value.function_name
  service_id    = var.service_id
  version       = each.value.version
}
