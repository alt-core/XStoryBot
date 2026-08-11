import datetime

from cloud_backend import create_state_store

_state_store = None
_client = None
_collection = None

def get_client():
    global _state_store
    global _client
    global _collection
    if _client is None:
        _state_store = create_state_store()
        _client = _state_store.client
    if _collection is None:
        _collection = _client.collection('build_cache')
    return _client

def get_collection():
    global _collection
    if _collection is None:
        _ = get_client()
    return _collection

def get_state_store():
    if _state_store is None:
        _ = get_client()
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
