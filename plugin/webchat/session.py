"""一つの署名付きセーブ内で、会話BotとLIFF Botを順に実行する。"""

from collections import deque
import copy
import json
import logging
import time

from plugin.webchat.errors import (
    BotNotWebCompatible, IncompatibleState, InvalidStateToken,
    InvalidWebchatConfiguration, TurnDeadlineExceeded,
)


MAX_SESSION_ACTIONS = 100


class _Output:
    """handle_actionへ渡す実行interfaceは、この3メソッドだけを持つ。"""

    def __init__(self, respond):
        self.respond = respond

    def get_retry_count(self):
        return 0

    def should_raise_exceptions(self):
        return True

    def respond_reaction(self, context, reactions):
        return self.respond(context, reactions)


def session_bots(root, get_bot):
    interface = root.get_interface('webchat')
    bots = {root.name: root}
    for app in interface.liff_apps.values():
        bot = get_bot(app['bot'])
        if (bot is None or bot.get_interface('webchat') is None
                or not bot.get_interface('webchat').enabled
                or bot.get_interface('liff') is None):
            raise InvalidWebchatConfiguration(
                'LIFF連携先には有効なwebchatとliff interfaceが必要です')
        bots[bot.name] = bot
    return bots


def run_session(root, get_bot, root_context, app_id=None):
    interface = root.get_interface('webchat')
    bots = session_bots(root, get_bot)
    payload = root_context.state_payload
    peers = payload.get('peer_players', {})
    epochs = dict(payload.get('peer_epochs', {}))
    for name, epoch in epochs.items():
        if (name not in bots
                or bots[name].get_interface('webchat').compatibility_epoch != epoch):
            raise IncompatibleState('実行済みの連携Botの互換epochが一致しません')
    namespaces = {bot.state_namespace for bot in bots.values()}
    if (not isinstance(peers, dict) or not set(peers) <= namespaces - {root.state_namespace}
            or any(not isinstance(player, dict) for player in peers.values())
            or (peers and not epochs)):
        raise InvalidStateToken('連携Botの状態が不正です')
    players = copy.deepcopy(peers)
    players[root.state_namespace] = root_context.original_player
    target = interface.liff_apps[app_id]['bot'] if app_id is not None else root.name
    service = 'liff' if app_id is not None else 'webchat'
    queue = deque([(target, service, root_context.action)])
    total = 1
    messages = []
    active_response = []
    events = []
    chat_executed = False
    active_generation = None

    def forward(bot_name, action, interface_name=None):
        nonlocal total
        if bot_name not in bots:
            raise BotNotWebCompatible('連携対象外のBotへは転送できません')
        service = interface_name if interface_name is not None else (
            'webchat' if bot_name == root.name else 'liff')
        if (service not in ('webchat', 'liff')
                or (service == 'webchat' and bot_name != root.name)
                or bots[bot_name].get_interface(service) is None):
            raise BotNotWebCompatible('このBotの同期forwardに使えないinterfaceです')
        total += 1
        if total > MAX_SESSION_ACTIONS:
            raise BotNotWebCompatible('同期forwardの実行回数上限を超えました')
        queue.append((bot_name, service, action))

    while queue:
        if root_context.deadline - time.monotonic() <= 0.5:
            raise TurnDeadlineExceeded('同期forwardを含む処理期限を超えました')
        name, service, action = queue.popleft()
        bot = bots[name]
        controller = bot.get_interface('webchat')
        controller.ensure_scenario(bot)
        if root_context.deadline - time.monotonic() <= 0.5:
            raise TurnDeadlineExceeded('Scenario読込後に処理期限を超えました')
        snapshot = players.get(bot.state_namespace, {})
        context = controller.create_context_from_state(
            {**payload, 'player': snapshot}, action, root_context.request_id,
            deadline_seconds=root_context.deadline - time.monotonic())
        context.deadline = root_context.deadline
        context.forward_action = forward
        context.service_name = service
        if service == 'webchat':
            responder = _Output(controller.present_reactions)
        else:
            liff = bot.get_interface('liff')
            context.add_interface('liff', liff)
            context.ignore_unhandled_action = liff.ignore_unhandled_action
            responder = _Output(liff.respond_reaction)
        result = bot.handle_action(context, interface=responder)
        if root_context.deadline - time.monotonic() <= 0.5:
            raise TurnDeadlineExceeded('同期forwardを含む処理期限を超えました')
        players[bot.state_namespace] = context.saved_player
        if name != root.name:
            epochs[name] = controller.compatibility_epoch
        if service == 'webchat':
            for message in result:
                message['id'] = f'{root_context.request_id}:{len(messages)}'
                messages.append(message)
            active_response = result
            active_generation = context.saved_player.get('action_generation')
            chat_executed = True
        else:
            values = json.loads(result)
            if not isinstance(values, list):
                raise BotNotWebCompatible('LIFF応答が配列ではありません')
            events.extend(values)
        logging.info(json.dumps({
            'type': 'XSBWebchat', 'event': 'session-action',
            'request_id': root_context.request_id, 'bot': name, 'service': service,
            'conversation': root_context.user.user_id, 'action': action,
            'scene': context.saved_player.get('scene'),
        }, ensure_ascii=False, separators=(',', ':')))

    root_context._saved_player = players.pop(root.state_namespace)
    generation = root_context.saved_player.get('action_generation')
    if generation != active_generation:
        active_response = []
    response = interface.make_response(root_context, messages, players, epochs)
    response.update({
        'chat_updated': chat_executed or generation != root_context.original_player.get('action_generation'),
        'active_message_ids': [message['id'] for message in active_response],
        'liff_apps': interface.public_liff_apps(),
    })
    if app_id is not None:
        response['liff_result'] = events
    return response
