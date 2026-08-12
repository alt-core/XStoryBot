"""GCPバックエンドの生成入口。"""


_object_store = None


def create_state_store():
    from cloud_backend.gcp.state_store import GcpStateStore
    return GcpStateStore()


def create_object_store():
    global _object_store
    from cloud_backend.gcp.object_store import GcpObjectStore
    if _object_store is None:
        # Scenarioのimport時clientをGroup用private操作でも共有し、
        # 従来になかった明示credential clientを増やさない。
        _object_store = GcpObjectStore()
    return _object_store


def create_task_queue():
    from cloud_backend.gcp.task_queue import GcpTaskQueue
    return GcpTaskQueue()


def create_credential_source():
    from cloud_backend.gcp.credential_source import GcpCredentialSource
    import settings
    return GcpCredentialSource(auth_settings=settings.AUTH_SETTINGS)
