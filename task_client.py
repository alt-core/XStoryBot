from cloud_backend import create_task_queue


_task_queue = None


def initialize(gcp_settings):
    global _task_queue
    _task_queue = create_task_queue()
    _task_queue.initialize(gcp_settings)


def get_client():
    if _task_queue is None:
        raise ValueError(
            'Task client not initialized. Call initialize() first.')
    return _task_queue.get_client()


def create_task(queue_name, url, params, delay_seconds=None):
    if _task_queue is None:
        raise ValueError(
            'Task client not initialized. Call initialize() first.')
    return _task_queue.create_task(
        queue_name, url, params, delay_seconds=delay_seconds)
