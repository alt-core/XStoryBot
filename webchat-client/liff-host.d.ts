export type LiffApp = { id: string; url: string; match?: 'exact' | 'prefix'; liff_id?: string };

export function findLiffApp(href: string, apps: LiffApp[]): LiffApp | null;
export function resolveLiffLink(href: string, apps: LiffApp[]): { app: LiffApp; url: string } | null;
export function attachLiffFrame(frame: HTMLIFrameElement, options: {
  app: LiffApp;
  request(action: string): Promise<unknown[]>;
  sendText(text: string): Promise<unknown>;
  close(): void;
}): { destroy(): void };
