# The policy's arn, not the topic's: a consumer that publishes waits for the policy that admits it
# (the same pattern as aws/modules/cost-control's sns_topic_arn).
output "pager_topic_arn" { value = aws_sns_topic_policy.pager.arn }

output "relay_role_arn" { value = aws_iam_role.relay.arn }
output "relay_function_name" { value = aws_lambda_function.relay.function_name }
output "relay_log_group_name" { value = aws_cloudwatch_log_group.relay.name }
output "relay_dlq_arn" { value = aws_sqs_queue.relay_dlq.arn }
