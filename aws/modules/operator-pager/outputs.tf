# Read through the topic POLICY, as cost-control does: same value, but every
# consumer is ordered after the publish grants exist.
output "pager_topic_arn" { value = aws_sns_topic_policy.pager.arn }

output "relay_log_group_name" { value = aws_cloudwatch_log_group.relay.name }

# Consumed by cost-control's AllowOperatorPagerRelayPublish statement.
output "relay_role_arn" { value = aws_iam_role.relay.arn }

output "relay_function_name" { value = aws_lambda_function.relay.function_name }

output "pager_alarm_names" {
  value = [
    aws_cloudwatch_composite_alarm.coord_crash_loop.alarm_name,
    aws_cloudwatch_composite_alarm.rds_free_storage.alarm_name,
    aws_cloudwatch_composite_alarm.rds_saturation.alarm_name,
  ]
}
