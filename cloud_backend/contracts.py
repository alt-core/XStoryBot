"""クラウドバックエンドが満たす最小契約。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional


class CloudBackendError(Exception):
    """クラウド境界内の共通エラー。"""


class StateStoreError(CloudBackendError):
    """状態保存処理のエラー。"""


class StateConflictError(StateStoreError):
    """楽観ロックまたはトランザクション競合。"""


class ObjectStoreError(CloudBackendError):
    """オブジェクト保存処理のエラー。"""


class ObjectNotFoundError(ObjectStoreError):
    """要求したオブジェクトが存在しない。"""


class InvalidObjectReferenceError(ObjectStoreError):
    """選択中のバックエンドでは扱えない参照。"""


class TaskQueueError(CloudBackendError):
    """非同期タスク登録処理のエラー。"""


class CredentialSourceError(CloudBackendError):
    """資格情報取得処理のエラー。"""


@dataclass(frozen=True)
class StateVersion:
    """保存先固有型を含まない楽観ロック用トークン。"""

    value: str


@dataclass(frozen=True)
class VersionedState:
    """状態本体と更新時に必要な版をまとめる。"""

    data: Mapping[str, Any]
    version: StateVersion


@dataclass(frozen=True)
class CredentialData:
    """ファイルまたはインラインJSONとして渡す資格情報。"""

    file_path: Optional[str] = None
    inline_json: Optional[str] = None
    use_default: bool = False


class StateStore(ABC):
    """Bot状態とキャッシュを保存する境界。"""

    @abstractmethod
    def get_global_bot_variables(self, bot_name):
        raise NotImplementedError

    @abstractmethod
    def save_global_bot_variables(self, bot_name, scenario_uri):
        raise NotImplementedError

    @abstractmethod
    def load_player_status(self, status_id):
        raise NotImplementedError

    @abstractmethod
    def create_player_status(self, status_id, data):
        raise NotImplementedError

    @abstractmethod
    def update_player_status(self, status_id, data, version):
        raise NotImplementedError

    @abstractmethod
    def force_put_player_status(self, status_id, data):
        raise NotImplementedError

    @abstractmethod
    def delete_player_status(self, status_id):
        raise NotImplementedError

    @abstractmethod
    def get_group_members(self, group_id):
        raise NotImplementedError

    @abstractmethod
    def append_group_member(self, group_id, shard_id, member):
        raise NotImplementedError

    @abstractmethod
    def remove_group_member(self, group_id, shard_id, member):
        raise NotImplementedError

    @abstractmethod
    def clear_group_members(self, group_id):
        raise NotImplementedError

    @abstractmethod
    def get_all_groups(self):
        raise NotImplementedError

    @abstractmethod
    def get_image_file_stat(self, key):
        raise NotImplementedError

    @abstractmethod
    def put_image_file_stat(self, key, data):
        raise NotImplementedError

    @abstractmethod
    def get_media_file_stat(self, key):
        raise NotImplementedError

    @abstractmethod
    def put_media_file_stat(self, key, data):
        raise NotImplementedError

    @abstractmethod
    def get_image_text_stat(self, key):
        raise NotImplementedError

    @abstractmethod
    def put_image_text_stat(self, key, data):
        raise NotImplementedError

    @abstractmethod
    def get_next_label(self, status_id):
        raise NotImplementedError

    @abstractmethod
    def set_next_label(self, status_id, label, trigger_message):
        raise NotImplementedError

    @abstractmethod
    def compare_and_clear_next_label(self, status_id, next_label):
        raise NotImplementedError

    @abstractmethod
    def clear_next_label(self, status_id):
        raise NotImplementedError

    @abstractmethod
    def get_build_cache(self, key):
        raise NotImplementedError

    @abstractmethod
    def set_build_cache(self, key, value, expire_at=None):
        raise NotImplementedError

    @abstractmethod
    def delete_build_cache(self, key):
        raise NotImplementedError

    @abstractmethod
    def clear_build_cache(self):
        raise NotImplementedError

    @abstractmethod
    def create_group_message_task(self, task_id, data):
        raise NotImplementedError

    @abstractmethod
    def get_group_message_task(self, task_id):
        raise NotImplementedError

    @abstractmethod
    def update_group_message_task(
            self, task_id, update_builder: Callable[[Mapping[str, Any]], Mapping[str, Any]]):
        raise NotImplementedError

    @abstractmethod
    def get_recent_group_message_tasks(self, bot_name, limit):
        raise NotImplementedError


class ObjectStore(ABC):
    """シナリオ、公開メディア、配信補助データを保存する境界。"""

    @abstractmethod
    def store_scenario(self, key, data):
        raise NotImplementedError

    @abstractmethod
    def load_scenario(self, reference):
        raise NotImplementedError

    @abstractmethod
    def store_public(self, key, data, content_type):
        raise NotImplementedError

    @abstractmethod
    def public_url(self, key):
        raise NotImplementedError

    @abstractmethod
    def store_private(self, key, data, content_type=None):
        raise NotImplementedError

    @abstractmethod
    def load_private(self, key):
        raise NotImplementedError


class TaskQueue(ABC):
    """即時・遅延タスクを登録する境界。"""

    @abstractmethod
    def initialize(self, backend_settings):
        raise NotImplementedError

    @abstractmethod
    def create_task(self, queue_name, url, params, delay_seconds=None):
        raise NotImplementedError


class CredentialSource(ABC):
    """管理者認証とGoogle資格情報を取得する境界。"""

    @abstractmethod
    def get_admin_auth_credential(self):
        raise NotImplementedError

    @abstractmethod
    def get_admin_auth_client_config(self):
        raise NotImplementedError

    @abstractmethod
    def get_google_service_account(self, reference=None, allow_default=False):
        raise NotImplementedError
