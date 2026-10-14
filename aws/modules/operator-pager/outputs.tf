# The policy's arn, not the topic's: a consumer that publishes waits for the policy that admits it
# (the same pattern as aws/modules/cost-control's sns_topic_arn).
output "pager_topic_arn" { value = aws_sns_topic_policy.pager.arn }

output "relay_role_arn" { value = aws_iam_role.relay.arn }
output "relay_function_name" { value = aws_lambda_function.relay.function_name }
output "relay_log_group_name" { value = aws_cloudwatch_log_group.relay.name }
output "relay_dlq_arn" { value = aws_sqs_queue.relay_dlq.arn }

# The three alarms that page, and the two that are only ever drilled (plan D16). The drill script
# and the post-apply routing check read these.
output "pager_alarm_names" {
  value = sort([
    aws_cloudwatch_composite_alarm.crash_loop.alarm_name,
    aws_cloudwatch_metric_alarm.free_storage.alarm_name,
    aws_cloudwatch_composite_alarm.saturation.alarm_name,
  ])
}
output "drill_child_alarm_names" { value = sort([for a in aws_cloudwatch_metric_alarm.drill : a.alarm_name]) }
output "pager_rule_names" {
  value = sort([
    aws_cloudwatch_event_rule.coord_task_exits.name,
    aws_cloudwatch_event_rule.deploy_failed.name,
    aws_cloudwatch_event_rule.rds_storage.name,
  ])
}

