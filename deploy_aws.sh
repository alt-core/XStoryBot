#!/bin/sh
set -eu

# 共通runtime秘密値はParameter Store名だけを渡す。Webchat署名鍵だけは
# Lambda versionへ固定するため、NoEcho parameterとしてdeploy時に注入する。
: "${AWS_REGION:?AWS_REGIONを設定してください}"
: "${XSBOT_AWS_STACK_NAME:?XSBOT_AWS_STACK_NAMEを設定してください}"
: "${XSBOT_AWS_ECR_REPOSITORY:?XSBOT_AWS_ECR_REPOSITORYを設定してください}"
: "${XSBOT_AWS_ENVIRONMENT:?XSBOT_AWS_ENVIRONMENTを設定してください}"
: "${XSBOT_AWS_SHEET_ID:?XSBOT_AWS_SHEET_IDを設定してください}"
: "${XSBOT_AWS_SHEETS_CREDENTIAL_PARAMETER:?XSBOT_AWS_SHEETS_CREDENTIAL_PARAMETERを設定してください}"
: "${XSBOT_AWS_ADMIN_AUTH_PARAMETER:?XSBOT_AWS_ADMIN_AUTH_PARAMETERを設定してください}"
: "${XSBOT_AWS_RUNTIME_SECRETS_PARAMETER:?XSBOT_AWS_RUNTIME_SECRETS_PARAMETERを設定してください}"

if [ "${XSBOT_WEBCHAT_ENABLED+x}" = x ]; then
    case "$XSBOT_WEBCHAT_ENABLED" in
        true|false) ;;
        *)
            echo "XSBOT_WEBCHAT_ENABLEDはtrueまたはfalseで指定してください" >&2
            exit 1
            ;;
    esac
fi

if [ "${XSBOT_WEBCHAT_ENABLED-}" = true ]; then
    : "${XSBOT_WEBCHAT_SIGNING_KEY:?XSBOT_WEBCHAT_SIGNING_KEYを設定してください}"
    : "${XSBOT_WEBCHAT_SCENARIO_URI:?XSBOT_WEBCHAT_SCENARIO_URIを設定してください}"
fi

# 未設定は前回値を維持する。空が無効な項目の明示空はbuild前に拒否する。
check_optional_value() {
    if [ "$2" = x ] && [ -z "$3" ]; then
        echo "$1に空文字は指定できません" >&2
        exit 1
    fi
}

check_optional_value XSBOT_WEBCHAT_SIGNING_KEY \
    "${XSBOT_WEBCHAT_SIGNING_KEY+x}" "${XSBOT_WEBCHAT_SIGNING_KEY-}"
check_optional_value XSBOT_WEBCHAT_SCENARIO_URI \
    "${XSBOT_WEBCHAT_SCENARIO_URI+x}" "${XSBOT_WEBCHAT_SCENARIO_URI-}"
check_optional_value XSBOT_WEBCHAT_COMPATIBILITY_EPOCH \
    "${XSBOT_WEBCHAT_COMPATIBILITY_EPOCH+x}" "${XSBOT_WEBCHAT_COMPATIBILITY_EPOCH-}"
check_optional_value XSBOT_WEBCHAT_THROTTLE_RATE \
    "${XSBOT_WEBCHAT_THROTTLE_RATE+x}" "${XSBOT_WEBCHAT_THROTTLE_RATE-}"
check_optional_value XSBOT_WEBCHAT_THROTTLE_BURST \
    "${XSBOT_WEBCHAT_THROTTLE_BURST+x}" "${XSBOT_WEBCHAT_THROTTLE_BURST-}"

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

if [ ! -s "$script_directory/settings.yaml" ]; then
    echo "settings.yamlを準備してください。空の設定ではdeployできません" >&2
    exit 1
fi

# 任意 plugin（twilio／pusher）を使う場合だけ、追加 requirements を image に入れる
extra_requirements=${XSBOT_EXTRA_REQUIREMENTS:-}
if [ -n "$extra_requirements" ] && [ ! -s "$script_directory/$extra_requirements" ]; then
    echo "XSBOT_EXTRA_REQUIREMENTS のファイルが見つかりません: $extra_requirements" >&2
    exit 1
fi

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
    --provenance=false \
    --build-arg XSBOT_CLOUD_PROVIDER=aws \
    --build-arg "XSBOT_EXTRA_REQUIREMENTS=$extra_requirements" \
    --tag "$image_uri" \
    --push \
    "$script_directory"

