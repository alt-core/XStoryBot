"""AWSバックエンドの生成入口。"""


_object_store = None
_state_store = None
_task_queue = None
_credential_source = None


def create_object_store():
    """プロセス内で共有するAWS ObjectStoreを返す。"""
    global _object_store

    if _object_store is None:
        import settings
        from cloud_backend.aws.object_store import AwsObjectStore
        _object_store = AwsObjectStore(settings.BACKEND_SETTINGS)
    return _object_store


def create_state_store():
    """プロセス内で共有するAWS StateStoreを返す。"""
    global _state_store

    if _state_store is None:
        import settings
        from cloud_backend.aws.state_store import AwsStateStore
        _state_store = AwsStateStore(
            settings.BACKEND_SETTINGS,
            object_store=create_object_store(),
        )
    return _state_store


def create_task_queue():
    """プロセス内で共有するAWS TaskQueueを返す。"""
    global _task_queue

    if _task_queue is None:
        from cloud_backend.aws.task_queue import AwsTaskQueue
        _task_queue = AwsTaskQueue()
    return _task_queue


def create_credential_source():
    """プロセス内で共有するAWS CredentialSourceを返す。"""
    global _credential_source

    if _credential_source is None:
        import settings
        from cloud_backend.aws.credential_source import AwsCredentialSource
        _credential_source = AwsCredentialSource(settings.BACKEND_SETTINGS)
    return _credential_source


def _reset_for_test():
    global _object_store, _state_store, _task_queue, _credential_source
    _object_store = None
    _state_store = None
    _task_queue = None
    _credential_source = None
