output "aws_region" { value = var.region }
output "dynamodb_table" { value = aws_dynamodb_table.main.name }
output "media_bucket" { value = aws_s3_bucket.media.bucket }
output "cognito_user_pool_id" { value = aws_cognito_user_pool.main.id }
output "cognito_issuer" { value = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.main.id}" }
output "cognito_domain_url" { value = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${var.region}.amazoncognito.com" }
output "dashboard_client_id" { value = aws_cognito_user_pool_client.dashboard.id }
output "alexa_service_client_id" { value = aws_cognito_user_pool_client.alexa_service.id }
output "alexa_service_client_secret" {
  value     = aws_cognito_user_pool_client.alexa_service.client_secret
  sensitive = true
}
output "alexa_link_client_id" { value = try(aws_cognito_user_pool_client.alexa_link[0].id, "") }
output "alexa_link_client_secret" {
  value     = try(aws_cognito_user_pool_client.alexa_link[0].client_secret, "")
  sensitive = true
}
output "mcp_tools_scope" { value = local.tools_scope }
output "mcp_service_scope" { value = local.service_scope }