# ImageUriへ完成済みimageを渡すため、sam buildは実行しない。
set -- sam deploy \
    --template-file "$template_file" \
    --stack-name "$XSBOT_AWS_STACK_NAME" \
    --region "$AWS_REGION" \
    --image-repository "$repository_uri" \
    --capabilities CAPABILITY_IAM \
    --confirm-changeset \
    --no-fail-on-empty-changeset \
    --parameter-overrides \
        "ParameterKey=ImageUri,ParameterValue=$image_uri" \
        "ParameterKey=WebchatImageUri,ParameterValue=$image_uri" \
        "ParameterKey=EnvironmentName,ParameterValue=$XSBOT_AWS_ENVIRONMENT" \
        "ParameterKey=SheetId,ParameterValue=$XSBOT_AWS_SHEET_ID" \
        "ParameterKey=GoogleSheetsCredentialParameterName,ParameterValue=$XSBOT_AWS_SHEETS_CREDENTIAL_PARAMETER" \
        "ParameterKey=AdminAuthParameterName,ParameterValue=$XSBOT_AWS_ADMIN_AUTH_PARAMETER" \
        "ParameterKey=RuntimeSecretsParameterName,ParameterValue=$XSBOT_AWS_RUNTIME_SECRETS_PARAMETER"

# SAMが空文字を認識できるよう、空を許す値は引用符も引数に含める。
if [ "${XSBOT_AWS_ALARM_EMAIL+x}" = x ]; then
    set -- "$@" \
        "ParameterKey=AlarmEmail,ParameterValue=\"$XSBOT_AWS_ALARM_EMAIL\""
fi

if [ "${XSBOT_WEBCHAT_ENABLED+x}" = x ]; then
    set -- "$@" \
        "ParameterKey=WebchatEnabled,ParameterValue=$XSBOT_WEBCHAT_ENABLED"
fi
if [ "${XSBOT_WEBCHAT_SIGNING_KEY+x}" = x ]; then
    set -- "$@" \
        "ParameterKey=WebchatSigningKey,ParameterValue=$XSBOT_WEBCHAT_SIGNING_KEY"
fi
if [ "${XSBOT_WEBCHAT_SCENARIO_URI+x}" = x ]; then
    set -- "$@" \
        "ParameterKey=WebchatScenarioUri,ParameterValue=$XSBOT_WEBCHAT_SCENARIO_URI"
fi
if [ "${XSBOT_WEBCHAT_COMPATIBILITY_EPOCH+x}" = x ]; then
    set -- "$@" \
        "ParameterKey=WebchatCompatibilityEpoch,ParameterValue=$XSBOT_WEBCHAT_COMPATIBILITY_EPOCH"
fi
if [ "${XSBOT_WEBCHAT_ALLOWED_ORIGINS+x}" = x ]; then
    set -- "$@" \
        "ParameterKey=WebchatAllowedOrigins,ParameterValue=\"$XSBOT_WEBCHAT_ALLOWED_ORIGINS\""
fi
if [ "${XSBOT_WEBCHAT_EXTERNAL_HTTP_ORIGINS+x}" = x ]; then
    set -- "$@" \
        "ParameterKey=WebchatExternalHttpOrigins,ParameterValue=\"$XSBOT_WEBCHAT_EXTERNAL_HTTP_ORIGINS\""
fi
if [ "${XSBOT_WEBCHAT_MEDIA_ORIGINS+x}" = x ]; then
    set -- "$@" \
        "ParameterKey=WebchatMediaOrigins,ParameterValue=\"$XSBOT_WEBCHAT_MEDIA_ORIGINS\""
fi
if [ "${XSBOT_WEBCHAT_THROTTLE_RATE+x}" = x ]; then
    set -- "$@" \
        "ParameterKey=WebchatThrottleRate,ParameterValue=$XSBOT_WEBCHAT_THROTTLE_RATE"
fi
if [ "${XSBOT_WEBCHAT_THROTTLE_BURST+x}" = x ]; then
    set -- "$@" \
        "ParameterKey=WebchatThrottleBurst,ParameterValue=$XSBOT_WEBCHAT_THROTTLE_BURST"
fi

"$@"

aws cloudformation describe-stacks \
    --region "$AWS_REGION" \
    --stack-name "$XSBOT_AWS_STACK_NAME" \
    --query 'Stacks[0].Outputs[].{Key:OutputKey,Value:OutputValue}' \
    --output table
