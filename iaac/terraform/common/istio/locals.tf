locals {
  name = "istio"

  default_helm_config = {
    name        = local.name
    version     = "0.1.0"
    namespace   = "default"    # change to namespace resources are being created it
    values      = []
    timeout     = "600"
  }

  helm_config = merge(
    local.default_helm_config,
    var.helm_config
  )

}
