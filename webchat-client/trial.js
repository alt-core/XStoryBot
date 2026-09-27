import { _createWebchatSession, WebchatClientError } from './index.js';
import { createTrialRuntime } from './trial-runtime.js';

// 媒体と登録ページだけを解決し、台詞中のURLやラベルには触れない。
const urlFields = new Set(['url', 'href', 'uri', 'image_url', 'icon_url', 'original_url', 'preview_url', 'poster_url']);
function resolveUrls(value, base) {
  if (Array.isArray(value)) return value.map(child => resolveUrls(child, base));
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(Object.entries(value).map(([key, child]) => [key,
    urlFields.has(key) && typeof child === 'string' && child
      ? new URL(child, base).href : resolveUrls(child, base),
  ]));
}

export function createTrialClient({ bot, programUrl, fetch: fetchImpl = globalThis.fetch?.bind(globalThis), indexedDB, storage } = {}) {
  if (!bot || !programUrl) throw new TypeError('botとprogramUrlが必要です');
  let runtime = null;
  const local = {
    async prepare() {
      if (runtime) return;
      try {
        const url = new URL(programUrl, globalThis.location?.href);
        const response = await fetchImpl(url.href, { credentials: 'omit', cache: 'no-cache' });
        if (!response.ok) throw new Error();
        const program = resolveUrls(await response.json(), url.href);
        if (program.bot !== bot) throw new Error();
        runtime = createTrialRuntime(program);
      } catch (cause) {
        if (cause instanceof WebchatClientError) throw cause;
        throw new WebchatClientError('体験版データを読み込めません。ページを読み込み直してください。', { code: 'invalid-scenario' });
      }
    },
    restore: head => runtime.restore(head),
    execute: body => runtime.execute(body),
  };
  return _createWebchatSession({ bot, indexedDB, storage }, local);
}
