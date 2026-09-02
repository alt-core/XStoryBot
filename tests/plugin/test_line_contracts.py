# coding: utf-8
"""LINE プラグインの公開版で維持する局所的な契約を確認する。"""

import base64
import hashlib
import hmac
import importlib.util
import io
import json
import re
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from cloud_backend import factory as backend_factory
import utility as utility_module


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MISSING = object()


def _module(name, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _load_module(name, relative_path, replacements):
    """外部依存を一時的に差し替え、production module だけを読み込む。"""
    previous = {key: sys.modules.get(key, _MISSING) for key in replacements}
    previous[name] = sys.modules.get(name, _MISSING)
    try:
        sys.modules.update(replacements)
        spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / relative_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in {**previous}.items():
            if value is _MISSING:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


class _Model:
    type_name = None

    def __init__(self, *args, **kwargs):
        self.args = args
        for key, value in kwargs.items():
            setattr(self, key, value)
        if self.type_name is not None and not hasattr(self, 'type'):
            self.type = self.type_name


def _model_class(name, type_name=None):
    return type(name, (_Model,), {'type_name': type_name})


class _TextSendMessage(_Model):
    def __init__(self, text=None, **kwargs):
        super().__init__(text=text, **kwargs)


class _MessageAction(_Model):
    def __init__(self, label=None, text=None, **kwargs):
        super().__init__(label=label, text=text, **kwargs)


class _QuickReply(_Model):
    def __init__(self, items=None, **kwargs):
        super().__init__(items=items, **kwargs)


def _linebot_stubs():
    """line-bot-sdk の型判定と生成に必要な最小限の型を返す。"""
    class InvalidSignatureError(Exception):
        pass

    classes = {
        'MessageEvent': _model_class('MessageEvent', 'message'),
        'PostbackEvent': _model_class('PostbackEvent', 'postback'),
        'VideoPlayCompleteEvent': _model_class(
            'VideoPlayCompleteEvent', 'videoPlayComplete'),
        'BeaconEvent': _model_class('BeaconEvent', 'beacon'),
        'FollowEvent': _model_class('FollowEvent', 'follow'),
        'UnfollowEvent': _model_class('UnfollowEvent', 'unfollow'),
        'JoinEvent': _model_class('JoinEvent', 'join'),
        'LeaveEvent': _model_class('LeaveEvent', 'leave'),
        'MemberJoinedEvent': _model_class('MemberJoinedEvent', 'memberJoined'),
        'MemberLeftEvent': _model_class('MemberLeftEvent', 'memberLeft'),
        'TextMessage': _model_class('TextMessage', 'text'),
        'ImageMessage': _model_class('ImageMessage', 'image'),
        'VideoMessage': _model_class('VideoMessage', 'video'),
        'AudioMessage': _model_class('AudioMessage', 'audio'),
        'FileMessage': _model_class('FileMessage', 'file'),
        'LocationMessage': _model_class('LocationMessage', 'location'),
        'StickerMessage': _model_class('StickerMessage', 'sticker'),
        'TextSendMessage': _TextSendMessage,
        'QuickReply': _QuickReply,
        'MessageAction': _MessageAction,
    }
    for name in (
            'ImageSendMessage', 'VideoSendMessage', 'AudioSendMessage',
            'TemplateSendMessage',
            'CarouselColumn', 'ImagemapSendMessage', 'ImagemapArea',
            'MessageImagemapAction', 'Sender', 'ButtonsTemplate',
            'ConfirmTemplate', 'CarouselTemplate', 'PostbackAction',
            'URIAction', 'URIImagemapAction', 'BaseSize', 'QuickReplyButton',
            'FlexSendMessage'):
        classes[name] = _model_class(name)

    models = _module('linebot.models', **classes)
    linebot = _module(
        'linebot',
        LineBotApi=_model_class('LineBotApi'),
        WebhookParser=_model_class('WebhookParser'),
    )
    exceptions = _module('linebot.exceptions', InvalidSignatureError=InvalidSignatureError)
    return {
        'linebot': linebot,
        'linebot.models': models,
        'linebot.exceptions': exceptions,
    }, SimpleNamespace(**classes, InvalidSignatureError=InvalidSignatureError)


def _base_stubs():
    commands = _module(
        'commands',
        CommandEntry=lambda **kwargs: SimpleNamespace(**kwargs),
        Default_Builder=lambda: object(),
        register_commands=Mock(),
        register_command=Mock(),
        invoke_runtime_construct_response=Mock(return_value=False),
    )
    utility = _module(
        'utility',
        safe_list_get=lambda values, index, default=None: (
            values[index] if index < len(values) else default),
        merge_params=lambda base, extra: {**base, **extra},
        extract_params=lambda values, names: {
            name: values[name] for name in names if name in values},
        deep_dump=Mock(),
        parse_url=lambda value: re.match(r'^(https?|tel):', value or ''),
        encode_action_string=lambda value, **_kwargs: value,
        decode_action_string=lambda value: (value, {}),
        encode_line_video_tracking_id=(
            utility_module.encode_line_video_tracking_id),
        decode_line_video_tracking_id=(
            utility_module.decode_line_video_tracking_id),
        sanitize_action=lambda value: (
            ' ' + value if value.startswith(('*', '＊', '#', '＃', ':', '：')) else value),
    )
    return {
        'hub': _module('hub', register_interface_factory=Mock(), register_handler=Mock()),
        'commands': commands,
        'utility': utility,
    }


class _HttpAbort(Exception):
    def __init__(self, status, body):
        super().__init__(status, body)
        self.status = status
        self.body = body


class _Bottle:
    def post(self, _path):
        return lambda function: function


class LineWebhookContractTest(unittest.TestCase):
    def setUp(self):
        self.secret = 'channel-secret'
        self.trace = []
        self.request = SimpleNamespace(headers={}, body=io.BytesIO())
        self.response = SimpleNamespace(content_type=None)

        class SignatureParser:
            def parse(parser_self, body, signature):
                self.trace.append('parse')
                expected = base64.b64encode(hmac.new(
                    self.secret.encode('utf-8'), body.encode('utf-8'), hashlib.sha256
                ).digest()).decode('ascii')
                if not hmac.compare_digest(signature, expected):
                    raise self.types.InvalidSignatureError()
                return [SimpleNamespace(timestamp=None)]

        self.parser = SignatureParser()
        self.interface = SimpleNamespace(
            parser=self.parser,
            line_abort_duration_ms=0,
            line_abort_duration_dont_break=False,
            create_context_from_line_event=Mock(return_value=None),
        )
        self.bot = SimpleNamespace(
            get_interface=Mock(return_value=self.interface),
            check_reload=Mock(side_effect=lambda: self.trace.append('reload')),
            handle_action=Mock(),
        )
        linebot, self.types = _linebot_stubs()
        bottle = _module(
            'bottle', request=self.request, response=self.response, Bottle=_Bottle,
            abort=lambda status, body: (_ for _ in ()).throw(_HttpAbort(status, body)),
        )
        utility = _module(
            'utility',
            make_error_json=lambda status, message: json.dumps({'status': status, 'message': message}),
            make_ok_json=lambda message: json.dumps({'message': message}),
        )
        replacements = {
            **linebot,
            'bottle': bottle,
            'auth': _module('auth'),
            'utility': utility,
            'main': _module('main', get_bot=Mock(return_value=self.bot)),
            'users': _module('users'),
        }
        self.webapi = _load_module(
            '_line_contract_webapi', 'plugin/line/webapi.py', replacements)

    def _set_request(self, body, signature=_MISSING):
        self.request.body = io.BytesIO(body.encode('utf-8'))
        self.request.headers = {}
        if signature is not _MISSING:
            self.request.headers['X-Line-Signature'] = signature

    def test_missing_signature_is_rejected_before_parser_and_log(self):
        self._set_request('{"events":[]}')
        with patch.object(self.webapi.logging, 'info') as info:
            with self.assertRaises(_HttpAbort) as error:
                self.webapi.callback('bot')
        self.assertEqual(401, error.exception.status)
        self.assertEqual([], self.trace)
        info.assert_not_called()

    def test_nonempty_invalid_signature_is_rejected_by_parser_without_body_log(self):
        self._set_request('{"events":[]}', 'invalid-signature')
        with patch.object(self.webapi.logging, 'info') as info:
            with self.assertRaises(_HttpAbort) as error:
                self.webapi.callback('bot')
        self.assertEqual(401, error.exception.status)
        self.assertEqual(['parse'], self.trace)
        info.assert_not_called()

    def test_valid_signature_logs_body_only_after_parse_then_reloads(self):
        body = '{"events":[]}'
        signature = base64.b64encode(hmac.new(
            self.secret.encode('utf-8'), body.encode('utf-8'), hashlib.sha256
        ).digest()).decode('ascii')
        self._set_request(body, signature)
        with patch.object(
                self.webapi.logging, 'info',
                side_effect=lambda *_args, **_kwargs: self.trace.append('log')) as info:
            result = self.webapi.callback('bot')
        self.assertEqual(['parse', 'log', 'reload'], self.trace)
        info.assert_called_once_with('Request body: {}'.format(body))
        self.assertEqual({'message': 'OK'}, json.loads(result))


class LineInterfaceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        linebot, cls.types = _linebot_stubs()
        replacements = {
            **linebot,
            **_base_stubs(),
            'common_commands': _module(
                'common_commands', AUDIO_CMDS=('@audio',),
                IMAGE_CMDS=('@image',), VIDEO_CMDS=('@video',),
                RAWIMAGE_CMDS=('@rawimage',)),
            'context': _module('context', ActionContext=object),
            'users': _module('users', User=_model_class('User')),
            'requests': _module('requests', RequestException=Exception),
        }
        cls.interface_module = _load_module(
            '_line_contract_interface', 'plugin/line/interface.py', replacements)

    def setUp(self):
        self.interface = object.__new__(self.interface_module.LinePlugin_Interface)
        self.interface.allow_special_action_text_for_debug = False
        self.interface.line_bot_api = Mock()
        self.interface.sender_icon_urls = {}

    def _message_event(self, message):
        return self.types.MessageEvent(message=message)

    def test_audio_reactionをLINE送信messageへ変換する(self):
        context = SimpleNamespace(response=None)
        messages = self.interface._construct_responses(
            context,
            [([None, '@audio', 'https://media.example/audio.mp3',
               1234, 'audio/mpeg'], None)],
        )
        self.assertEqual(1, len(messages))
        self.assertIsInstance(messages[0], self.types.AudioSendMessage)
        self.assertEqual(
            'https://media.example/audio.mp3',
            messages[0].original_content_url,
        )
        self.assertEqual(1234, messages[0].duration)

    def test_video完了actionを世代付きtracking_IDへ変換する(self):
        context = SimpleNamespace(
            response=None,
            source_type='user',
            status=SimpleNamespace(action_token='Generation'),
        )
        messages = self.interface._construct_responses(
            context,
            [([None, '@video', 'https://media.example/poster.png',
               'https://media.example/video.mp4', '*完了'], None)],
        )
        expected = utility_module.encode_line_video_tracking_id(
            '*完了', 'Generation')
        self.assertLessEqual(len(expected), 100)
        self.assertRegex(
            expected, r'^[a-zA-Z0-9\-.=,+*()%$&;:@{}!?<>\[\]]+$')
        self.assertEqual(expected, messages[0].tracking_id)

        event = self.types.VideoPlayCompleteEvent(
            video_play_complete=SimpleNamespace(tracking_id=expected))
        action, attrs = self.interface._construct_action(event)
        self.assertEqual('*完了', action)
        self.assertEqual('Generation', attrs['action_token'])

    def test_group動画では完了actionを付けない(self):
        context = SimpleNamespace(
            response=None,
            source_type='group',
            status=SimpleNamespace(action_token='Generation'),
        )
        with patch.object(self.interface_module.logging, 'warning') as warning:
            messages = self.interface._construct_responses(
                context,
                [([None, '@video', 'https://media.example/poster.png',
                   'https://media.example/video.mp4', '*完了'], None)],
            )
        self.assertIsNone(messages[0].tracking_id)
        warning.assert_called_once()

    def test_不正な旧video_tracking_IDは本文を出さず無視する(self):
        event = self.types.VideoPlayCompleteEvent(
            video_play_complete=SimpleNamespace(tracking_id='旧形式'))
        with patch.object(self.interface_module.logging, 'warning') as warning:
            action, _attrs = self.interface._construct_action(event)
        self.assertIsNone(action)
        self.assertNotIn('旧形式', str(warning.call_args_list))

    def test_all_scenario_versions_use_same_latest_internal_action_mapping(self):
        provider = SimpleNamespace(type='line')
        cases = (
            (self._message_event(self.types.LocationMessage(
                title='題', latitude=35.0, longitude=139.0, address='住所')),
             ':LINE_LOCATION:題,35.0,139.0,住所'),
            (self._message_event(self.types.StickerMessage(package_id='1', sticker_id='2')),
             ':LINE_STICKER:1,2'),
            (self._message_event(self.types.ImageMessage(id='image-id', content_provider=provider)),
             ':LINE_IMAGE:image-id'),
            (self._message_event(self.types.VideoMessage(
                id='video-id', duration=1200, content_provider=provider)),
             ':LINE_VIDEO:video-id,1200'),
            (self._message_event(self.types.AudioMessage(
                id='audio-id', duration=800, content_provider=provider)),
             ':LINE_AUDIO:audio-id,800'),
            (self._message_event(self.types.FileMessage(
                id='file-id', file_name='name.txt', file_size=42)),
             ':LINE_FILE:file-id,name.txt,42'),
            (self._message_event(self.types.ImageMessage(
                id='external-id', content_provider=SimpleNamespace(type='external'))),
             ':LINE_ETC:image'),
            (self.types.BeaconEvent(beacon=SimpleNamespace(type='enter', hwid='beacon-id')),
             ':LINE_BEACON:enter,beacon-id'),
        )
        for version in (1, 2, 3):
            self.interface.params = {'scenario_version': version}
            for event, expected in cases:
                with self.subTest(version=version, expected=expected):
                    self.assertEqual(expected, self.interface._construct_action(event)[0])

    def test_text_that_looks_like_internal_action_is_sanitized(self):
        event = self._message_event(self.types.TextMessage(text=':LINE_IMAGE:spoof'))
        self.assertEqual(' :LINE_IMAGE:spoof', self.interface._construct_action(event)[0])
        ordinary = self._message_event(self.types.TextMessage(text='こんにちは'))
        self.assertEqual('こんにちは', self.interface._construct_action(ordinary)[0])

    def test_generated_message_is_logged_when_event_has_no_reply_token(self):
        context = SimpleNamespace(event=SimpleNamespace(type='unfollow'))
        messages = ['生成本文']
        with patch.object(self.interface_module.logging, 'info') as info:
            self.interface._reply_message(context, messages)
        info.assert_called_once_with(
            "event unfollow doesnt have reply_token: ['生成本文']")
        self.interface.line_bot_api.reply_message.assert_not_called()
        self.interface.line_bot_api.push_message.assert_not_called()


class LineDefaultCommandsContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        linebot, cls.types = _linebot_stubs()
        replacements = {**linebot, **_base_stubs()}
        cls.module = _load_module(
            '_line_contract_default_commands', 'plugin/line/default_commands.py', replacements)

    def test_reply_without_preceding_message_uses_configured_fallback(self):
        runtime = self.module.LineDefaultCommandsPlugin_Runtime({
            'alt_text': 'alt', 'reply_fallback_message': '代替メッセージ'})
        context = SimpleNamespace(
            response=[], status=SimpleNamespace(action_token='token'))
        runtime.construct_response(context, None, '@reply', [], [['選択肢', '返答']])
        self.assertEqual(1, len(context.response))
        self.assertEqual('代替メッセージ', context.response[0].text)
        self.assertEqual(1, len(context.response[0].quick_reply.items))

        # 連続した @reply ではフォールバック本文を重複生成しない。
        first_message = context.response[0]
        runtime.construct_response(context, None, '@reply', [], [['別の選択肢', '別の返答']])
        self.assertEqual([first_message], context.response)

    def test_reply_attaches_to_existing_message(self):
        runtime = self.module.LineDefaultCommandsPlugin_Runtime({'alt_text': 'alt'})
        message = self.types.TextSendMessage(text='本文')
        context = SimpleNamespace(
            response=[message], status=SimpleNamespace(action_token='token'))
        runtime.construct_response(context, None, '@reply', [], [['選択肢', '返答']])
        self.assertEqual([message], context.response)
        self.assertEqual('本文', message.text)
        self.assertEqual(1, len(message.quick_reply.items))


def _plugin_packages(default_commands=None, more=None, quick_reply=None):
    plugin = _module('plugin')
    plugin.__path__ = []
    line = _module('plugin.line')
    line.__path__ = []
    plugin.line = line
    replacements = {'plugin': plugin, 'plugin.line': line}
    if default_commands is not None:
        command_names = _module(
            'plugin.line.command_names',
            IMAGEMAP_CMDS=getattr(
                default_commands, 'IMAGEMAP_CMDS', ('@imagemap',)),
            REPLY_CMDS=getattr(
                default_commands, 'REPLY_CMDS', ('@reply',)),
        )
        line.command_names = command_names
        replacements['plugin.line.command_names'] = command_names
    for name, value in (
            ('default_commands', default_commands), ('more', more),
            ('quick_reply', quick_reply)):
        if value is not None:
            setattr(line, name, value)
            replacements['plugin.line.' + name] = value
    return replacements


class FirestoreAndImageTextContractTest(unittest.TestCase):
    def _load_modules(self):
        self.models_db = Mock(name='models_db')
        self.more_db = Mock(name='more_db')
        self.image_db = Mock(name='image_db')
        self.client = Mock(side_effect=[
            self.models_db, self.more_db, self.image_db])
        firestore = _module(
            'google.cloud.firestore', Client=self.client,
            transactional=lambda function: function)
        google = _module('google')
        google.__path__ = []
        cloud = _module('google.cloud', firestore=firestore)
        cloud.__path__ = []
        google.cloud = cloud
        base = _base_stubs()
        default_commands = _module(
            'plugin.line.default_commands', IMAGEMAP_CMDS=('@imagemap',),
            REPLY_CMDS=('@reply',))
        packages = _plugin_packages(default_commands=default_commands)
        common = {
            **base, **packages,
            'google': google, 'google.cloud': cloud,
            'google.cloud.firestore': firestore,
        }
        with patch.object(backend_factory, '_provider', 'gcp'):
            models_module = _load_module(
                '_line_contract_models', 'models.py', common)
            more_module = _load_module(
                '_line_contract_more', 'plugin/line/more.py', common)

            renderer = _module(
                'plugin.render_text.renderer', render_text_to_png=Mock())
            render_text = _module('plugin.render_text', renderer=renderer)
            packages = _plugin_packages(
                default_commands=default_commands, more=more_module,
                quick_reply=_module(
                    'plugin.line.quick_reply', append_quick_reply=Mock()))
            packages['plugin'].render_text = render_text
            image_replacements = {
                **base, **packages,
                'plugin.render_text': render_text,
                'plugin.render_text.renderer': renderer,
                'google': google, 'google.cloud': cloud,
                'google.cloud.firestore': firestore,
            }
            image_module = _load_module(
                '_line_contract_image_text',
                'plugin/line/image_text.py', image_replacements)
        return models_module, more_module, image_module, renderer

    def test_each_module_owns_an_import_time_firestore_client(self):
        models_module, more_module, image_module, _renderer = self._load_modules()
        self.assertEqual(3, self.client.call_count)
        self.assertIs(
            self.models_db, models_module.get_state_store().client)
        self.assertIs(self.more_db, more_module._state_store.client)
        self.assertIs(self.image_db, image_module._state_store.client)

    def test_player_next_label_uses_full_status_id_as_document_id(self):
        _models_module, more_module, _image_module, _renderer = self._load_modules()
        collection = Mock()
        document = Mock()
        document.get.return_value = SimpleNamespace(exists=False)
        collection.document.return_value = document
        self.more_db.collection.return_value = collection
        self.more_db.transaction.return_value = Mock()
        status = SimpleNamespace(id='shared:line:user,U1')

        more_module.PlayerNextLabelDB.set_next_label('##NEXT', '続きを読む', status)
        self.assertEqual((None, None), more_module.PlayerNextLabelDB.get_next_label(status))
        self.assertEqual(
            (None, None),
            more_module.PlayerNextLabelDB.compare_and_clear_next_label(status, '##NEXT'))
        more_module.PlayerNextLabelDB.clear_next_label(status)

        self.assertEqual([call(status.id)] * 4, collection.document.call_args_list)
        self.assertEqual(
            [call('player_next_labels')] * 4,
            self.more_db.collection.call_args_list)

    def test_image_text_cache_and_build_use_mocked_storage_and_renderer(self):
        _models_module, _more_module, image_module, renderer = self._load_modules()
        collection = Mock()
        document = Mock()
        collection.document.return_value = document
        self.image_db.collection.return_value = collection
        frame_opt = '{"frame":"default"}'
        document.get.return_value = SimpleNamespace(
            exists=True,
            to_dict=lambda: {
                'text': '本文', 'frame_opt': frame_opt, 'url': 'https://cached',
                'width': 100, 'height': 80, 'rest': None,
            })
        self.assertEqual(
            ('https://cached', (100, 80), None),
            image_module.ImageTextStatDB.get_cached_image_text_stat('本文', frame_opt))

        renderer.render_text_to_png.return_value = (b'png-data', None)
        builder = Mock()
        builder.option_force = True
        builder.scene.get_relative_position_desc.return_value = 'scene:1'
        builder.build_image_for_imagemap_command_with_rawdata.return_value = (
            'https://built', (100, 100))
        plugin = image_module.LineImageTextPlugin_Builder({
            'more_message': '続きを読む',
            'more_image_url': 'https://more',
            'frames': {'default': {
                'size_x': 100, 'size_y': 100, 'more_mode': 'inner'}},
            'default_frame': 'default',
        })
        self.assertTrue(plugin.build_from_command(
            builder, None, '@imagetext', ['画像本文']))
        renderer.render_text_to_png.assert_called_once()
        builder.build_image_for_imagemap_command_with_rawdata.assert_called_once()
        document.set.assert_called_once()
        self.assertIn(
            call(None, '@@set_next_label', ['##IMGTEXT__scene:1__0', '続きを読む'], None),
            builder.add_command.call_args_list)


class QuickReplyContractTest(unittest.TestCase):
    def _load(self, filename):
        linebot, _types = _linebot_stubs()
        default_commands = _module(
            'plugin.line.default_commands', REPLY_CMDS=('@reply',))
        replacements = {
            **linebot, **_base_stubs(),
            **_plugin_packages(default_commands=default_commands),
        }
        replacements['utility'].parse_sender = lambda message: (None, message)
        return _load_module(
            '_line_contract_' + filename, 'plugin/line/' + filename + '.py', replacements)

    def test_v3_builder_and_runtime_keep_guard_and_choice_mapping(self):
        module = self._load('quick_reply')
        builder = Mock()
        builder.make_control_flow_refernce_label.side_effect = ['##CF0', '##CF1']
        module.append_quick_reply(
            builder, '##Q_', ['選択1', '選択2=>表示2'], '話者', '##PLEASE')
        self.assertIn(
            call('話者', '@reply', [], [
                ['選択1', '##Q_1'], ['選択2', '表示2', '##Q_2']]),
            builder.add_command.call_args_list)
        self.assertIn(
            call('話者', '@@set_quick_reply_guard', [
                '##Q_', '##PLEASE',
                json.dumps([['選択1', '##Q_1'], ['選択2', '表示2', '##Q_2']]),
                'True'], None),
            builder.add_command.call_args_list)
        builder.start_control_flow.assert_called_once_with('quick_reply')
        builder.add_new_control_flow_block.assert_called_once_with()

        runtime = module.LineQuickReplyPlugin_Runtime({
            'default_reply': '既定', 'please_select_quick_reply_label': '##PLEASE'})
        status = {}
        context = SimpleNamespace(status=status)
        runtime.run_command(context, None, '@@set_quick_reply_guard', [
            '##Q_', '##PLEASE',
            json.dumps([['選択1', '##Q_1'], ['選択2', '表示2', '##Q_2']]),
            'True'])
        self.assertEqual('##Q_2', runtime.modify_incoming_action(context, '選択2'))
        self.assertEqual('##PLEASE', runtime.modify_incoming_action(context, '想定外'))
        self.assertEqual(1, status[module.QUICK_REPLY_GUARD_VARIABLE]['retry_count'])

    def test_v2_builder_and_runtime_keep_retry_branch(self):
        module = self._load('quick_reply_v2')
        builder = Mock()
        module.append_quick_reply(
            builder, '##Q_', ['選択1', '選択2=>表示2'], '話者', '再選択')
        self.assertIn(
            call('話者', '@reply', [], [
                ['選択1', '##Q_1'], ['選択2', '表示2', '##Q_2']]),
            builder.add_command.call_args_list)
        self.assertIn(call('話者', '@@set_quick_reply_guard', ['##Q_'], None),
                      builder.add_command.call_args_list)
        self.assertIn(call('話者', '再選択', [], None), builder.add_command.call_args_list)
        self.assertIn(call('##Q_R'), builder.add_new_string_block.call_args_list)
        builder.add_new_anonymous_block.assert_called_once_with()

        runtime = module.LineQuickReplyPlugin_Runtime({
            'default_reply': '既定', 'retry_message': '再選択'})
        context = SimpleNamespace(status={})
        runtime.run_command(context, None, '@@set_quick_reply_guard', ['##Q_'])
        self.assertEqual('##Q_R', runtime.modify_incoming_action(context, '想定外'))
        runtime.run_command(context, None, '@clear_quick_reply_guard', [])
        self.assertNotIn(module.QUICK_REPLY_GUARD_VARIABLE, context.status)


if __name__ == '__main__':
    unittest.main()
