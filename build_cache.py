import datetime

from google.cloud import firestore

_client = None
_collection = None

def get_client():
    global _client
    global _collection
    if _client is None:
        _client = firestore.Client()
    if _collection is None:
        _collection = _client.collection('build_cache')
    return _client

def get_collection():
    global _collection
    if _collection is None:
        _ = get_client()
    return _collection

def set_cache(key, value, sec=None):
    collection = get_collection()
    doc_ref = collection.document(key)
    if sec:
        expire_time = datetime.datetime.now() + datetime.timedelta(seconds=sec)
        doc_ref.set({
            'value': value,
            'expireAt': expire_time,
        })
    else:
        doc_ref.set({
            'value': value,
        })
    return True

def get_cache(key):
    collection = get_collection()
    doc = collection.document(key).get()
    if doc.exists:
        data = doc.to_dict()
        return data.get('value')
    return None

def delete_cache(key):
    collection = get_collection()
    doc_ref = collection.document(key)
    doc_ref.delete()
    return True

def clear():
    collection = get_collection()
    documents = collection.stream()
    for doc in documents:
        collection.document(doc.id).delete()
    return True
