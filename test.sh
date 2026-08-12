#!/bin/sh
set -eu

export XSBOT_DEPLOY_ENV=test

python3 -m unittest \
    tests.test_settings_configuration \
    tests.test_cloud_backend_factory \
    tests.test_credential_source \
    tests.test_state_store_boundary \
    tests.test_gcp_state_store_contract \
    tests.test_aws_state_store \
    tests.test_gcp_object_store \
    tests.test_aws_object_store \
    tests.test_auth \
    tests.test_expression \
    tests.test_runtime_isolation \
    tests.test_state_namespace \
    tests.test_scenario_storage \
    tests.plugin.test_google_sheets \
    tests.plugin.test_chatgpt \
    tests.plugin.test_liff \
    tests.plugin.test_mock_line \
    tests.plugin.test_images \
    tests.plugin.test_twilio \
    tests.plugin.test_line_contracts \
    tests.test_task_client \
    tests.test_task_queue_contract \
    tests.test_aws_task_queue \
    tests.test_async_task_processor \
    tests.test_aws_task_handler \
    tests.test_auth_middleware \
    tests.test_api_endpoints \
    tests.test_group_message_batching \
    tests.test_dashboard \
    tests.test_initialization_boundaries \
    tests.test_container_configuration \
    tests.test_logparser
