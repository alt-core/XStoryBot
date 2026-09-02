const DB_NAME = 'xstorybot-webchat-v1';
const DB_VERSION = 1;
const CONVERSATIONS = 'conversations';
const TURNS = 'turns';

export class WebchatClientError extends Error {
  constructor(message, {
    status = 0,
    code = 'network-error',
    requestId = null,
    retryAfter = null,
  } = {}) {
    super(message);
    this.name = 'WebchatClientError';
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.retryAfter = retryAfter;
  }
}

class StaleStateError extends Error {}
class CorruptStorageError extends Error {}

const requestAsPromise = (request) => new Promise((resolve, reject) => {
  request.onsuccess = () => resolve(request.result);
  request.onerror = () => reject(request.error || new Error('IndexedDB request failed'));
});

const transactionAsPromise = (transaction) => new Promise((resolve, reject) => {
  transaction.oncomplete = () => resolve();
  transaction.onabort = () => reject(transaction.error || new Error('IndexedDB transaction aborted'));
  transaction.onerror = () => reject(transaction.error || new Error('IndexedDB transaction failed'));
});

const cloneData = (value) => {
  if (typeof globalThis.structuredClone === 'function') {
    return globalThis.structuredClone(value);
  }
  return JSON.parse(JSON.stringify(value));
};

const deepFreeze = (value) => {
  if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value;
  Object.freeze(value);
  for (const child of Object.values(value)) deepFreeze(child);
  return value;
};

const immutableSnapshot = (value) => deepFreeze({
  ...value,
  turns: [...(value.turns || [])],
  messages: [...(value.messages || [])],
  activeResponse: [...(value.activeResponse || [])],
});

const emptySnapshot = (persistence = 'memory') => immutableSnapshot({
  status: 'idle',
  persistence,
  stateId: null,
  stateRevision: null,
  turns: [],
  messages: [],
  activeResponse: [],
  error: null,
  notice: persistence === 'memory'
    ? 'このページを閉じると進行が失われます。'
    : null,
});

class MemoryStorage {
  constructor(key) {
    this.key = key;
    this.head = null;
    this.turns = [];
    this.kind = 'memory';
  }

  async load() {
    return cloneData({ head: this.head, turns: this.turns });
  }

  seed(stored) {
    const cloned = cloneData(stored);
    this.head = cloned.head || null;
    this.turns = cloned.turns || [];
  }

  async commit(baseStateId, response) {
    const currentStateId = this.head?.stateId || null;
    if (currentStateId !== baseStateId) throw new StaleStateError();
    const sequence = response.state.revision;
    const turn = makeTurn(this.key, sequence, response);
    this.turns = this.turns.filter((item) => item.id !== turn.id);
    this.turns.push(turn);
    this.turns.sort((a, b) => a.sequence - b.sequence);
    this.head = makeHead(this.key, response);
    return false;
  }

  async reset() {
    this.head = null;
    this.turns = [];
  }

  async clearHistory() {
    this.turns = [];
  }
}

const makeTurn = (key, sequence, response) => ({
  id: `${key}:${sequence}`,
  conversationKey: key,
  sequence,
  requestId: response.request_id,
  echoMessage: response.echo_message ?? null,
  messages: response.messages || [],
});

const makeHead = (key, response) => ({
  key,
  schemaVersion: 1,
  stateId: response.state.id,
  stateRevision: response.state.revision,
  stateToken: response.state_token,
  activeMessageIds: (response.messages || []).map((message) => message.id),
  updatedAt: Date.now(),
});

class IndexedDbStorage {
  constructor(factory, key) {
    this.factory = factory;
    this.key = key;
    this.kind = 'indexeddb';
    this.database = null;
  }

