from cloud_backend import create_task_queue


_task_queue = None


def allows_delayed_scenarios():
    return create_task_queue().allows_delayed_scenarios


def defer_until_response():
    return _task_queue is not None and _task_queue.defer_until_response is True


def initialize(gcp_settings):
    global _task_queue
    _task_queue = create_task_queue()
    _task_queue.initialize(gcp_settings)


def create_task(queue_name, url, params, delay_seconds=None):
    if _task_queue is None:
        raise ValueError(
            'Task client not initialized. Call initialize() first.')
    return _task_queue.create_task(
        queue_name, url, params, delay_seconds=delay_seconds)
