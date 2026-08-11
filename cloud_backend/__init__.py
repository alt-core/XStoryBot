"""クラウドごとの差を四つの境界へ閉じ込める。"""

from cloud_backend.factory import (
    configure,
    create_credential_source,
    create_object_store,
    create_state_store,
    create_task_queue,
    get_provider,
)


__all__ = [
    'configure',
    'create_credential_source',
    'create_object_store',
    'create_state_store',
    'create_task_queue',
    'get_provider',
]