  async open() {
    if (this.database) return;
    const request = this.factory.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(CONVERSATIONS)) {
        database.createObjectStore(CONVERSATIONS, { keyPath: 'key' });
      }
      if (!database.objectStoreNames.contains(TURNS)) {
        const store = database.createObjectStore(TURNS, { keyPath: 'id' });
        store.createIndex('conversationKey', 'conversationKey', { unique: false });
      }
    };
    this.database = await requestAsPromise(request);
    this.database.onversionchange = () => {
      this.database.close();
      this.database = null;
    };
    this.database.onclose = () => {
      this.database = null;
    };
  }

  async load() {
    await this.open();
    const tx = this.database.transaction([CONVERSATIONS, TURNS], 'readonly');
    const headRequest = tx.objectStore(CONVERSATIONS).get(this.key);
    const turnsRequest = tx.objectStore(TURNS).index('conversationKey').getAll(this.key);
    const [head, turns] = await Promise.all([
      requestAsPromise(headRequest), requestAsPromise(turnsRequest), transactionAsPromise(tx),
    ]);
    const stored = {
      head: head || null,
      turns: (turns || []).sort((a, b) => a.sequence - b.sequence),
    };
    const validHead = stored.head == null || (
      stored.head.schemaVersion === 1
      && typeof stored.head.stateId === 'string'
      && Number.isInteger(stored.head.stateRevision)
      && typeof stored.head.stateToken === 'string'
      && (
        Array.isArray(stored.head.activeMessageIds)
        || Array.isArray(stored.head.activeResponse)
      )
    );
    const validTurns = stored.turns.every((turn) => (
      typeof turn.id === 'string'
      && turn.conversationKey === this.key
      && Number.isInteger(turn.sequence)
      && typeof turn.requestId === 'string'
      && (turn.echoMessage == null || typeof turn.echoMessage === 'string')
      && Array.isArray(turn.messages)
    ));
    if (!validHead || !validTurns || (!stored.head && stored.turns.length)) {
      throw new CorruptStorageError();
    }
    return stored;
  }

  async _commitOnce(baseStateId, response) {
    await this.open();
    await new Promise((resolve, reject) => {
      const tx = this.database.transaction([CONVERSATIONS, TURNS], 'readwrite');
      const conversations = tx.objectStore(CONVERSATIONS);
      const turns = tx.objectStore(TURNS);
      let stale = false;
      let failure = null;
      tx.oncomplete = () => resolve();
      tx.onabort = () => reject(stale
        ? new StaleStateError()
        : (failure || tx.error || new Error('IndexedDB transaction aborted')));
      tx.onerror = (event) => {
        failure = event.target?.error || failure;
      };
      const headRequest = conversations.get(this.key);
      headRequest.onsuccess = () => {
        const current = headRequest.result;
        if ((current?.stateId || null) !== baseStateId) {
          stale = true;
          tx.abort();
          return;
        }
        const sequence = response.state.revision;
        const turnRequest = turns.put(makeTurn(this.key, sequence, response));
        const headPutRequest = conversations.put(makeHead(this.key, response));
        turnRequest.onerror = (event) => {
          failure = event.target?.error || failure;
        };
        headPutRequest.onerror = (event) => {
          failure = event.target?.error || failure;
        };
      };
    });
  }

  async commit(baseStateId, response) {
    let pruned = false;
    while (true) {
      try {
        await this._commitOnce(baseStateId, response);
        return pruned;
      } catch (error) {
        if (error instanceof StaleStateError) throw error;
        if (error?.name !== 'QuotaExceededError') throw error;
        const current = await this.load();
        if (!current.turns.length) throw error;
        await this._pruneOldest();
        pruned = true;
      }
    }
  }

  async _pruneOldest() {
    await this.open();
    await new Promise((resolve, reject) => {
      const tx = this.database.transaction([TURNS], 'readwrite');
      const store = tx.objectStore(TURNS);
      tx.oncomplete = () => resolve();
      tx.onabort = () => reject(tx.error || new Error('IndexedDB transaction aborted'));
      tx.onerror = () => {};
      const listRequest = store.index('conversationKey').getAll(this.key);
      listRequest.onsuccess = () => {
        const existing = listRequest.result || [];
        existing.sort((a, b) => a.sequence - b.sequence);
        if (existing.length) store.delete(existing[0].id);
      };
    });
  }

  _queueDeleteTurns(transaction) {
    const store = transaction.objectStore(TURNS);
    const keysRequest = store.index('conversationKey').getAllKeys(this.key);
    keysRequest.onsuccess = () => {
      for (const key of keysRequest.result || []) store.delete(key);
    };
  }

  async reset() {
    await this.open();
    const tx = this.database.transaction([CONVERSATIONS, TURNS], 'readwrite');
    this._queueDeleteTurns(tx);
    tx.objectStore(CONVERSATIONS).delete(this.key);
    await transactionAsPromise(tx);
  }

  async clearHistory() {
    await this.open();
    const tx = this.database.transaction([TURNS], 'readwrite');
    this._queueDeleteTurns(tx);
    await transactionAsPromise(tx);
  }

  close() {
    this.database?.close();
    this.database = null;
  }
}

