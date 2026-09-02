export type WebchatSender = {
  name: string;
  icon_url: string | null;
};

export type WebchatAction =
  | { type: 'message'; label: string; text: string; echo_text?: string | null }
  | { type: 'postback'; label: string; token: string; echo_text: string | null }
  | { type: 'uri'; label: string; href: string };

export type WebchatMessageBase = {
  id: string;
  role: 'assistant';
  sender: WebchatSender | null;
  quick_replies?: WebchatAction[];
};

export type WebchatMessage =
  | (WebchatMessageBase & { type: 'text'; text: string })
  | (WebchatMessageBase & {
      type: 'image'; original_url: string; preview_url: string; alt: string;
    })
  | (WebchatMessageBase & {
      type: 'audio'; url: string; duration_ms: number; mime_type: string | null;
    })
  | (WebchatMessageBase & {
      type: 'video'; url: string; poster_url: string;
      completion_action?: Extract<WebchatAction, { type: 'postback' }>;
    })
  | (WebchatMessageBase & {
      type: 'button'; text: string; title: string | null;
      image_url: string | null; actions: WebchatAction[];
    })
  | (WebchatMessageBase & {
      type: 'imagemap'; image_url: string; width: number; height: number;
      alt: string; sources: Array<{ url: string; width: number }>;
      areas: Array<{
        x: number; y: number; width: number; height: number;
        action: WebchatAction;
      }>;
    });

export type WebchatTurn = {
  id: string;
  sequence: number;
  requestId: string;
  echoMessage: string | null;
  messages: WebchatMessage[];
};

export type WebchatSnapshot = {
  status: 'idle' | 'loading' | 'ready' | 'sending' | 'error';
  persistence: 'indexeddb' | 'memory';
  stateId: string | null;
  stateRevision: number | null;
  turns: WebchatTurn[];
  messages: WebchatMessage[];
  activeResponse: WebchatMessage[];
  error: WebchatClientError | null;
  notice: string | null;
};

export class WebchatClientError extends Error {
  status: number;
  code: string;
  requestId: string | null;
  retryAfter: string | null;
}

export type WebchatClient = {
  initialize(): Promise<WebchatSnapshot>;
  start(): Promise<WebchatSnapshot>;
  sendText(text: string): Promise<WebchatSnapshot>;
  sendPostback(
    token: string,
    options?: { silent?: boolean },
  ): Promise<WebchatSnapshot>;
  reset(): Promise<WebchatSnapshot>;
  clearHistory(): Promise<WebchatSnapshot>;
  subscribe(listener: (snapshot: WebchatSnapshot) => void): () => void;
  getSnapshot(): WebchatSnapshot;
  getServerSnapshot(): WebchatSnapshot;
  destroy(): void;
};

export function createWebchatClient(options: {
  apiBaseUrl: string;
  bot: string;
  fetch?: typeof globalThis.fetch;
  indexedDB?: IDBFactory;
}): WebchatClient;
