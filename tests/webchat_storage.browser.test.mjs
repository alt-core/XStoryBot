import { createWebchatClient, WebchatClientError } from '../webchat-client/index.js';

if (typeof window === 'undefined' || typeof document === 'undefined') {
  throw new Error('実IndexedDBのテストは開発サーバーの/devtest/storageで実行してください');
}

// ローカルbrowserで実IndexedDBを使う。人工Botの記録だけを作成・削除する。
export async function runWebchatStorageTests() {
  const results = [];
  const database = await new Promise((resolve, reject) => {
    const request = indexedDB.open('xstorybot-webchat-v1', 1);
    request.onupgradeneeded = () => {
      request.result.createObjectStore('conversations', { keyPath: 'key' });
      request.result.createObjectStore('turns', { keyPath: 'id' })
        .createIndex('conversationKey', 'conversationKey');
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  const assert = (condition, message) => {
    if (!condition) throw new Error(message);
  };
  const transaction = (mode, callback) => new Promise((resolve, reject) => {
    const tx = database.transaction(['conversations', 'turns'], mode);
    tx.oncomplete = () => resolve();
    tx.onabort = () => reject(tx.error || new Error('transaction中断'));
    callback(tx);
  });
  const records = async (key) => {
    let head;
    let turns;
    await transaction('readonly', (tx) => {
      tx.objectStore('conversations').get(key).onsuccess = (event) => {
        head = event.target.result;
      };
      tx.objectStore('turns').index('conversationKey').getAll(key)
        .onsuccess = (event) => { turns = event.target.result; };
    });
    return { head, turns };
  };
  const keys = [];
  const clients = [];
  const fixture = async (name, { empty = false } = {}) => {
    const bot = `storage-test-${name}-${crypto.randomUUID()}`;
    const key = `${location.origin}|${bot}`;
    keys.push(key);
    const message = {
      id: `${bot}:message`, type: 'button', text: '選択',
      actions: [{ type: 'postback', label: '次へ', token: 'choice-token' }],
    };
    const messages = empty ? [] : [message];
    const head = {
      key, schemaVersion: 1, stateId: 'state-0', stateRevision: 0,
      stateToken: 'state-token-0', activeResponse: messages,
    };
    await transaction('readwrite', (tx) => {
      tx.objectStore('conversations').put(head);
      tx.objectStore('turns').put({
        id: `${key}:0`, conversationKey: key, sequence: 0,
        requestId: 'request-0', echoMessage: null, messages,
      });
    });
    const requests = [];
    const newClient = () => {
      const client = createWebchatClient({
        apiBaseUrl: location.origin, bot,
        fetch: async (_url, options) => {
          requests.push(JSON.parse(options.body));
          return new Response(JSON.stringify({
            schema_version: 1, request_id: 'request-1',
            state: { id: 'state-1', revision: 1 }, state_token: 'state-token-1',
            echo_message: null,
            messages: [{ ...message, id: `${bot}:next-message` }],
          }), { headers: { 'Content-Type': 'application/json' } });
        },
      });
      clients.push(client);
      return client;
    };
    return { key, messages, requests, newClient };
  };

  try {
    const test = await fixture('最新応答');
    const client = test.newClient();
    await client.initialize();
    await client.clearHistory();
    await client.clearHistory();
    const stored = await records(test.key);
    assert(stored.turns.length === 0, '履歴が残っている');
    assert(stored.head.activeResponse[0].id === test.messages[0].id,
      '最新応答が保存されていない');
    assert(test.requests.length === 0, '削除だけで通信した');
    client.destroy();
    const reopened = test.newClient();
    await reopened.initialize();
    assert(reopened.getSnapshot().stateId === 'state-0', 'stateが変わった');
    await reopened.sendPostback(
      reopened.getSnapshot().activeResponse[0].actions[0].token);
    assert(test.requests[0].state_token === 'state-token-0', '保存stateを使っていない');
    assert(test.requests[0].input.postback_token === 'choice-token', '選択肢が変わった');
    await reopened.clearHistory();
    assert(reopened.getSnapshot().activeResponse[0].id.endsWith(':next-message'),
      '送信後の削除で応答が戻った');
    results.push('履歴削除・再読込・選択肢継続');

    const empty = await fixture('空応答', { empty: true });
    const emptyClient = empty.newClient();
    await emptyClient.initialize();
    await emptyClient.clearHistory();
    assert(emptyClient.getSnapshot().activeResponse.length === 0, '空応答が変わった');
    results.push('空応答を維持');

    const corrupt = await fixture('不正head');
    const corruptClient = corrupt.newClient();
    await corruptClient.initialize();
    await transaction('readwrite', (tx) => {
      const store = tx.objectStore('conversations');
      store.get(corrupt.key).onsuccess = (event) => {
        store.put({ ...event.target.result, activeResponse: null });
      };
    });
    for (const operation of [
      () => corrupt.newClient().initialize(),
      () => corruptClient.clearHistory(),
    ]) {
      let corruptRejected = false;
      try { await operation(); } catch (error) {
        corruptRejected = error instanceof WebchatClientError
          && error.code === 'storage-corrupt';
      }
      assert(corruptRejected, '不正なheadの例外を整形していない');
    }
    assert(corrupt.requests.length === 0, '不正なheadで通信した');
    assert(corruptClient.getSnapshot().error.code === 'storage-corrupt',
      '不正なheadのエラーをsnapshotへ反映していない');
    results.push('不正headをinitializeとclearHistoryで同じ例外に整形');

    const aborted = await fixture('abort');
    const abortedClient = aborted.newClient();
    await abortedClient.initialize();
    const originalDelete = IDBObjectStore.prototype.delete;
    IDBObjectStore.prototype.delete = function (key) {
      const request = originalDelete.call(this, key);
      if (this.name === 'turns' && key === `${aborted.key}:0`) {
        this.transaction.abort();
      }
      return request;
    };
    try {
      let rejected = false;
      try { await abortedClient.clearHistory(); } catch (error) {
        rejected = error instanceof WebchatClientError
          && error.code === 'persistence-error';
      }
      assert(rejected, '削除失敗の例外を整形していない');
    } finally {
      IDBObjectStore.prototype.delete = originalDelete;
    }
    const afterAbort = await records(aborted.key);
    assert(afterAbort.turns.length === 1
      && afterAbort.head.activeResponse[0].id === aborted.messages[0].id,
      'abort後に履歴または最新応答が変わった');
    assert(abortedClient.getSnapshot().error.code === 'persistence-error',
      '削除失敗をsnapshotへ反映していない');
    results.push('履歴削除のabortでstateと応答を維持');

    const quota = await fixture('quota');
    const quotaClient = quota.newClient();
    await quotaClient.initialize();
    const originalPut = IDBObjectStore.prototype.put;
    const quotaTransactions = new WeakSet();
    IDBObjectStore.prototype.put = function (value, ...args) {
      const request = originalPut.call(this, value, ...args);
      if ((this.name === 'turns' && value.conversationKey === quota.key)
        || (this.name === 'conversations' && value.key === quota.key
          && value.stateId === 'state-1')) {
        Object.defineProperty(request, 'error', {
          value: new DOMException('容量不足を注入', 'QuotaExceededError'),
        });
        if (!quotaTransactions.has(this.transaction)) {
          quotaTransactions.add(this.transaction);
          queueMicrotask(() => {
            request.onerror?.({ target: request });
            this.transaction.abort();
          });
        }
      }
      return request;
    };
    try {
      await quotaClient.sendText('続ける');
    } finally {
      IDBObjectStore.prototype.put = originalPut;
    }
    assert(quotaClient.getSnapshot().persistence === 'memory', 'memoryへ降格していない');
    assert(quotaClient.getSnapshot().stateId === 'state-1', '応答を失った');
    const afterQuota = await records(quota.key);
    assert(afterQuota.turns.length === 0 && afterQuota.head.activeResponse.length === 1,
      'quota削減でheadの最新応答を失った');
    const quotaReopened = quota.newClient();
    await quotaReopened.initialize();
    assert(quotaReopened.getSnapshot().activeResponse[0].id === quota.messages[0].id,
      '保存先の旧進行を再開できない');
    results.push('quota削減・memory降格・保存先の最新応答保持');
    return results;
  } finally {
    for (const client of clients) client.destroy();
    await transaction('readwrite', (tx) => {
      for (const key of keys) {
        tx.objectStore('conversations').delete(key);
        const turns = tx.objectStore('turns');
        turns.index('conversationKey').getAllKeys(key).onsuccess = (event) => {
          for (const id of event.target.result) turns.delete(id);
        };
      }
    });
    database.close();
  }
}

const resultElement = document.getElementById('storage-test-result');
if (resultElement) {
  runWebchatStorageTests().then((results) => {
    resultElement.textContent = `成功: ${results.length}項目\n${results.join('\n')}`;
    resultElement.dataset.status = 'passed';
  }).catch((error) => {
    resultElement.textContent = `失敗: ${error.message || error}`;
    resultElement.dataset.status = 'failed';
  });
}
