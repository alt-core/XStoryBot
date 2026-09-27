"""ビルド済みの限定Scenarioを、ブラウザ用の命令と照合規則へ変換する。"""

import json
import re
from types import SimpleNamespace

import commands
import utility
from common_commands import (
    AUDIO_CMDS, IMAGE_CMDS, RAWIMAGE_CMDS, VIDEO_CMDS, OR_CMDS, RESET_CMDS,
)
from condition_expr import (
    OPTION_REGEXP_EXACT_MATCH, OPTION_REGEXP_LOWER_CASE, OPTION_REGEXP_NORMALIZE,
)
from plugin.line.command_names import BUTTON_CMDS, CONFIRM_CMDS, IMAGEMAP_CMDS, REPLY_CMDS
from plugin.line.quick_reply import (
    SET_QUICK_REPLY_GUARD_CMDS, CLEAR_QUICK_REPLY_GUARD_CMDS, SHOW_QUICK_REPLY_CHOICES_CMDS,
)
from plugin.webchat.presenter import WebchatPresenter, register_runtime
from plugin.webchat.errors import BotNotWebCompatible


class TrialBuildError(ValueError):
    """台本の対応範囲・書式・参照に関する診断。"""


def source_of(node):
    title, line = node.source_position or (node.tab_name, node.line_no)
    return {'sheet': title, 'line': line + 1}


def fail(message, source):
    location = f"{source['sheet']}!{source['line']}行目" if source else '設定'
    frame = f"（frame: {source['frame']}）" if source and source.get('frame') else ''
    raise TrialBuildError(f'{location}{frame}: {message}')


def build_tables(tables, constants, options, version=3):
    # 通常のScenarioやpickleの形式を変えず、書出し時だけ元の行を保持する。
    from scenario import ScenarioBuilder, ScenarioSyntaxError, INCLUDEIF_REGION_CMDS, FILTER_REGION_CMDS

    class TrialBuilder(ScenarioBuilder):
        def _process_region_operator(self, condition):
            if condition in INCLUDEIF_REGION_CMDS + FILTER_REGION_CMDS:
                fail(f'体験版では条件付きの{condition}を使えません', source_of(self.node))
            return super()._process_region_operator(condition)

        def _process_block_body(self, condition=None):
            if condition is not None:
                condition.trial_source = source_of(self.node)
            return super()._process_block_body(condition)

        def add_command(self, sender, msg, options, children):
            super().add_command(sender, msg, options, children)
            source = source_of(self.node)
            _sender, original = utility.parse_sender(self.node.get_factor(0))
            original = utility.to_hankaku(original).strip()
            if original.startswith('/'):
                original = '@' + original[1:]
            entry = commands.get_command(original, self.version, 'line')
            if entry and hasattr(entry.builder, 'default_frame'):
                source['frame'] = self.node.get_factor(2) or entry.builder.default_frame
            self.lines[-1].trial_source = source

    if version != 3:
        raise TrialBuildError('体験版はscenario_version: 3に対応しています')
    for _title, rows in tables:
        for row in rows:
            path = getattr(row, 'source_path', None)
            if path and getattr(row, 'source_position', None):
                title, line = row.source_position
                row.source_position = (title.removesuffix(f' ({path})'), line)
    try:
        return TrialBuilder.build_from_tables(tables, constants, options=options, version=version)
    except ScenarioSyntaxError as error:
        raise TrialBuildError(str(error)) from error


class PresentationHost:
    """署名・API設定を必要としない、既存presenterの小さな接続先。"""

    def __init__(self, params, media_url):
        self.sender_icon_urls = params.get('sender_icon_urls', {}) or {}
        if not isinstance(self.sender_icon_urls, dict):
            raise TrialBuildError('sender_icon_urlsはmappingにしてください')
        self.alt_text = params.get('alt_text', '選択可能な画像')
        self.reply_fallback_message = params.get('reply_fallback_message', '選択してください')
        self.validate_media_url = media_url
        self._presenter = WebchatPresenter(self)

    @staticmethod
    def preview_image_url(url):
        return re.sub(r'_1024\.', '_240.', url)

    @staticmethod
    def make_postback_action(context, label, resolved_action, echo_text):
        return {'type': 'postback', 'label': label, 'action': resolved_action, 'echo_text': echo_text}

    def present(self, sender, msg, options, children, source):
        context = SimpleNamespace(service_name='webchat', version=3, get_interface=lambda _: self)
        try:
            return self._presenter.present(context, [([sender, msg, *options], children)])
        except (ValueError, BotNotWebCompatible) as error:
            fail(str(error), source)


