import json
import logging
import time
import traceback
import uuid

from bottle import Bottle, HTTPResponse, request, response

import utility
from plugin.webchat.errors import (
    BotResponseTooLarge,
    InvalidStateToken,
    TurnDeadlineExceeded,
    WebchatError,
)


app = Bottle()
MAX_SYNC_RESPONSE_BYTES = 6 * 1024 * 1024
_get_bot_callback = None


def configure(get_bot):
    global _get_bot_callback
    _get_bot_callback = get_bot


def _get_bot(bot_name):
    if _get_bot_callback is not None:
        return _get_bot_callback(bot_name)
    import main
    return main.get_bot(bot_name)


def _request_id():
    context = _lambda_context()
    value = context.get('request_id')
    if isinstance(value, str):
        try:
            return str(uuid.UUID(value))
        except ValueError:
            pass
    return str(uuid.uuid4())


def _lambda_context():
    cached = request.environ.get('webchat.lambda_context')
    if isinstance(cached, dict):
        return cached
    raw = request.headers.get('X-Amzn-Lambda-Context')
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    request.environ['webchat.lambda_context'] = value
    return value


def _deadline_seconds(interface):
    fallback = interface.turn_deadline_seconds
    deadline = _lambda_context().get('deadline')
    if not isinstance(deadline, (int, float)):
        return fallback
    remaining = (float(deadline) / 1000.0) - time.time()
    return max(0.0, min(fallback, remaining))


def _log_event(event, **values):
    logging.info(json.dumps({
        'type': 'XSBWebchat',
        'event': event,
        **values,
    }, ensure_ascii=False, separators=(',', ':')))


def _json_response(data, status=200, content_type='application/json'):
    body = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    body_bytes = len(body.encode('utf-8'))
    if body_bytes > MAX_SYNC_RESPONSE_BYTES:
        raise BotResponseTooLarge('同期Lambda response上限を超えました')
    headers = {
        'Content-Type': f'{content_type}; charset=utf-8',
        'Cache-Control': 'no-store',
    }
    origin = request.headers.get('Origin')
    interface = request.environ.get('webchat.interface')
    if interface and origin and interface.origin_allowed(origin):
        headers['Access-Control-Allow-Origin'] = origin
        headers['Vary'] = 'Origin'
    if status == 200:
        _log_event(
            'response-encoded',
            request_id=request.environ.get('webchat.request_id'),
            status=status,
            response_bytes=body_bytes,
        )
    raise HTTPResponse(body=body, status=status, headers=headers)


def _problem(error, request_id):
    status = getattr(error, 'status', 500)
    code = getattr(error, 'code', 'internal-error')
    started_at = request.environ.get('webchat.started_at')
    elapsed_ms = (
        int((time.monotonic() - started_at) * 1000)
        if isinstance(started_at, (int, float)) else None
    )
    if status >= 500:
        frames = [
            f'{frame.filename.rsplit("/", 1)[-1]}:{frame.lineno}:{frame.name}'
            for frame in traceback.extract_tb(error.__traceback__)
        ]
        logging.error(
            'Webchat request failed: request_id=%s error_type=%s '
            'code=%s request_bytes=%s elapsed_ms=%s frames=%s',
            request_id, type(error).__name__, code,
            request.content_length, elapsed_ms, frames)
    else:
        logging.warning(
            'Webchat request rejected: request_id=%s error_type=%s code=%s '
            'request_bytes=%s elapsed_ms=%s',
            request_id, type(error).__name__, code,
            request.content_length, elapsed_ms)
    _json_response({
        'type': 'about:blank',
        'title': code,
        'status': status,
        'code': code,
        'request_id': request_id,
    }, status=status, content_type='application/problem+json')


def _bot_and_interface(bot_name):
    bot = _get_bot(bot_name)
    if bot is None:
        raise InvalidStateToken('Botが見つかりません')
    interface = bot.get_interface('webchat')
    if interface is None:
        raise InvalidStateToken('Webchatが有効ではありません')
    request.environ['webchat.interface'] = interface
    return bot, interface


def _require_origin(interface):
    origin = request.headers.get('Origin')
    if not interface.origin_allowed(origin):
        error = WebchatError('Originが許可されていません')
        error.status = 403
        error.code = 'invalid-origin'
        raise error
    return origin


def _invalid_request(message='request形式が不正です'):
    error = WebchatError(message)
    error.status = 400
    error.code = 'invalid-request'
    return error


def _require_exact_keys(value, expected):
    if set(value) != set(expected):
        raise _invalid_request()


@app.route('/api/webchat/v1/bots/<bot_name>/turn', method='OPTIONS')
def options_turn(bot_name):
    request.environ['webchat.started_at'] = time.monotonic()
    request_id = _request_id()
    try:
        _bot, interface = _bot_and_interface(bot_name)
        origin = _require_origin(interface)
        headers = {
            'Access-Control-Allow-Origin': origin,
            'Access-Control-Allow-Methods': 'POST, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type',
            'Vary': 'Origin',
            'Cache-Control': 'no-store',
        }
        raise HTTPResponse(body='', status=204, headers=headers)
    except HTTPResponse:
        raise
    except Exception as error:
        _problem(error, request_id)


