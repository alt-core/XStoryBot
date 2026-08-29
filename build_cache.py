import datetime

from cloud_backend import create_state_store

_state_store = None

def get_state_store():
    global _state_store
    if _state_store is None:
        _state_store = create_state_store()
    return _state_store

def set_cache(key, value, sec=None):
    if sec:
        expire_time = datetime.datetime.now() + datetime.timedelta(seconds=sec)
        get_state_store().set_build_cache(key, value, expire_time)
    else:
        get_state_store().set_build_cache(key, value)
    return True

def get_cache(key):
    return get_state_store().get_build_cache(key)

def delete_cache(key):
    get_state_store().delete_build_cache(key)
    return True

def clear():
    get_state_store().clear_build_cache()
    return True