const snapshotFromStorage = (stored, persistence, overrides = {}) => {
  const turns = stored.turns || [];
  const messages = turns.flatMap((turn) => turn.messages || []);
  const activeIds = new Set(
    stored.head?.activeMessageIds
    || (stored.head?.activeResponse || []).map((message) => message.id),
  );
  return immutableSnapshot({
    status: stored.head ? 'ready' : 'idle',
    persistence,
    stateId: stored.head?.stateId || null,
    stateRevision: stored.head?.stateRevision ?? null,
    turns,
    messages,
    activeResponse: messages.filter((message) => activeIds.has(message.id)),
    error: null,
    notice: persistence === 'memory'
      ? 'このページを閉じると進行が失われます。'
      : null,
    ...overrides,
  });
};

export function createWebchatClient(options) {
  if (!options?.apiBaseUrl || !options?.bot) {
    throw new TypeError('apiBaseUrlとbotが必要です');
  }
  const apiBaseUrl = String(options.apiBaseUrl).replace(/\/+$/, '');
  const bot = String(options.bot);
  const key = `${apiBaseUrl}|${bot}`;
  const fetchImpl = options.fetch || globalThis.fetch?.bind(globalThis);

  let storage = new MemoryStorage(key);
  let snapshot = emptySnapshot();
  const serverSnapshot = emptySnapshot();
  let initialized = false;
  let initializePromise = null;
  let lifecycle = 0;
  let inFlight = false;
  const listeners = new Set();
  const lockName = `xstorybot-webchat:${key}`;
  const channelName = `xstorybot-webchat:${key}`;
  let channel = null;

  const emit = (next) => {
    snapshot = immutableSnapshot(next);
    for (const listener of listeners) listener(snapshot);
  };

  const refresh = async (notice = null) => {
    const stored = await storage.load();
    if (!notice && snapshot.stateId && !stored.head) {
      notice = '保存された進行が見つからないため、「最初から」で再開してください。';
    }
    emit(snapshotFromStorage(stored, storage.kind, { notice }));
    return snapshot;
  };

  const notify = () => {
    if (channel) {
      channel.postMessage({ type: 'updated' });
      return;
    }
    try {
      globalThis.localStorage?.setItem(
        `xstorybot-webchat-beacon:${key}`,
        `${Date.now()}:${Math.random()}`,
      );
    } catch (_error) {
      // 通知失敗は次操作時のhead再読込で回復する。
    }
  };

  const detachSharedListeners = () => {
    channel?.close();
    channel = null;
    if (typeof globalThis.removeEventListener === 'function') {
      globalThis.removeEventListener('storage', onStorage);
      globalThis.removeEventListener('focus', onFocus);
      globalThis.document?.removeEventListener('visibilitychange', onVisibility);
    }
  };

  const initialize = async () => {
    if (initialized) return snapshot;
    if (initializePromise) return initializePromise;
    const generation = lifecycle;
    initializePromise = (async () => {
      if (options.indexedDB || globalThis.indexedDB) {
        try {
          const candidate = new IndexedDbStorage(
            options.indexedDB || globalThis.indexedDB, key);
          await candidate.open();
          if (generation !== lifecycle) {
            candidate.close();
            return snapshot;
          }
          storage = candidate;
        } catch (_error) {
          if (storage.kind !== 'memory') storage = new MemoryStorage(key);
        }
      }
      if (generation !== lifecycle) return snapshot;
      if (
        storage.kind === 'indexeddb'
        && typeof globalThis.BroadcastChannel !== 'undefined'
      ) {
        channel = new globalThis.BroadcastChannel(channelName);
        channel.onmessage = (event) => {
          if (event.data?.type === 'updated') {
            refresh('別のタブの最新状態を表示しました。').catch(() => {});
          }
        };
      }
      if (
        storage.kind === 'indexeddb'
        && typeof globalThis.addEventListener === 'function'
      ) {
        globalThis.addEventListener('storage', onStorage);
        globalThis.addEventListener('focus', onFocus);
        globalThis.document?.addEventListener('visibilitychange', onVisibility);
      }
      initialized = true;
      try {
        return await refresh();
      } catch (error) {
        if (error instanceof CorruptStorageError) {
          const corruptError = new WebchatClientError(
            '保存した進行の形式が不正です。「最初から」で再開してください。',
            { code: 'storage-corrupt' },
          );
          emit({ ...snapshot, status: 'error', error: corruptError });
          throw corruptError;
        }
        storage.close?.();
        storage = new MemoryStorage(key);
        detachSharedListeners();
        return refresh();
      }
    })();
    try {
      return await initializePromise;
    } finally {
      if (generation === lifecycle) initializePromise = null;
    }
  };

  const parseError = async (response) => {
    let problem = null;
    const contentType = response.headers.get('Content-Type') || '';
    if (contentType.includes('/json') || contentType.includes('+json')) {
      try {
        problem = await response.json();
      } catch (_error) {
        // providerが生成する不正JSON errorを固定errorへ正規化する。
      }
    }
    const messages = {
      'action-not-active': 'この選択肢は古くなりました。最新の会話を確認してください。',
      'incompatible-state': 'シナリオが更新されました。「最初から」で再開してください。',
      'invalid-state': '保存した進行を確認できません。「最初から」で再開してください。',
      'rate-limited': '混み合っています。しばらく待って手動で再試行してください。',
      'turn-timeout': '処理が時間内に完了しませんでした。手動再試行では外部処理が重複する場合があります。',
      'external-http-timeout': '外部処理が時間内に完了しませんでした。手動再試行では処理が重複する場合があります。',
      'external-http-error': '外部処理に失敗しました。状態は更新していません。',
    };
    return new WebchatClientError(
      messages[problem?.code]
        || problem?.title
        || `Webchat request failed (${response.status})`,
      {
        status: response.status,
        code: problem?.code || 'network-error',
        requestId: problem?.request_id || null,
        retryAfter: response.headers.get('Retry-After'),
      },
    );
  };

  const postTurn = async (body) => {
    if (!fetchImpl) {
      throw new WebchatClientError('fetchが利用できません', {
        code: 'fetch-unavailable',
      });
    }
    let response;
    try {
      response = await fetchImpl(
        `${apiBaseUrl}/api/webchat/v1/bots/${encodeURIComponent(bot)}/turn`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'omit',
          cache: 'no-store',
          body: JSON.stringify(body),
        },
      );
    } catch (error) {
      throw new WebchatClientError(error?.message || 'Network error');
    }
    if (!response.ok) throw await parseError(response);
    const contentType = response.headers.get('Content-Type') || '';
    if (!(contentType.includes('/json') || contentType.includes('+json'))) {
      throw new WebchatClientError('Webchat responseの形式が不正です', {
        code: 'invalid-response',
      });
    }
    let value;
    try {
      value = await response.json();
    } catch (_error) {
      throw new WebchatClientError('Webchat responseを解析できません', {
        code: 'invalid-response',
      });
    }
    if (
      value?.schema_version !== 1
      || typeof value.request_id !== 'string'
      || typeof value.state?.id !== 'string'
      || !Number.isInteger(value.state?.revision)
      || typeof value.state_token !== 'string'
      || !Array.isArray(value.messages)
      || !(value.echo_message == null || typeof value.echo_message === 'string')
    ) {
      throw new WebchatClientError('Webchat responseの形式が不正です', {
        code: 'invalid-response',
      });
    }
    return value;
  };

  const runLocked = async (callback) => {
    if (globalThis.navigator?.locks?.request) {
      return globalThis.navigator.locks.request(lockName, callback);
    }
    return callback();
  };

  const transact = async (input, { silent = false } = {}) => {
    if (inFlight) {
      throw new WebchatClientError('別のrequestを処理中です', {
        code: 'request-in-flight',
      });
    }
    const requestedStateId = snapshot.stateId;
    inFlight = true;
    try {
      return await runLocked(async () => {
        await initialize();
        const before = await storage.load();
        const baseStateId = before.head?.stateId || null;
        if (requestedStateId !== baseStateId) {
          await refresh(silent
            ? null
            : '別のタブで会話が進んだため、最新履歴を表示しました。');
          const error = new WebchatClientError(
            '別のタブの最新状態を表示しました。入力内容を確認して再送してください。',
            { code: 'state-refreshed' },
          );
          if (!silent) emit({ ...snapshot, status: 'error', error });
          throw error;
        }
        emit({ ...snapshot, status: 'sending', error: null, notice: null });
        const body = { input };
        if (before.head?.stateToken) body.state_token = before.head.stateToken;
        try {
          const response = await postTurn(body);
          let historyPruned = false;
          let memoryFallback = false;
          try {
            historyPruned = await storage.commit(baseStateId, response);
          } catch (error) {
            if (
              storage.kind !== 'indexeddb'
              || error?.name !== 'QuotaExceededError'
            ) throw error;
            const fallback = new MemoryStorage(key);
            fallback.seed(before);
            await fallback.commit(baseStateId, response);
            storage.close?.();
            storage = fallback;
            detachSharedListeners();
            memoryFallback = true;
          }
          if (!memoryFallback) notify();
          return refresh(memoryFallback
            ? 'browserへ永続保存できないため、このページだけで進行を保持します。'
            : (historyPruned
              ? 'browserの保存容量に合わせ、古い履歴を削除しました。'
              : null));
        } catch (error) {
          if (error instanceof StaleStateError) {
            await refresh(silent
              ? null
              : '別のタブの応答を採用し、最新履歴を表示しました。');
            const staleError = new WebchatClientError(
              '別のタブの最新状態を表示しました。入力内容を確認して再送してください。',
              { code: 'state-refreshed' },
            );
            if (!silent) {
              emit({ ...snapshot, status: 'error', error: staleError });
            }
            throw staleError;
          }
          if (silent) {
            await refresh();
          } else {
            emit({ ...snapshot, status: 'error', error, notice: null });
          }
          throw error;
        }
      });
    } catch (error) {
      if (error instanceof WebchatClientError) throw error;
      if (error instanceof CorruptStorageError) {
        const corruptError = new WebchatClientError(
          '保存した進行の形式が不正です。「最初から」で再開してください。',
          { code: 'storage-corrupt' },
        );
        emit({ ...snapshot, status: 'error', error: corruptError });
        throw corruptError;
      }
      const persistenceError = new WebchatClientError(
        'browserの保存領域を更新できませんでした。',
        { code: 'persistence-error' },
      );
      emit({ ...snapshot, status: 'error', error: persistenceError });
      throw persistenceError;
    } finally {
      inFlight = false;
    }
  };

  const onStorage = (event) => {
    if (event.key === `xstorybot-webchat-beacon:${key}`) {
      refresh('別のタブの最新状態を表示しました。').catch(() => {});
    }
  };
  const onFocus = () => refresh().catch(() => {});
  const onVisibility = () => {
    if (globalThis.document?.visibilityState === 'visible') {
      refresh().catch(() => {});
    }
  };

  return {
    initialize,
    async start() {
      await initialize();
      if (snapshot.stateId) return snapshot;
      return transact({ type: 'start' });
    },
    async sendText(text) {
      if (typeof text !== 'string') throw new TypeError('textは文字列です');
      return transact({ type: 'text', text });
    },
    async sendPostback(token, options = {}) {
      if (typeof token !== 'string') throw new TypeError('tokenは文字列です');
      return transact(
        { type: 'postback', postback_token: token },
        { silent: options?.silent === true },
      );
    },
    async reset() {
      await initialize();
      await runLocked(async () => storage.reset());
      notify();
      await refresh();
      return transact({ type: 'start' });
    },
    async clearHistory() {
      await initialize();
      await runLocked(async () => storage.clearHistory());
      notify();
      return refresh();
    },
    subscribe(listener) {
      listeners.add(listener);
      listener(snapshot);
      return () => listeners.delete(listener);
    },
    getSnapshot: () => snapshot,
    getServerSnapshot: () => serverSnapshot,
    destroy() {
      lifecycle += 1;
      initialized = false;
      initializePromise = null;
      detachSharedListeners();
      storage.close?.();
      listeners.clear();
    },
  };
}
