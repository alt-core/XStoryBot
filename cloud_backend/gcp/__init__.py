"""GCPバックエンドの生成入口。"""


def create_state_store():
    from cloud_backend.gcp.state_store import GcpStateStore
    return GcpStateStore()


def create_object_store():
    from cloud_backend.gcp.object_store import GcpObjectStore
    return GcpObjectStore()


def create_task_queue():
    from cloud_backend.gcp.task_queue import GcpTaskQueue
    return GcpTaskQueue()


def create_credential_source():
    from cloud_backend.gcp.credential_source import GcpCredentialSource
    return GcpCredentialSource()
