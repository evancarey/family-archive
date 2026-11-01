output "website_url" {
  description = "CloudFront URL for the family library web client."
  value       = "https://${aws_cloudfront_distribution.site.domain_name}"
}

output "api_url" {
  description = "Authenticated API base URL. Only configured routes are available."
  value       = aws_apigatewayv2_api.library.api_endpoint
}

output "cognito_user_pool_id" {
  description = "User pool ID for administrator-managed family accounts."
  value       = aws_cognito_user_pool.family.id
}

output "cognito_app_client_id" {
  description = "Public OAuth client ID for the browser application."
  value       = aws_cognito_user_pool_client.web.id
}

output "cognito_hosted_ui_domain" {
  description = "Cognito managed sign-in domain."
  value       = "https://${aws_cognito_user_pool_domain.family.domain}.auth.${var.aws_region}.amazoncognito.com"
}

output "web_bucket_name" {
  description = "Private S3 bucket that CloudFront uses to serve the web client."
  value       = aws_s3_bucket.web.bucket
}