def compile_scenario(scenario, *, bot, params, constants, quick_reply, media_url, richmenu=None, menu_actions=None, liff=None):
    from scenario import (
        CONDITION_KIND_STRING, CONDITION_KIND_EXPR, CONDITION_KIND_COMMAND,
        INCLUDE_REGION_CMDS, StringFormatter,
    )
    register_runtime()
    host = PresentationHost(params, media_url)
    formatter = StringFormatter()
    values = {'$$bot_name': bot, **constants, **scenario.constants, **params.get('constants', {})}
    if scenario.version != 3:
        raise TrialBuildError('体験版はscenario_version: 3に対応しています')

    def freeze(value, source):
        if not isinstance(value, str):
            fail('体験版では実行時の式を使えません', source)
        for _text, field, _format, _conversion in formatter.parse(value):
            if field is not None:
                root = re.split(r'[.\[]', field, 1)[0]
                if not root or root.isdecimal() or (root.startswith('$$') and root != '$$bot_name'):
                    fail(f'実行時の参照は使えません: {field}', source)
        try:
            return formatter.vformat(value, (), values)
        except (KeyError, IndexError, ValueError, TypeError, AttributeError) as error:
            fail(f'定数・書式を展開できません: {error}', source)

    def predicate(condition):
        source = getattr(condition, 'trial_source', None)
        if condition.guards:
            fail('体験版では条件セルのguardを使えません', source)
        if condition.kind == CONDITION_KIND_STRING:
            return {'kind': 'exact', 'value': condition.value}
        if condition.kind != CONDITION_KIND_EXPR:
            fail('体験版で使えない入力条件です', source)
        node = condition.value.expr
        while not node.is_terminal:
            children = [child for child in node.children if child.name != 'EOF']
            if not children:
                return {'kind': 'never'}
            if len(children) != 1:
                fail('体験版では条件セルのAND／OR結合を使えません', source)
            node = children[0]
        if node.name == 'string_match':
            return {'kind': 'contains', 'value': node.value}
        if node.name == 'regex_match':
            regex, flags = node.value
            return {'kind': 'regex', 'value': regex.pattern,
                    'ignore_case': bool(regex.flags & re.IGNORECASE),
                    'normalize': OPTION_REGEXP_NORMALIZE in flags,
                    'lower': OPTION_REGEXP_LOWER_CASE in flags,
                    'exact': OPTION_REGEXP_EXACT_MATCH in flags}
        fail('体験版で使えない入力条件です', source)

    regions = {region.get_fullpath(): region for scene in scenario.scenes.values() for region in scene.regions}
    blocks = {}
    conditions = {}
    sources = {}
    display_commands = IMAGE_CMDS + RAWIMAGE_CMDS + VIDEO_CMDS + AUDIO_CMDS + BUTTON_CMDS + CONFIRM_CMDS + IMAGEMAP_CMDS
    for region_name, region in regions.items():
        for index, (condition, lines) in enumerate(region.blocks):
            key = f'{region_name}:{index}'
            source = getattr(condition, 'trial_source', None)
            sources[key] = source
            if condition.guards:
                fail('体験版では条件セルのguardを使えません', source)
            if condition.kind == CONDITION_KIND_COMMAND:
                if condition.value not in INCLUDE_REGION_CMDS:
                    fail(f'体験版では{condition.value}を使えません', source)
            else:
                conditions[key] = predicate(condition)
            ops = []
            for command in lines:
                src = getattr(command, 'trial_source', source)
                msg = freeze(command.msg, src)
                allowed = (not msg.startswith('@') or msg in display_commands + OR_CMDS + RESET_CMDS + REPLY_CMDS
                           or msg in SET_QUICK_REPLY_GUARD_CMDS + CLEAR_QUICK_REPLY_GUARD_CMDS + SHOW_QUICK_REPLY_CHOICES_CMDS)
                if not allowed:
                    fail(f'体験版で使えない命令です: {msg}', src)
                args = [freeze(value, src) for value in command.options or []]
                children = [[freeze(value, src) for value in row] for row in command.children or []]
                if msg.startswith(('*', '#')):
                    op = {'op': 'jump', 'action': msg}
                elif msg in OR_CMDS:
                    op = {'op': 'next'}
                elif msg in RESET_CMDS:
                    op = {'op': 'reset'}
                elif msg in SET_QUICK_REPLY_GUARD_CMDS:
                    op = {'op': 'wait', 'wait': {'label': args[0], 'retry': args[1], 'choices': json.loads(args[2]), 'guard': len(args) < 4 or args[3] == 'True'}}
                elif msg in CLEAR_QUICK_REPLY_GUARD_CMDS:
                    op = {'op': 'clear_wait', 'label': args[0] if args else None}
                elif msg in SHOW_QUICK_REPLY_CHOICES_CMDS:
                    op = {'op': 'show_choices'}
                elif msg in REPLY_CMDS:
                    reply = host.present(command.sender, msg, args, children, src)[0]
                    op = {'op': 'reply', 'actions': reply.pop('quick_replies'), 'fallback': reply}
                else:
                    messages = host.present(command.sender, msg, args, children, src)
                    op = {'op': 'emit', 'messages': messages, 'text': msg if not msg.startswith('@') else None}
                op['source'] = src
                ops.append(op)
            blocks[key] = {'ops': ops, 'next': f'{region_name}:{index + 1}' if index + 1 < len(region.blocks) else None}

    def walk(scene, region, visited):
        visited.add(scene.get_fullpath())
        for index, (condition, _lines) in enumerate(region.blocks):
            key = f'{region.get_fullpath()}:{index}'
            if condition.kind == CONDITION_KIND_COMMAND:
                target = scenario.scenes[condition.options[0][1:]]
                if target.get_fullpath() not in visited:
                    for child in target.regions:
                        yield from walk(target, child, visited)
            else:
                yield {'test': conditions[key], 'block': key, 'source': sources[key]}

    scenes = {}
    startup = scenario.scenes[scenario.startup_scene_title]
    for scene in scenario.scenes.values():
        if scene.get_fullpath() in scenes:
            continue
        rules, seen = [], set()
        for region in [startup.regions[0]] + scene.regions + startup.regions[1:]:
            if region in seen:
                continue
            seen.add(region)
            rules.extend(walk(scene, region, set()))
        scenes[scene.get_fullpath()] = {'tab': scene.tab_name, 'entry': scene.get_entrypoint_label(), 'rules': rules}

    epoch, start = params.get('scenario_compatibility_epoch'), params.get('start_action')
    if not isinstance(epoch, str) or not epoch or not isinstance(start, str) or not start:
        raise TrialBuildError('scenario_compatibility_epochとstart_actionを指定してください')
    result = {'schema_version': 1, 'bot': bot, 'epoch': epoch, 'start_action': start,
              'startup': scenario.startup_scene_title, 'scenes': scenes, 'blocks': blocks,
              'reset_keyword': quick_reply.get('reset_keyword', ''),
              'ignore_pattern': quick_reply.get('ignore_pattern') or None,
              'richmenu': richmenu, 'menu_actions': menu_actions or [], 'liff': liff,
              'liff_apps': params.get('liff_apps', {}) or {}}
    validate_references(result)
    return result


