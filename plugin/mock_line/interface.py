import random
import time
import logging
import uuid
from requests import RequestException

from context import ActionContext
from users import User
import hub
import utility

class MockLinePlugin_ActionContext(ActionContext):
    def __init__(self, bot_name, interface, user, action, attrs, event):
        self.event = event
        if user.user_id.startswith("user,"):
            source_type, source_id = "user", user.user_id.split(",")[1]
        else:
            source_type, source_id = user.user_id.split(",")
        self.source_type = source_type
        self.source_id = source_id
        ActionContext.__init__(self, bot_name, "mock_line", interface, user, action, attrs)

class MockLinePlugin_Interface:
    def __init__(self, bot_name, params):
        self.bot_name = bot_name
        self.params = params
        self.error_rate = float(params.get('error_rate', 0.05))
        self.rate_limit_threshold = int(params.get('rate_limit_threshold', 100))
        self.request_count = 0
        self.request_times = []  # レート制限計算用
        self.logging_enabled = params.get('logging_enabled', True)

        # メッセージ検証機能のための変数
        self.sent_messages = []

        # テスト制御可能なエラー注入機能
        self.force_error = None
        self.error_count = 0

        if self.logging_enabled:
            logging.info(f"[MOCK] Initialized MockLinePlugin_Interface with error_rate={self.error_rate}, rate_limit_threshold={self.rate_limit_threshold}")

    def get_service_list(self):
        return {'mock_line': self}

    def get_retry_count(self):
        return self.params.get('retry_count', 3)

    def create_context(self, user, action, attrs):
        return MockLinePlugin_ActionContext(self.bot_name, self, user, action, attrs, event=None)

    def _simulate_error(self):
        # 強制エラーの確認
        if self.force_error:
            return True, self.force_error

        # ランダムエラーのシミュレーション
        if random.random() < self.error_rate:
            error_type = random.choice(['timeout', 'server_error', 'client_error'])
            return True, error_type
        return False, None

    def set_force_error(self, error_type=None):
        """特定のエラーを強制する（テスト用）
        error_type: 'timeout', 'server_error', 'client_error', 'rate_limit'のいずれか
                    None を指定すると強制エラーを解除
        """
        self.force_error = error_type
        return self

    def respond_reaction(self, context, reactions):
        self.request_count += 1
        now = time.time()

        # 直近1秒間のリクエスト数をカウント
        self.request_times = [t for t in self.request_times if now - t < 1.0]
        self.request_times.append(now)

        # 強制的なレート制限エラー
        if self.force_error == 'rate_limit':
            if self.logging_enabled:
                logging.warning("[MOCK] Forcing rate limit error")
            self.error_count += 1
            mock_response = type('obj', (object,), {'status_code': 429})
            mock_exception = RequestException(response=mock_response)
            raise mock_exception

        # レート制限のシミュレーション - 1秒あたりrate_limit_threshold件を超えたらエラー
        if len(self.request_times) >= self.rate_limit_threshold:
            if self.logging_enabled:
                logging.warning(f"[MOCK] Rate limit exceeded: {len(self.request_times)} requests/sec")
            mock_response = type('obj', (object,), {'status_code': 429})
            mock_exception = RequestException(response=mock_response)
            raise mock_exception

        # エラーシミュレーション
        should_error, error_type = self._simulate_error()
        if should_error:
            self.error_count += 1
            if error_type == 'timeout':
                if self.logging_enabled:
                    logging.warning("[MOCK] Simulating timeout error")
                raise RequestException("Connection timed out")
            elif error_type == 'server_error':
                if self.logging_enabled:
                    logging.warning("[MOCK] Simulating server error")
                mock_response = type('obj', (object,), {'status_code': 500})
                mock_exception = RequestException(response=mock_response)
                raise mock_exception
            else:  # client_error
                if self.logging_enabled:
                    logging.warning("[MOCK] Simulating client error")
                mock_response = type('obj', (object,), {'status_code': 400})
                mock_exception = RequestException(response=mock_response)
                raise mock_exception

        # メッセージを保存（検証機能用）
        message_data = {
            'context': {
                'user_id': context.user.user_id if hasattr(context, 'user') else None,
                'action': context.action if hasattr(context, 'action') else None,
                'source_type': context.source_type if hasattr(context, 'source_type') else None,
                'source_id': context.source_id if hasattr(context, 'source_id') else None
            },
            'reactions': reactions,
            'timestamp': now
        }
        self.sent_messages.append(message_data)

        # 成功レスポンスのシミュレーション - 処理時間も模擬
        delay = random.uniform(0.01, 0.05)  # 10〜50ミリ秒の処理時間
        time.sleep(delay)

        if self.logging_enabled and self.request_count % 100 == 0:
            logging.info(f"[MOCK] Successfully processed {self.request_count} requests")

        return "OK"

    def get_last_message(self):
        return self.sent_messages[-1] if self.sent_messages else None

    def get_message_history(self, limit=None):
        return self.sent_messages[-limit:] if limit and self.sent_messages else self.sent_messages

    def clear_messages(self):
        self.sent_messages = []


class MockLinePlugin_InterfaceFactory(object):
    def __init__(self, params):
        self.params = params

    def create_interface(self, bot_name, params):
        return MockLinePlugin_Interface(bot_name, utility.merge_params(self.params, params))


def inner_load_plugin(plugin_params):
    hub.register_interface_factory(type_name="mock_line",
                                   factory=MockLinePlugin_InterfaceFactory(plugin_params))
