import datetime
import logging
from urllib.parse import urlencode
import uuid

from google.cloud import tasks_v2
from google.oauth2 import service_account
from google.protobuf import timestamp_pb2

import auth


_client = None
_project_id = None
_location = None
_base_url_map = None
_credentials = None
_initialized = False


def initialize(gcp_settings):
    global _client, _project_id, _location, _base_url_map
    global _credentials, _initialized

    # 再初期化時に以前の設定で作成したクライアントを再利用しない。
    _client = None
    _initialized = False
    _project_id = gcp_settings['project_id']
    _location = gcp_settings['location']
    _base_url_map = {
        'build-queue': gcp_settings['services']['builder']['base_url'],
        'action-queue': gcp_settings['services']['app']['base_url'],
        'group-message-queue': gcp_settings['services']['app']['base_url'],
    }

    credentials_path = gcp_settings.get('credentials_path')
    if credentials_path:
        _credentials = (
            service_account.Credentials.from_service_account_file(
                credentials_path))
    else:
        _credentials = None
    _initialized = True


def get_client():
    global _client

    if not _initialized:
        raise ValueError(
            'Task client not initialized. Call initialize() first.')
    if _client is None:
        if _credentials is None:
            _client = tasks_v2.CloudTasksClient()
        else:
            _client = tasks_v2.CloudTasksClient(credentials=_credentials)
    return _client


def create_task(queue_name, url, params, delay_seconds=None):
    client = get_client()
    base_url = _base_url_map[queue_name]
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
                'X-API-Token': auth.get_api_token(),
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
                _project_id, _location, queue_name),
            task=task,
        )
    )
    logging.info(f'Created task: {response.name}, task_id: {task_id}')
    return task_id
