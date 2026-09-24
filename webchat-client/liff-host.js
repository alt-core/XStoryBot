const PROTOCOL = 'xstorybot-liff';
const VERSION = 1;
const messages = {
  'invalid-input': '操作の指定が不正です。',
  unavailable: 'この環境では利用できません。起動方法や設定を確認してください。',
  busy: '処理中です。完了してから操作してください。',
  'state-refreshed': '別のタブで状態が変わりました。表示を更新して操作を確認してください。',
  'request-failed': '処理に失敗しました。',
  'outcome-unknown': '処理結果を確認できません。操作を繰り返さず、一度閉じて開き直してください。',
};

class BridgeError extends Error {
  constructor(code, { cause, requestId = null } = {}) {
    const normalized = Object.prototype.hasOwnProperty.call(messages, code) ? code : 'request-failed';
    super(messages[normalized]);
    this.name = 'BridgeError';
    this.code = normalized;
    this.requestId = requestId;
    if (cause !== undefined) this.cause = cause;
  }
}

const failure = (error) => {
  if (error instanceof BridgeError) return error;
  const code = error?.code;
  let mapped = 'request-failed';
  if (code === 'request-in-flight') mapped = 'busy';
  else if (code === 'state-refreshed') mapped = code;
  else if (['network-error', 'invalid-response', 'persistence-error',
    'turn-timeout', 'external-http-timeout'].includes(code)) mapped = 'outcome-unknown';
  else if (['invalid-state', 'incompatible-state', 'service-unavailable',
    'invalid-origin'].includes(code)
    || [401, 403].includes(error?.status)) mapped = 'unavailable';
  const result = new BridgeError(mapped, { cause: error, requestId: error?.requestId });
  if (['invalid-state', 'incompatible-state'].includes(code)) {
    result.message = '保存した進行を確認できません。ページを閉じ、Webchatの「最初から」で再開してください。';
  }
  return result;
};

const envelope = (type, values) => ({ protocol: PROTOCOL, version: VERSION, type, ...values });
const isEnvelope = (data) => data?.protocol === PROTOCOL && data?.version === VERSION;
let nextId = 0;
const identifier = () => `${Date.now()}-${++nextId}-${Math.random().toString(36).slice(2)}`;

const httpUrl = (value) => {
  try {
    const url = new URL(value);
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) throw new Error();
    return url;
  } catch (error) {
    throw new BridgeError('invalid-input', { cause: error });
  }
};

export function findLiffApp(href, apps) {
  return resolveLiffLink(href, apps)?.app || null;
}

// URLの読み替えと所属判定を共用し、APIへ渡すapp.urlは登録値のまま保つ。
export function resolveLiffLink(href, apps) {
  let source;
  try { source = httpUrl(href); }
  catch (_error) { return null; }
  let best = null;
  let bestLength = -1;
  for (const app of apps) {
    try {
      const candidate = httpUrl(app.url);
      let url = source;
      const alias = `/${app.liff_id}`;
      if (app.liff_id && source.origin === 'https://liff.line.me'
          && (source.pathname === alias || source.pathname.startsWith(`${alias}/`))) {
        url = new URL(candidate.href);
        const extra = source.pathname.slice(alias.length);
        if (extra) url.pathname = candidate.pathname.replace(/\/$/, '') + extra;
        for (const [key, value] of source.searchParams) url.searchParams.append(key, value);
        if (source.hash) url.hash = source.hash;
      }
      const base = candidate.pathname.replace(/\/$/, '');
      const matches = url.pathname === candidate.pathname || (app.match === 'prefix'
        && (url.pathname === base || url.pathname.startsWith(`${base}/`)));
      if (url.origin === candidate.origin && matches && candidate.pathname.length > bestLength) {
        best = { app, url: url.href };
        bestLength = candidate.pathname.length;
      }
    } catch (_error) { /* 不正な登録値は候補にしない。 */ }
  }
  return best;
}

// 親画面用。呼出先は親の設定で固定し、子からBotやセーブを受け取らない。
export function attachLiffFrame(frame, { app, request, sendText, close }) {
  const window = globalThis.window;
  const origin = httpUrl(app.url).origin;
  let session = null;
  let closed = false;
  let busy = false;
  let pendingConnection = null;
  const respond = (target, type, data) => target.postMessage(envelope(type, data), origin);
  const finishConnection = () => {
    if (closed || busy || !pendingConnection) return;
    const { source, id } = pendingConnection;
    pendingConnection = null;
    session = identifier();
    respond(source, 'connected', { id, session });
  };
  const listen = async (event) => {
    if (closed || event.source !== frame.contentWindow || event.origin !== origin
        || !isEnvelope(event.data)) return;
    const data = event.data;
    if (data.type === 'connect' && typeof data.id === 'string') {
      session = null;
      // 操作の完了を待つ間にページが再び移った場合は、最新の接続だけを残す。
      pendingConnection = { source: event.source, id: data.id };
      finishConnection();
      return;
    }
    if (!session || data.session !== session) return;
    if (data.type === 'disconnect') { session = null; return; }
    if (data.type === 'close') { close(); return; }
    if (data.type !== 'invoke' || typeof data.id !== 'string') return;
    const currentSession = session;
    const reply = (values) => {
      if (!closed && session === currentSession) {
        respond(event.source, 'result', { id: data.id, session, ...values });
      }
    };
    if (busy) { reply({ ok: false, error: { code: 'busy' } }); return; }
    busy = true;
    try {
      let value;
      if (data.method === 'request' && typeof data.args?.action === 'string') {
        value = await request(data.args.action);
        if (!Array.isArray(value)) throw new BridgeError('outcome-unknown');
      } else if (data.method === 'sendText' && typeof data.args?.text === 'string') {
        await sendText(data.args.text);
        value = null;
      } else {
        throw new BridgeError('invalid-input');
      }
      reply({ ok: true, value });
    } catch (error) {
      const normalized = failure(error);
      reply({ ok: false, error: {
        code: normalized.code, message: normalized.message, requestId: normalized.requestId,
      } });
    } finally {
      busy = false;
      finishConnection();
    }
  };
  window.addEventListener('message', listen);
  return {
    destroy() {
      closed = true;
      session = null;
      pendingConnection = null;
      window.removeEventListener('message', listen);
    },
  };
}
