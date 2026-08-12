#!/bin/sh
set -eu

# 秘密値そのものは受け取らず、登録済みParameter Store名だけをSAMへ渡す。
: "${AWS_REGION:?AWS_REGIONを設定してください}"
: "${XSBOT_AWS_STACK_NAME:?XSBOT_AWS_STACK_NAMEを設定してください}"
: "${XSBOT_AWS_ECR_REPOSITORY:?XSBOT_AWS_ECR_REPOSITORYを設定してください}"
: "${XSBOT_AWS_ENVIRONMENT:?XSBOT_AWS_ENVIRONMENTを設定してください}"
: "${XSBOT_AWS_SHEET_ID:?XSBOT_AWS_SHEET_IDを設定してください}"
: "${XSBOT_AWS_SHEETS_CREDENTIAL_PARAMETER:?XSBOT_AWS_SHEETS_CREDENTIAL_PARAMETERを設定してください}"
: "${XSBOT_AWS_ADMIN_AUTH_PARAMETER:?XSBOT_AWS_ADMIN_AUTH_PARAMETERを設定してください}"
: "${XSBOT_AWS_RUNTIME_SECRETS_PARAMETER:?XSBOT_AWS_RUNTIME_SECRETS_PARAMETERを設定してください}"

for command_name in aws docker sam; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "必要なコマンドが見つかりません: $command_name" >&2
        exit 1
    fi
done

for parameter_name in \
        "$XSBOT_AWS_SHEETS_CREDENTIAL_PARAMETER" \
        "$XSBOT_AWS_ADMIN_AUTH_PARAMETER" \
        "$XSBOT_AWS_RUNTIME_SECRETS_PARAMETER"; do
    case "$parameter_name" in
        /*) ;;
        *)
            echo "Parameter Store名は/から始めてください" >&2
            exit 1
            ;;
    esac
done

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
template_file="$script_directory/template.aws.yaml"
image_tag=${XSBOT_AWS_IMAGE_TAG:-$(date -u +%Y%m%dT%H%M%SZ)}

# ECRへ書き込む前にtemplateの構文とresource定義を検査する。
sam validate \
    --lint \
    --template-file "$template_file" \
    --region "$AWS_REGION"

case "$image_tag" in
    ''|*[!a-zA-Z0-9_.-]*)
        echo "XSBOT_AWS_IMAGE_TAGに使用できない文字が含まれています" >&2
        exit 1
        ;;
esac

repository_uri=$(aws ecr describe-repositories \
    --region "$AWS_REGION" \
    --repository-names "$XSBOT_AWS_ECR_REPOSITORY" \
    --query 'repositories[0].repositoryUri' \
    --output text)

case "$repository_uri" in
    ''|None)
        echo "既存ECR repositoryを取得できませんでした" >&2
        exit 1
        ;;
esac

registry_uri=${repository_uri%%/*}
image_uri="$repository_uri:$image_tag"

aws ecr get-login-password --region "$AWS_REGION" \
    | docker login --username AWS --password-stdin "$registry_uri"

# API、2つのworker、Fargateで共用する同一imageを1回だけbuild/pushする。
docker buildx build \
    --platform linux/amd64 \
    --tag "$image_uri" \
    --push \
    "$script_directory"

# ImageUriへ完成済みimageを渡すため、sam buildは実行しない。
sam deploy \
    --template-file "$template_file" \
    --stack-name "$XSBOT_AWS_STACK_NAME" \
    --region "$AWS_REGION" \
    --capabilities CAPABILITY_IAM \
    --confirm-changeset \
    --no-fail-on-empty-changeset \
    --parameter-overrides \
        "ParameterKey=ImageUri,ParameterValue=$image_uri" \
        "ParameterKey=EnvironmentName,ParameterValue=$XSBOT_AWS_ENVIRONMENT" \
        "ParameterKey=SheetId,ParameterValue=$XSBOT_AWS_SHEET_ID" \
        "ParameterKey=GoogleSheetsCredentialParameterName,ParameterValue=$XSBOT_AWS_SHEETS_CREDENTIAL_PARAMETER" \
        "ParameterKey=AdminAuthParameterName,ParameterValue=$XSBOT_AWS_ADMIN_AUTH_PARAMETER" \
        "ParameterKey=RuntimeSecretsParameterName,ParameterValue=$XSBOT_AWS_RUNTIME_SECRETS_PARAMETER"

aws cloudformation describe-stacks \
    --region "$AWS_REGION" \
    --stack-name "$XSBOT_AWS_STACK_NAME" \
    --query 'Stacks[0].Outputs[].{Key:OutputKey,Value:OutputValue}' \
    --output table
