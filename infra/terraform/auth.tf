resource "aws_cognito_user_pool" "main" {
  name                     = local.name
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

resource "aws_cognito_user_pool_domain" "main" {
  domain       = "${local.name}-${data.aws_caller_identity.current.account_id}"
  user_pool_id = aws_cognito_user_pool.main.id
}

resource "aws_cognito_resource_server" "mcp" {
  identifier   = "homie"
  name         = "homie-mcp"
  user_pool_id = aws_cognito_user_pool.main.id

  scope {
    scope_name        = "mcp:tools"
    scope_description = "Use homie tools on behalf of the user"
  }
  scope {
    scope_name        = "mcp:service"
    scope_description = "Service-level access for Alexa"
  }
}

locals {
  tools_scope   = "${aws_cognito_resource_server.mcp.identifier}/mcp:tools"
  service_scope = "${aws_cognito_resource_server.mcp.identifier}/mcp:service"
}

resource "aws_cognito_user_pool_client" "dashboard" {
  name                                 = "dashboard"
  user_pool_id                         = aws_cognito_user_pool.main.id
  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile", local.tools_scope]
  callback_urls                        = [for u in var.dashboard_urls : "${u}/auth/callback"]
  logout_urls                          = var.dashboard_urls
  supported_identity_providers         = ["COGNITO"]
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_SRP_AUTH"]
  prevent_user_existence_errors        = "ENABLED"
}

resource "aws_cognito_user_pool_client" "alexa_service" {
  name                                 = "alexa-service"
  user_pool_id                         = aws_cognito_user_pool.main.id
  generate_secret                      = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["client_credentials"]
  allowed_oauth_scopes                 = [local.service_scope]
  supported_identity_providers         = ["COGNITO"]
}

resource "aws_cognito_user_pool_client" "alexa_link" {
  count                                = length(var.alexa_redirect_uris) > 0 ? 1 : 0
  name                                 = "alexa-link"
  user_pool_id                         = aws_cognito_user_pool.main.id
  generate_secret                      = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = [local.tools_scope]
  callback_urls                        = var.alexa_redirect_uris
  supported_identity_providers         = ["COGNITO"]
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors        = "ENABLED"
}