def validate_references(program):
    """存在しない固定参照を診断する。シーン別の到達可能性解析は行わない。"""
    scenes = program['scenes']
    labels = {rule['test']['value'] for scene in scenes.values() for rule in scene['rules'] if rule['test']['kind'] == 'exact'}

    def check(action, source=None):
        if not isinstance(action, str):
            fail('移動先は文字列にしてください', source)
        if action.startswith('#') and action not in labels:
            fail(f'移動先のラベルがありません: {action}', source)
        if action.startswith('*'):
            match = re.fullmatch(r'\*([^#]+)(#.*)?', action)
            if not match:
                fail(f'シーン移動の書式が不正です: {action}', source)
            name, label = match.groups()
            found = [name] if '/' in name else [f"{scene['tab']}/{name}" for scene in scenes.values()]
            if not any(target in scenes for target in found):
                fail(f'移動先のシーンがありません: {action}', source)
            if label and label not in labels:
                fail(f'移動先のラベルがありません: {action}', source)

    def actions(value, source):
        if isinstance(value, dict):
            if value.get('type') == 'postback':
                check(value['action'], source)
            for child in value.values():
                actions(child, source)
        elif isinstance(value, list):
            for child in value:
                actions(child, source)

    check(program['start_action'])
    for block in program['blocks'].values():
        for op in block['ops']:
            source = op['source']
            if op['op'] == 'jump':
                check(op['action'], source)
            if op['op'] == 'wait':
                if op['wait']['guard']:
                    check(op['wait']['retry'], source)
                check(op['wait']['label'] + '0', source)
            actions(op, source)
    for action in program['menu_actions']:
        if action['type'] == 'postback':
            check(action['data'])
