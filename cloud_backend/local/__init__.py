"""外部サービスを使わないローカル保存の生成入口。"""


def create_state_store():
    import settings
    from cloud_backend.local.state_store import LocalStateStore
    objects = create_object_store()
    return LocalStateStore(settings.BACKEND_SETTINGS.get(
        'state_storage_root', objects.storage_root))


def create_object_store():
    import settings
    from cloud_backend.local.object_store import LocalObjectStore
    local = settings.BACKEND_SETTINGS
    return LocalObjectStore(local['storage_root'], local['public_base_url'])


def create_task_queue():
    from cloud_backend.local.task_queue import LocalTaskQueue
    return LocalTaskQueue()


def create_credential_source():
    import settings
    from cloud_backend.local.credential_source import LocalCredentialSource
    return LocalCredentialSource(auth_settings=settings.AUTH_SETTINGS)
