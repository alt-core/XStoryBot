import { useEffect, useMemo, useSyncExternalStore } from 'react';
import { createWebchatClient } from '../../index.js';

export function useWebchat({ apiBaseUrl, bot }) {
  const client = useMemo(
    () => createWebchatClient({ apiBaseUrl, bot }),
    [apiBaseUrl, bot],
  );
  const snapshot = useSyncExternalStore(
    client.subscribe,
    client.getSnapshot,
    client.getServerSnapshot,
  );
  useEffect(() => {
    client.initialize().then(() => client.start()).catch(() => {});
    return () => client.destroy();
  }, [client]);
  return { client, snapshot };
}
