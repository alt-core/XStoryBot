import type { WebchatClient } from './index.js';

/** 静的な体験版データを読み込み、会話APIなしで既存UIの操作を提供する。 */
export function createTrialClient(options: {
  bot: string;
  programUrl: string | URL;
  fetch?: typeof globalThis.fetch;
  indexedDB?: IDBFactory;
}): WebchatClient;
