"""AWSバックエンドの生成入口。"""


_object_store = None
_state_store = None


def create_object_store():
    """プロセス内で共有するAWS ObjectStoreを返す。"""
    global _object_store

    if _object_store is None:
        import settings
        from cloud_backend.aws.object_store import AwsObjectStore
        _object_store = AwsObjectStore(settings.BACKEND_SETTINGS)
    return _object_store


def _not_implemented(boundary_name):
    raise ValueError(f'AWS {boundary_name}はまだ実装されていません')


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
    return _not_implemented('TaskQueue')


def create_credential_source():
    return _not_implemented('CredentialSource')


def _reset_for_test():
    global _object_store, _state_store
    _object_store = None
    _state_store = None
