#!/bin/sh
set -eu

# 互換性を維持したWebchat Scenarioを更新する。
# epoch変更とimage build/pushは行わない。

umask 077

: "${AWS_REGION:?AWS_REGIONを設定してください}"
: "${XSBOT_AWS_STACK_NAME:?XSBOT_AWS_STACK_NAMEを設定してください}"
: "${XSBOT_WEBCHAT_SCENARIO_URI:?XSBOT_WEBCHAT_SCENARIO_URIを設定してください}"

if ! printf '%s\n' "$XSBOT_WEBCHAT_SCENARIO_URI" \
        | grep -Eq '^s3://[^/]+/scenario/[0-9a-f]{32}$'; then
    echo "XSBOT_WEBCHAT_SCENARIO_URIの形式が不正です" >&2
    exit 1
fi

if ! command -v aws >/dev/null 2>&1; then
    echo "必要なコマンドが見つかりません: aws" >&2
    exit 1
fi

change_set_name=${XSBOT_WEBCHAT_CHANGE_SET_NAME:-webchat-scenario-$(date -u +%Y%m%dT%H%M%SZ)}
case "$change_set_name" in
    ''|[!a-zA-Z]*|*[!a-zA-Z0-9-]*)
        echo "XSBOT_WEBCHAT_CHANGE_SET_NAMEの形式が不正です" >&2
        exit 1
        ;;
esac

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
template_file="$script_directory/template.aws.yaml"
if [ ! -s "$template_file" ]; then
    echo "template.aws.yamlが見つかりません" >&2
    exit 1
fi

scenario_path=${XSBOT_WEBCHAT_SCENARIO_URI#s3://}
scenario_bucket=${scenario_path%%/*}
scenario_key=${scenario_path#*/}

# 更新前に、指定したimmutable artifactが存在することだけ確認する。
aws s3api head-object \
    --region "$AWS_REGION" \
    --bucket "$scenario_bucket" \
    --key "$scenario_key" \
    --query '{Size:ContentLength,ETag:ETag}' \
    --output json >/dev/null

set -- \
    "ParameterKey=ImageUri,UsePreviousValue=true" \
    "ParameterKey=EnvironmentName,UsePreviousValue=true" \
    "ParameterKey=SheetId,UsePreviousValue=true" \
    "ParameterKey=GoogleSheetsCredentialParameterName,UsePreviousValue=true" \
    "ParameterKey=AdminAuthParameterName,UsePreviousValue=true" \
    "ParameterKey=RuntimeSecretsParameterName,UsePreviousValue=true" \
    "ParameterKey=WebchatEnabled,UsePreviousValue=true" \
    "ParameterKey=WebchatImageUri,UsePreviousValue=true" \
    "ParameterKey=WebchatSigningKey,UsePreviousValue=true" \
    "ParameterKey=WebchatScenarioUri,ParameterValue=$XSBOT_WEBCHAT_SCENARIO_URI" \
    "ParameterKey=WebchatCompatibilityEpoch,UsePreviousValue=true" \
    "ParameterKey=WebchatAllowedOrigins,UsePreviousValue=true" \
    "ParameterKey=WebchatExternalHttpOrigins,UsePreviousValue=true" \
    "ParameterKey=WebchatMediaOrigins,UsePreviousValue=true" \
    "ParameterKey=WebchatThrottleRate,UsePreviousValue=true" \
    "ParameterKey=WebchatThrottleBurst,UsePreviousValue=true"

aws cloudformation create-change-set \
    --region "$AWS_REGION" \
    --stack-name "$XSBOT_AWS_STACK_NAME" \
    --change-set-name "$change_set_name" \
    --change-set-type UPDATE \
    --description "Update Webchat Scenario" \
    --template-body "file://$template_file" \
    --parameters "$@" \
    --capabilities CAPABILITY_IAM \
    --query Id \
    --output text

if ! aws cloudformation wait change-set-create-complete \
        --region "$AWS_REGION" \
        --stack-name "$XSBOT_AWS_STACK_NAME" \
        --change-set-name "$change_set_name"; then
    aws cloudformation describe-change-set \
        --region "$AWS_REGION" \
        --stack-name "$XSBOT_AWS_STACK_NAME" \
        --change-set-name "$change_set_name" \
        --query '{Status:Status,Reason:StatusReason}' \
        --output table
    exit 1
fi

# Scenario更新で許可するのはWebchat Function、version、aliasだけである。
# transform差分等が混入したchange setは、確認promptへ進む前に削除する。
change_resources=$(aws cloudformation describe-change-set \
    --region "$AWS_REGION" \
    --stack-name "$XSBOT_AWS_STACK_NAME" \
    --change-set-name "$change_set_name" \
    --query 'Changes[].ResourceChange.[LogicalResourceId,ResourceType,Action]' \
    --output text)

unexpected_changes=
tab=$(printf '\t')
while IFS="$tab" read -r logical_id resource_type action; do
    [ -n "$logical_id" ] || continue
    case "$logical_id:$resource_type:$action" in
        WebchatFunction:AWS::Lambda::Function:Modify|\
        WebchatFunctionAliaslive:AWS::Lambda::Alias:Modify|\
        WebchatFunctionVersion*:AWS::Lambda::Version:Add|\
        WebchatFunctionVersion*:AWS::Lambda::Version:Remove)
            ;;
        *)
            unexpected_changes="${unexpected_changes}${logical_id}${tab}${resource_type}${tab}${action}\n"
            ;;
    esac
done <<EOF
$change_resources
EOF

if [ -n "$unexpected_changes" ]; then
    echo "Webchat Scenario以外の変更が含まれるため実行しません" >&2
    printf '%b' "$unexpected_changes" >&2
    aws cloudformation delete-change-set \
        --region "$AWS_REGION" \
        --stack-name "$XSBOT_AWS_STACK_NAME" \
        --change-set-name "$change_set_name"
    exit 1
fi

aws cloudformation describe-change-set \
    --region "$AWS_REGION" \
    --stack-name "$XSBOT_AWS_STACK_NAME" \
    --change-set-name "$change_set_name" \
    --query 'Changes[].ResourceChange.{Action:Action,LogicalResourceId:LogicalResourceId,ResourceType:ResourceType,Replacement:Replacement,Scope:Scope}' \
    --output table

printf "この変更を実行しますか？ [y/N] "
if ! IFS= read -r answer; then
    answer=
fi
case "$answer" in
    y|Y|yes|YES|Yes)
        ;;
    *)
        echo "更新を中止しました。change set '$change_set_name'は実行していません。"
        exit 0
        ;;
esac

aws cloudformation execute-change-set \
    --region "$AWS_REGION" \
    --stack-name "$XSBOT_AWS_STACK_NAME" \
    --change-set-name "$change_set_name"

if ! aws cloudformation wait stack-update-complete \
        --region "$AWS_REGION" \
        --stack-name "$XSBOT_AWS_STACK_NAME"; then
    aws cloudformation describe-stacks \
        --region "$AWS_REGION" \
        --stack-name "$XSBOT_AWS_STACK_NAME" \
        --query 'Stacks[0].{Status:StackStatus,Reason:StackStatusReason}' \
        --output table
    exit 1
fi

aws cloudformation describe-stacks \
    --region "$AWS_REGION" \
    --stack-name "$XSBOT_AWS_STACK_NAME" \
    --query 'Stacks[0].{Status:StackStatus,Updated:LastUpdatedTime}' \
    --output table

echo "Webchat Scenarioの更新が完了しました。"
