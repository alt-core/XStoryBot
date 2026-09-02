export const createControlActivator = (
  activate, { cooldownMs = 350, setTimer = setTimeout } = {},
) => {
  let locked = false;
  return async (action) => {
    if (locked) return false;
    locked = true;
    try {
      await activate(action);
      return true;
    } finally {
      setTimer(() => {
        locked = false;
      }, cooldownMs);
    }
  };
};

export const classifyUriTarget = (href, currentOrigin = '') => {
  let url;
  try {
    url = new URL(href);
  } catch (_error) {
    return 'blocked';
  }
  if (url.protocol === 'tel:') return 'native';
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    return 'blocked';
  }
  if (url.searchParams.get('openExternalBrowser') === '1') {
    return 'external';
  }
  return (
    url.protocol === 'https:' || (currentOrigin && url.origin === currentOrigin)
  ) ? 'embedded' : 'blocked';
};

export const createVideoPlaybackController = ({
  play,
  pause,
  isPaused,
}) => {
  const start = () => {
    try {
      const result = play();
      result?.catch?.(() => {});
    } catch (_error) {
      // 次の利用者tapで再試行できるため、ここでは表示を増やさない。
    }
  };

  const toggle = () => {
    if (isPaused()) {
      start();
    } else {
      pause();
    }
  };

  return { start, toggle };
};

export const createHorizontalDragController = ({
  getScrollLeft,
  setScrollLeft,
  threshold = 6,
  setTimer = setTimeout,
}) => {
  let pointerId = null;
  let startX = 0;
  let startScrollLeft = 0;
  let dragging = false;
  let suppressClick = false;

  const pointerDown = ({
    pointerId: nextPointerId,
    clientX,
    button,
    pointerType,
    scrollable,
  }) => {
    if (
      pointerId !== null || pointerType !== 'mouse' || button !== 0
      || !scrollable
    ) return false;
    pointerId = nextPointerId;
    startX = clientX;
    startScrollLeft = getScrollLeft();
    dragging = false;
    return true;
  };

  const pointerMove = ({ pointerId: currentPointerId, clientX }) => {
    if (currentPointerId !== pointerId) {
      return { handled: false, dragging: false };
    }
    const distance = clientX - startX;
    if (!dragging && Math.abs(distance) < threshold) {
      return { handled: true, dragging: false };
    }
    dragging = true;
    setScrollLeft(startScrollLeft - distance);
    return { handled: true, dragging: true };
  };

  const reset = () => {
    pointerId = null;
    dragging = false;
  };

  const pointerUp = (currentPointerId) => {
    if (currentPointerId !== pointerId) return false;
    const wasDragging = dragging;
    if (wasDragging) {
      suppressClick = true;
      setTimer(() => {
        suppressClick = false;
      }, 0);
    }
    reset();
    return wasDragging;
  };

  const pointerCancel = (currentPointerId) => {
    if (currentPointerId !== pointerId) return false;
    reset();
    return true;
  };

  const consumeClick = () => {
    if (!suppressClick) return false;
    suppressClick = false;
    return true;
  };

  return {
    pointerDown,
    pointerMove,
    pointerUp,
    pointerCancel,
    consumeClick,
  };
};

export const createBottomResizeFollower = ({
  getScrollTop,
  getClientHeight,
  getScrollHeight,
  isFollowing,
  scrollToBottom,
  threshold = 64,
}) => {
  let previousScrollHeight = getScrollHeight();

  const sync = () => {
    const currentScrollHeight = getScrollHeight();
    const wasAtBottom = isFollowing()
      || getScrollTop() + getClientHeight()
        >= previousScrollHeight - threshold;
    previousScrollHeight = currentScrollHeight;
    if (!wasAtBottom) return false;
    scrollToBottom();
    return true;
  };

  const refresh = () => {
    previousScrollHeight = getScrollHeight();
  };

  return { sync, refresh };
};

export const createVideoCompletionQueue = ({
  send,
  getStatus,
  beforeSend = () => {},
  enqueueTask = queueMicrotask,
}) => {
  const completed = new Set();
  const pending = new Map();
  let flushing = false;

  const flush = async () => {
    if (flushing || getStatus() === 'sending' || pending.size === 0) {
      return false;
    }
    flushing = true;
    const [messageId, action] = pending.entries().next().value;
    let removePending = false;
    try {
      beforeSend();
      await send(action);
      removePending = true;
    } catch (error) {
      // client側のrequestが先に始まった場合だけ、readyへ戻った後に再試行する。
      if (error?.code !== 'request-in-flight') removePending = true;
    } finally {
      if (removePending) {
        pending.delete(messageId);
        completed.add(messageId);
      }
      flushing = false;
      if (removePending && pending.size > 0 && getStatus() !== 'sending') {
        enqueueTask(() => {
          flush();
        });
      }
    }
    return removePending;
  };

  const enqueue = (messageId, action) => {
    if (
      !messageId || !action || completed.has(messageId)
      || pending.has(messageId)
    ) return false;
    pending.set(messageId, action);
    flush();
    return true;
  };

  return { enqueue, flush };
};
