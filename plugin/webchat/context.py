import copy
import json
import logging
import time

from context import ActionContext
from users import User

from plugin.webchat.errors import BotNotWebCompatible, TurnDeadlineExceeded
from plugin.webchat.state import TokenNextLabelStore, TokenPlayerStatus


DEFAULT_ALLOWED_COMMANDS = frozenset({
    '@image', '@画像', '@video', '@動画', '@rawimage', '@生画像',
    '@or', '@または', '@reset', '@リセット', '@set', '@セット',
    '@if', '@条件', '@else', '@さもなくば', '@elif', '@あるいは',
    '@end', '@終わり', '@seq', '@順々', '@loop', '@ループ',
    '@random', '@ランダム', '@call', '@サブルーチン',
    '@return', '@リターン', '@defer', '@遅延実行',
    '@reset_nodes', '@ノードリセット', '@new_chapter', '@新章',
    '@webhook', '@WebHook', '@postjson', '@PostJSON',
    '@getjson', '@GetJSON', '@log', '@Log',
    '@button', '@ボタン', '@confirm', '@確認',
    '@carousel', '@カルーセル', '@panel', '@パネル',
    '@imagemap', '@イメージマップ', '@reply', '@リプライ',
    '@richmenu', '@リッチメニュー', '@flex', '@フレックス',
    '@audio', '@音声', '@@set_quick_reply_guard',
    '@show_quick_reply_choices',
    '@clear_quick_reply_guard', '@clear_quick_reply_state',
    '@@set_next_label', '@clear_next_label', '@reset_next_label',
})


class WebchatActionContext(ActionContext):
    """署名済みclient stateだけから動作するWebchat context。"""

    def __init__(self, bot_name, interface, conversation_id, action,
                 player_snapshot, attrs=None, deadline_seconds=None):
        user = User('webchat', conversation_id)
        super().__init__(
            bot_name, 'webchat', interface, user, action, attrs or {})
        self._player_snapshot = copy.deepcopy(player_snapshot or {})
        self._saved_player = None
        self.next_label_store = TokenNextLabelStore()
        self.allowed_commands = set(DEFAULT_ALLOWED_COMMANDS)
        self.allowed_commands.update(interface.allowed_commands)
        if deadline_seconds is None:
            deadline_seconds = interface.turn_deadline_seconds
        self.deadline = time.monotonic() + max(0.0, deadline_seconds)

    def load_status(self):
        self.status = TokenPlayerStatus(
            self.bot_name, self.user.user_id, self._player_snapshot)

    def save_status(self):
        self._saved_player = self.status.export()
        self.status.mark_saved()

    def rollback_status(self):
        if self.status is not None:
            self.status.rollback()

    @property
    def saved_player(self):
        if self._saved_player is None:
            return self.status.export()
        return copy.deepcopy(self._saved_player)

    @property
    def original_player(self):
        return copy.deepcopy(self._player_snapshot)

    def check_command_policy(self, command):
        if self.deadline - time.monotonic() <= 0.5:
            raise TurnDeadlineExceeded(
                'Webchat turn deadlineを超えました')
        if command not in self.allowed_commands:
            raise BotNotWebCompatible(
                f'Webchatで許可されていないcommandです: {command}')
        logging.info(json.dumps({
            'type': 'XSBWebchat',
            'event': 'command',
            'request_id': getattr(self, 'request_id', None),
            'conversation': self.user.user_id,
            'command': command,
            'scene': self.status.scene if self.status is not None else None,
        }, ensure_ascii=False, separators=(',', ':')))

    def request_external(self, method, url, **kwargs):
        return self.get_interface('webchat').request_external(
            self, method, url, **kwargs)
