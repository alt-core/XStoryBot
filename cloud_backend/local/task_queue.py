"""ローカルでは非同期処理を模倣しない。"""

from cloud_backend.contracts import TaskQueue, TaskQueueError


class LocalTaskQueue(TaskQueue):
    def initialize(self, backend_settings):
        pass

    def create_task(self, queue_name, url, params, delay_seconds=None):
        raise TaskQueueError('local providerは非同期タスクに対応していません')
