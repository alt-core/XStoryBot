"""Cloud Tasksを利用するGCP TaskQueue実装。"""

import datetime
import json
import logging
from urllib.parse import urlencode
import uuid

from google.cloud import tasks_v2
from google.oauth2 import service_account
from google.protobuf import timestamp_pb2

import auth
from cloud_backend.contracts import TaskQueue
from cloud_backend.gcp.credential_source import GcpCredentialSource


class GcpTaskQueue(TaskQueue):
    """既存のCloud Tasks登録契約を維持する。"""

    def __init__(self, credential_source=None, token_supplier=None):
        self._client = None
        self._project_id = None
        self._location = None
        self._base_url_map = None
        self._credentials = None
        self._initialized = False
        self._credential_source = (
            credential_source or GcpCredentialSource())
        self._token_supplier = token_supplier or auth.get_api_token

    def initialize(self, gcp_settings):
        # 再初期化時に以前の設定で作成したクライアントを再利用しない。
        self._client = None
        self._initialized = False
        self._project_id = gcp_settings['project_id']
        self._location = gcp_settings['location']
        self._base_url_map = {
            'build-queue': gcp_settings['services']['builder']['base_url'],
            'action-queue': gcp_settings['services']['app']['base_url'],
            'group-message-queue': gcp_settings['services']['app']['base_url'],
        }

        credential_data = self._credential_source.get_google_service_account(
            gcp_settings.get('credentials_path'), allow_default=True)
        if credential_data.use_default:
            self._credentials = None
        elif credential_data.inline_json is not None:
            self._credentials = (
                service_account.Credentials.from_service_account_info(
                    json.loads(credential_data.inline_json)))
        else:
            self._credentials = (
                service_account.Credentials.from_service_account_file(
                    credential_data.file_path))
        self._initialized = True

    def get_client(self):
        if not self._initialized:
            raise ValueError(
                'Task client not initialized. Call initialize() first.')
        if self._client is None:
            if self._credentials is None:
                self._client = tasks_v2.CloudTasksClient()
            else:
                self._client = tasks_v2.CloudTasksClient(
                    credentials=self._credentials)
        return self._client

    def create_task(self, queue_name, url, params, delay_seconds=None):
        client = self.get_client()
        base_url = self._base_url_map[queue_name]
        full_url = f'{base_url}{url}'

        # Cloud Tasks上の名前はGoogleへ任せ、本文のIDは処理の相関にだけ使う。
        task_id = str(uuid.uuid4())
        request_params = params.copy()
        request_params['task_id'] = task_id

        logging.info(f'Creating task: {full_url}, task_id: {task_id}')

        task = tasks_v2.Task(
            http_request=tasks_v2.HttpRequest(
                http_method=tasks_v2.HttpMethod.POST,
                headers={
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'X-API-Token': self._token_supplier(),
                },
                url=full_url,
                body=urlencode(request_params).encode(),
            ),
        )

        if delay_seconds:
            scheduled_at = (
                datetime.datetime.utcnow()
                + datetime.timedelta(seconds=delay_seconds))
            timestamp = timestamp_pb2.Timestamp()
            timestamp.FromDatetime(scheduled_at)
            task.schedule_time = timestamp

        response = client.create_task(
            tasks_v2.CreateTaskRequest(
                parent=client.queue_path(
                    self._project_id, self._location, queue_name),
                task=task,
            )
        )
        logging.info(f'Created task: {response.name}, task_id: {task_id}')
        return task_id