@app.post('/api/webchat/v1/bots/<bot_name>/turn')
def turn(bot_name):
    request.environ['webchat.started_at'] = time.monotonic()
    request_id = _request_id()
    request.environ['webchat.request_id'] = request_id
    try:
        bot, interface = _bot_and_interface(bot_name)
        _require_origin(interface)
        media_type = request.content_type.split(';', 1)[0].strip()
        if media_type != 'application/json':
            error = WebchatError('JSONだけを受理します')
            error.status = 415
            error.code = 'unsupported-media-type'
            raise error
        try:
            data = request.json
        except Exception as error:
            raise _invalid_request('JSONを解析できません') from error
        if not isinstance(data, dict) or not isinstance(data.get('input'), dict):
            raise _invalid_request()
        input_data = data['input']
        input_type = input_data.get('type')
        echo_message = None
        received_state_id = None
        received_revision = None
        received_scenario_revision = None
        deadline_seconds = _deadline_seconds(interface)

        if input_type == 'start':
            _require_exact_keys(data, {'input'})
            _require_exact_keys(input_data, {'type'})
            context = interface.create_start_context(
                request_id, deadline_seconds=deadline_seconds)
        else:
            if 'state_token' not in data:
                raise _invalid_request('state tokenがありません')
            _require_exact_keys(data, {'input', 'state_token'})
            state_token = data.get('state_token')
            state_payload = interface.load_state(state_token)
            received_state_id = interface.codec.state_id(state_token)
            received_revision = state_payload['revision']
            received_scenario_revision = state_payload.get(
                'scenario_revision')
            if input_type == 'text':
                _require_exact_keys(input_data, {'type', 'text'})
                text = input_data.get('text')
                if not isinstance(text, str):
                    raise _invalid_request('text inputが不正です')
                action = utility.sanitize_action(text)
                echo_message = text
            elif input_type == 'postback':
                _require_exact_keys(
                    input_data, {'type', 'postback_token'})
                postback = interface.load_postback(
                    input_data.get('postback_token'), state_payload)
                action = postback['resolved_action']
                echo_message = postback.get('echo_text')
            else:
                error = WebchatError('input typeが不明です')
                error.status = 422
                error.code = 'unsupported-input'
                raise error
            context = interface.create_context_from_state(
                state_payload, action, request_id, echo_message,
                deadline_seconds=deadline_seconds)

        _log_event(
            'turn-accepted',
            request_id=request_id,
            bot=bot_name,
            input_type=input_type,
            conversation=context.user.user_id,
            received_state_id=received_state_id,
            received_revision=received_revision,
            received_scenario_revision=received_scenario_revision,
            current_scenario_revision=interface.scenario_revision,
            compatibility_epoch=interface.compatibility_epoch,
            action=context.action,
            request_bytes=request.content_length,
            state_token_bytes=(
                len(data['state_token'].encode('utf-8'))
                if isinstance(data.get('state_token'), str) else 0
            ),
            postback_token_bytes=(
                len(input_data['postback_token'].encode('utf-8'))
                if isinstance(input_data.get('postback_token'), str) else 0
            ),
        )

        interface.ensure_scenario(bot)
        if context.deadline - time.monotonic() <= 0.5:
            raise TurnDeadlineExceeded(
                'Webchat turn deadlineを超えました')
        result = bot.handle_action(context)
        if not isinstance(result, dict):
            raise WebchatError('Webchat responseを生成できませんでした')
        original_player = context.original_player
        saved_player = context.saved_player
        original_flags = original_player.get('flags', {})
        saved_flags = saved_player.get('flags', {})
        if not isinstance(original_flags, dict):
            original_flags = {}
        if not isinstance(saved_flags, dict):
            saved_flags = {}
        changed_flag_count = sum(
            1 for key in set(original_flags) | set(saved_flags)
            if original_flags.get(key) != saved_flags.get(key)
        )
        message_types = {}
        action_count = 0
        postback_bytes = 0
        for message in result['messages']:
            message_type = message.get('type', 'unknown')
            message_types[message_type] = message_types.get(message_type, 0) + 1
            action_count += len(message.get('actions', []))
            action_count += len(message.get('quick_replies', []))
            action_count += len(message.get('areas', []))
            actions = list(message.get('actions', []))
            actions.extend(message.get('quick_replies', []))
            actions.extend(
                area.get('action', {})
                for area in message.get('areas', []))
            postback_bytes += sum(
                len(action['token'].encode('utf-8'))
                for action in actions
                if (
                    action.get('type') == 'postback'
                    and isinstance(action.get('token'), str)
                )
            )
        _log_event(
            'turn-completed',
            request_id=request_id,
            bot=bot_name,
            conversation=context.user.user_id,
            received_state_id=received_state_id,
            received_revision=received_revision,
            next_state_id=result['state']['id'],
            next_revision=result['state']['revision'],
            scenario_revision=interface.scenario_revision,
            compatibility_epoch=interface.compatibility_epoch,
            scene=saved_player.get('scene'),
            scene_history=saved_player.get('scene_history', []),
            changed_flags=changed_flag_count,
            messages=len(result['messages']),
            message_types=message_types,
            actions=action_count,
            next_state_token_bytes=len(
                result['state_token'].encode('utf-8')),
            postback_token_bytes=postback_bytes,
            elapsed_ms=int((
                time.monotonic()
                - request.environ['webchat.started_at']) * 1000),
        )
        _json_response(result)
    except HTTPResponse:
        raise
    except Exception as error:
        _problem(error, request_id)
