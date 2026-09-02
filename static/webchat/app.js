import { createWebchatClient } from '/webchat-client/index.js';
import {
  classifyUriTarget,
  createBottomResizeFollower,
  createControlActivator,
  createHorizontalDragController,
  createVideoPlaybackController,
  createVideoCompletionQueue,
} from '/static/webchat/ui_logic.mjs';

const pathParts = location.pathname.split('/').filter(Boolean);
const bot = decodeURIComponent(pathParts[pathParts.length - 1] || 'bot');
const client = createWebchatClient({ apiBaseUrl: location.origin, bot });

const scroller = document.querySelector('#scroller');
const messagesElement = document.querySelector('#messages');
const bannerElement = document.querySelector('#banner');
const noticeElement = document.querySelector('#notice');
const errorElement = document.querySelector('#error');
const composer = document.querySelector('#composer');
const draft = document.querySelector('#draft');
const sendButton = document.querySelector('#send');
const resetButton = document.querySelector('#reset');
const jumpButton = document.querySelector('#jump');
const mediaViewer = document.querySelector('#media-viewer');
const mediaViewerClose = document.querySelector('#media-viewer-close');
const nativeMediaViewer = (
  typeof mediaViewer.showModal === 'function'
  && typeof mediaViewer.close === 'function');

const historyElement = document.createElement('div');
const ephemeralElement = document.createElement('div');
messagesElement.append(historyElement, ephemeralElement);

const touchLike = matchMedia('(hover: none) and (pointer: coarse)');
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');

const EXTERNAL_ICON =
  '<svg class="external" viewBox="0 0 16 16" aria-hidden="true" '
  + 'fill="none" stroke="currentColor" stroke-width="1.6">'
  + '<path d="M6.5 3.5H3.5v9h9V9.5M9.5 2.5h4v4M13 3 7.5 8.5"/></svg>';

// 送信中だけ表示する自分側のメッセージ。応答のechoで確定表示に置き換わる。
let pendingEcho = null;
let messagesResizeObserver = null;
let bottomResizeFollower = null;

// ---- 送信操作 ----

const autosize = () => {
  draft.style.height = 'auto';
  // border-box指定のためborder分を足す。不足すると常に数pxだけ
  // スクロール可能になり、スクロールバーが出続ける。
  const borders = draft.offsetHeight - draft.clientHeight;
  const maxHeight = parseFloat(getComputedStyle(draft).maxHeight);
  const target = draft.scrollHeight + borders;
  const limit = Number.isFinite(maxHeight) ? maxHeight : Infinity;
  draft.style.height = `${Math.min(target, limit)}px`;
  draft.style.overflowY = target > limit ? 'auto' : 'hidden';
};

const updateSendState = () => {
  const sending = client.getSnapshot().status === 'sending';
  sendButton.disabled = sending || !draft.value.trim();
};

const refocusDraft = () => {
  draft.focus({ preventScroll: true });
};

const submitText = async () => {
  const text = draft.value;
  if (!text.trim() || client.getSnapshot().status === 'sending') return;
  draft.value = '';
  autosize();
  updateSendState();
  pendingEcho = text;
  try {
    await client.sendText(text);
  } catch (_error) {
    // 文言はsnapshotのerrorに表示済み。未入力なら下書きを復元する。
    if (!draft.value) {
      draft.value = text;
      autosize();
    }
  }
  updateSendState();
  refocusDraft();
};

// clientが'sending'をemitするまでの短い窓だけ同期lockで塞ぐ。
let actionLock = false;

const activate = async (action) => {
  if (actionLock || client.getSnapshot().status === 'sending') return false;
  actionLock = true;
  try {
    if (action.type === 'message') {
      pendingEcho = action.text;
      await client.sendText(action.text);
    } else if (action.type === 'postback') {
      pendingEcho = action.echo_text ?? null;
      await client.sendPostback(action.token);
    }
  } catch (_error) {
    // 状態と文言はsnapshotへ反映済み。
  } finally {
    actionLock = false;
  }
  return true;
};

const bindAction = (control, action) => {
  // cooldownはcontrolごとのclosureへ閉じる。応答後に描画された新しい
  // Quick Replyを、直前controlの二重clickと一緒に抑止しない。
  const activateControl = createControlActivator(activate);
  control.addEventListener('click', () => {
    activateControl(action);
  });
};

const videoCompletionQueue = createVideoCompletionQueue({
  send: (action) => client.sendPostback(action.token, { silent: true }),
  getStatus: () => client.getSnapshot().status,
  beforeSend: () => {
    // 動画完了は利用者の発言として表示しない。
    pendingEcho = null;
  },
});

// ---- メディア拡大表示 ----

let mediaViewerReturnFocus = null;

const clearMediaViewer = () => {
  for (const child of [...mediaViewer.children]) {
    if (child !== mediaViewerClose) child.remove();
  }
  mediaViewer.classList.remove('link-viewer');
  mediaViewer.classList.remove('fallback-open');
  mediaViewer.classList.remove('pointer-open');
  mediaViewer.removeAttribute('aria-modal');
  if (mediaViewerReturnFocus?.isConnected) {
    mediaViewerReturnFocus.focus({ preventScroll: true });
  }
  mediaViewerReturnFocus = null;
};

const closeMediaViewer = () => {
  if (!mediaViewer.hasAttribute('open')) return;
  if (nativeMediaViewer) {
    mediaViewer.close();
  } else {
    mediaViewer.removeAttribute('open');
    clearMediaViewer();
  }
};

const showMediaViewer = (
  content, trigger, mode = 'media', pointerOpened = false,
) => {
  if (mediaViewer.hasAttribute('open')) return false;
  mediaViewerReturnFocus = trigger;
  mediaViewer.classList.toggle('link-viewer', mode === 'link');
  mediaViewer.classList.toggle('pointer-open', pointerOpened);
  mediaViewer.append(content);
  if (nativeMediaViewer) {
    mediaViewer.showModal();
  } else {
    mediaViewer.classList.add('fallback-open');
    mediaViewer.setAttribute('aria-modal', 'true');
    mediaViewer.setAttribute('open', '');
    mediaViewerClose.focus({ preventScroll: true });
  }
  return true;
};

const openMediaViewer = (message, trigger, pointerOpened) => {
  let media;
  let playback = null;
  if (message.type === 'video') {
    media = document.createElement('video');
    media.playsInline = true;
    media.preload = 'metadata';
    media.src = message.url;
    media.poster = message.poster_url || '';
    media.tabIndex = 0;
    media.setAttribute('role', 'button');
    const updateLabel = () => {
      media.setAttribute(
        'aria-label',
        media.paused
          ? '動画。一時停止中。タップまたはSpaceで再生'
          : '動画。再生中。タップまたはSpaceで一時停止');
    };
    playback = createVideoPlaybackController({
      play: () => media.play(),
      pause: () => media.pause(),
      isPaused: () => media.paused,
    });
    media.addEventListener('click', playback.toggle);
    media.addEventListener('keydown', (event) => {
      if (event.key !== ' ' && event.key !== 'Enter') return;
      event.preventDefault();
      playback.toggle();
    });
    media.addEventListener('play', updateLabel);
    media.addEventListener('pause', updateLabel);
    updateLabel();
    if (message.completion_action) {
      media.addEventListener('ended', () => {
        closeMediaViewer();
        videoCompletionQueue.enqueue(
          message.id, message.completion_action);
      });
    }
  } else {
    media = document.createElement('img');
    media.src = message.original_url;
    media.alt = message.alt || '画像';
  }
  if (!showMediaViewer(media, trigger, 'media', pointerOpened)) return;
  playback?.start();
};

const openLinkViewer = (href, label, trigger, pointerOpened) => {
  const frame = document.createElement('iframe');
  frame.className = 'link-viewer-frame';
  frame.src = href;
  frame.title = label || 'リンク先';
  frame.referrerPolicy = 'no-referrer';
  frame.setAttribute(
    'sandbox', 'allow-forms allow-scripts allow-same-origin');
  showMediaViewer(frame, trigger, 'link', pointerOpened);
};

mediaViewerClose.addEventListener('click', closeMediaViewer);
mediaViewer.addEventListener('click', (event) => {
  if (event.target === mediaViewer) closeMediaViewer();
});
mediaViewer.addEventListener('cancel', (event) => {
  event.preventDefault();
  closeMediaViewer();
});
mediaViewer.addEventListener('keydown', (event) => {
  mediaViewer.classList.remove('pointer-open');
  if (nativeMediaViewer || event.key !== 'Escape') return;
  event.preventDefault();
  closeMediaViewer();
});
mediaViewer.addEventListener('close', clearMediaViewer);

// ---- メッセージのDOM生成 ----

const linkAttributes = (link, href, external = false) => {
  link.href = href;
  if (external) {
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
  }
};

const uriControl = (action, className, hideLabel = false) => {
  const label = action.label || action.href || 'リンク';
  const mode = classifyUriTarget(action.href, location.origin);
  let control;
  if (mode === 'embedded') {
    control = document.createElement('button');
    control.type = 'button';
    control.setAttribute('aria-haspopup', 'dialog');
    control.addEventListener('click', (event) => {
      openLinkViewer(action.href, label, control, event.detail > 0);
    });
  } else if (mode === 'external' || mode === 'native') {
    control = document.createElement('a');
    linkAttributes(control, action.href, mode === 'external');
  } else {
    control = document.createElement('button');
    control.type = 'button';
    control.disabled = true;
    control.dataset.alwaysDisabled = 'true';
    control.title = 'HTTPSではないためチャット内で表示できません';
  }
  control.className = className;
  control.dataset.linkMode = mode;
  control.setAttribute(
    'aria-label', mode === 'blocked'
      ? `${label}（この環境では開けません）` : label);
  if (!hideLabel) control.textContent = label;
  return control;
};

const watchMediaLoad = (element) => {
  const eventName = element.tagName === 'VIDEO' ? 'loadedmetadata' : 'load';
  element.addEventListener(eventName, () => {
    requestAnimationFrame(() => {
      bottomResizeFollower?.sync();
    });
  }, { once: true });
};

const textBubble = (text) => {
  const bubble = document.createElement('div');
  bubble.className = 'bubble text enter';
  bubble.textContent = text;
  return bubble;
};

const actionButton = (action, className) => {
  if (!action || typeof action !== 'object') {
    const unavailable = document.createElement('span');
    unavailable.className = className;
    unavailable.textContent = '利用できない選択肢';
    return unavailable;
  }
  if (action.type === 'uri') {
    return uriControl(action, className);
  }
  const button = document.createElement('button');
  button.type = 'button';
  button.className = className;
  button.textContent = action.label || action.echo_text || action.text || '選択';
  bindAction(button, action);
  return button;
};

const bindHorizontalDrag = (container) => {
  const controller = createHorizontalDragController({
    getScrollLeft: () => container.scrollLeft,
    setScrollLeft: (value) => {
      container.scrollLeft = value;
    },
  });

  container.addEventListener('pointerdown', (event) => {
    controller.pointerDown({
      pointerId: event.pointerId,
      clientX: event.clientX,
      button: event.button,
      pointerType: event.pointerType,
      scrollable: container.scrollWidth > container.clientWidth,
    });
  });
  container.addEventListener('pointermove', (event) => {
    const state = controller.pointerMove({
      pointerId: event.pointerId,
      clientX: event.clientX,
    });
    if (!state.dragging) return;
    if (!container.hasPointerCapture(event.pointerId)) {
      container.setPointerCapture(event.pointerId);
    }
    container.classList.add('is-dragging');
    event.preventDefault();
  });
  const finish = (event, cancelled = false) => {
    if (cancelled) {
      controller.pointerCancel(event.pointerId);
    } else {
      controller.pointerUp(event.pointerId);
    }
    container.classList.remove('is-dragging');
    if (container.hasPointerCapture(event.pointerId)) {
      container.releasePointerCapture(event.pointerId);
    }
  };
  container.addEventListener('pointerup', (event) => finish(event));
  container.addEventListener(
    'pointercancel', (event) => finish(event, true));
  container.addEventListener('click', (event) => {
    if (!controller.consumeClick()) return;
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);
};

const renderQuickReplies = (message) => {
  const container = document.createElement('div');
  container.className = 'quick-replies';
  container.dataset.messageId = message.id;
  container.setAttribute('role', 'group');
  container.setAttribute('aria-label', '返信の候補');
  container.hidden = true;
  for (const action of message.quick_replies || []) {
    const chip = actionButton(action, 'chip');
    if (chip.dataset.linkMode === 'external') {
      chip.insertAdjacentHTML('beforeend', EXTERNAL_ICON);
    }
    container.append(chip);
  }
  bindHorizontalDrag(container);
  return container;
};

const renderMessage = (message) => {
  if (message.type === 'text') {
    return textBubble(message.text);
  }
  if (message.type === 'image') {
    const bubble = document.createElement('div');
    bubble.className = 'bubble media enter';
    const trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'media-trigger';
    trigger.setAttribute('aria-haspopup', 'dialog');
    trigger.setAttribute(
      'aria-label', `${message.alt || '画像'}を拡大表示`);
    const image = document.createElement('img');
    image.src = message.preview_url || message.original_url;
    image.alt = '';
    image.loading = 'lazy';
    watchMediaLoad(image);
    trigger.append(image);
    trigger.addEventListener('click', (event) => {
      openMediaViewer(message, trigger, event.detail > 0);
    });
    bubble.append(trigger);
    return bubble;
  }
  if (message.type === 'audio') {
    const bubble = document.createElement('div');
    bubble.className = 'bubble audio enter';
    const audio = document.createElement('audio');
    audio.controls = true;
    audio.preload = 'metadata';
    audio.src = message.url;
    bubble.append(audio);
    return bubble;
  }
  if (message.type === 'video') {
    const bubble = document.createElement('div');
    bubble.className = 'bubble media enter';
    const trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'media-trigger';
    trigger.setAttribute('aria-haspopup', 'dialog');
    trigger.setAttribute('aria-label', '動画を拡大して再生');
    const video = document.createElement('video');
    video.muted = true;
    video.playsInline = true;
    video.preload = 'metadata';
    video.src = message.url;
    video.poster = message.poster_url || '';
    video.setAttribute('aria-hidden', 'true');
    watchMediaLoad(video);
    const play = document.createElement('span');
    play.className = 'media-trigger-play';
    play.setAttribute('aria-hidden', 'true');
    play.textContent = '▶';
    trigger.append(video, play);
    trigger.addEventListener('click', (event) => {
      openMediaViewer(message, trigger, event.detail > 0);
    });
    bubble.append(trigger);
    return bubble;
  }
  if (message.type === 'button') {
    const bubble = document.createElement('div');
    bubble.className = 'bubble card enter';
    if (message.image_url) {
      const image = document.createElement('img');
      image.className = 'card-image';
      image.src = message.image_url;
      image.alt = '';
      watchMediaLoad(image);
      bubble.append(image);
    }
    const body = document.createElement('div');
    body.className = 'card-body';
    if (message.title) {
      const title = document.createElement('p');
      title.className = 'card-title';
      title.textContent = message.title;
      body.append(title);
    }
    if (message.text) {
      const text = document.createElement('p');
      text.className = 'card-text';
      text.textContent = message.text;
      body.append(text);
    }
    bubble.append(body);
    const actions = document.createElement('div');
    actions.className = 'card-actions';
    for (const action of message.actions || []) {
      actions.append(actionButton(action, 'card-action'));
    }
    bubble.append(actions);
    return bubble;
  }
  if (message.type === 'imagemap') {
    const bubble = document.createElement('div');
    bubble.className = 'bubble media imagemap-bubble enter';
    const map = document.createElement('div');
    map.className = 'imagemap';
    map.style.aspectRatio = `${message.width} / ${message.height}`;
    const image = document.createElement('img');
    image.src = message.image_url;
    if (message.sources?.length) {
      image.srcset = message.sources
        .map((source) => `${source.url} ${source.width}w`)
        .join(', ');
      image.sizes = '(max-width: 46rem) 100vw, 46rem';
    }
    image.alt = message.alt || '選択可能な画像';
    watchMediaLoad(image);
    map.append(image);
    for (const area of message.areas || []) {
      const action = area.action || {};
      let hotspot;
      if (action.type === 'uri') {
        hotspot = uriControl(action, 'hotspot', true);
      } else {
        hotspot = document.createElement('button');
        hotspot.type = 'button';
        bindAction(hotspot, action);
        hotspot.className = 'hotspot';
      }
      hotspot.setAttribute(
        'aria-label', action.label || action.text || '選択');
      hotspot.style.left = `${(area.x / message.width) * 100}%`;
      hotspot.style.top = `${(area.y / message.height) * 100}%`;
      hotspot.style.width = `${(area.width / message.width) * 100}%`;
      hotspot.style.height = `${(area.height / message.height) * 100}%`;
      map.append(hotspot);
    }
    bubble.append(map);
    return bubble;
  }
  return textBubble('(この環境では表示できないメッセージです)');
};

// ---- グループ化しながらの追記描画 ----

const historyState = {
  signatures: [],
  tail: null, // {senderKey, stackEl, lastText}
};

const turnSignature = (turn) =>
  `${turn.id}|${turn.requestId}|${(turn.messages || []).length}`;

const makeAvatar = (sender) => {
  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  if (sender?.icon_url) {
    const image = document.createElement('img');
    image.src = sender.icon_url;
    image.alt = '';
    avatar.append(image);
  } else if (!sender?.name) {
    avatar.classList.add('spacer');
  }
  return avatar;
};

const appendEntry = (role, sender, node, isText, wide = false) => {
  if (wide) {
    const group = document.createElement('div');
    group.className = `group ${role} wide`;
    const stack = document.createElement('div');
    stack.className = 'stack';
    stack.append(node);
    group.append(stack);
    historyElement.append(group);
    historyState.tail = null;
    return;
  }
  const senderKey = `${role}|${sender?.name ?? ''}`;
  let tail = historyState.tail;
  if (!tail || tail.senderKey !== senderKey) {
    const group = document.createElement('div');
    group.className = `group ${role}`;
    if (tail && tail.role === role) group.classList.add('continued');
    const stack = document.createElement('div');
    stack.className = 'stack';
    if (role === 'in') {
      group.append(makeAvatar(sender));
      if (sender?.name) {
        const name = document.createElement('div');
        name.className = 'sender-name';
        name.textContent = sender.name;
        stack.append(name);
      }
    }
    group.append(stack);
    historyElement.append(group);
    tail = { role, senderKey, stackEl: stack, lastText: null };
    historyState.tail = tail;
  }
  if (isText) {
    if (tail.lastText) {
      const previous = tail.lastText.classList;
      if (!previous.replace('g-solo', 'g-first')) {
        previous.replace('g-last', 'g-mid');
      }
      node.classList.add('g-last');
    } else {
      node.classList.add('g-solo');
    }
    tail.lastText = node;
  }
  tail.stackEl.append(node);
};

const appendTurn = (turn) => {
  if (turn.echoMessage != null) {
    appendEntry('out', null, textBubble(turn.echoMessage), true);
  }
  for (const message of turn.messages || []) {
    appendEntry(
      'in', message.sender, renderMessage(message),
      message.type === 'text', message.type === 'imagemap');
    if ((message.quick_replies || []).length) {
      const row = document.createElement('div');
      row.className = 'quick-replies-row';
      row.append(renderQuickReplies(message));
      historyElement.append(row);
      historyState.tail = null;
    }
  }
};

const syncHistory = (snapshot) => {
  const turns = snapshot.turns;
  const rendered = historyState.signatures;
  const isPrefix = rendered.length <= turns.length && rendered.every(
    (signature, index) => signature === turnSignature(turns[index]));
  let appended = false;
  if (!isPrefix) {
    historyElement.replaceChildren();
    historyState.signatures = [];
    historyState.tail = null;
    appended = turns.length > 0;
  }
  for (const turn of turns.slice(historyState.signatures.length)) {
    appendTurn(turn);
    historyState.signatures.push(turnSignature(turn));
    appended = true;
  }
  return appended;
};

const syncEphemeral = (snapshot) => {
  const sending = snapshot.status === 'sending';
  const signature = sending ? `sending|${pendingEcho ?? ''}` : '';
  if (ephemeralElement.dataset.signature === signature) return false;
  ephemeralElement.dataset.signature = signature;
  ephemeralElement.replaceChildren();
  if (!sending) return false;

  if (pendingEcho != null) {
    const group = document.createElement('div');
    group.className = 'group out';
    const stack = document.createElement('div');
    stack.className = 'stack';
    const bubble = textBubble(pendingEcho);
    bubble.classList.add('pending', 'g-solo');
    stack.append(bubble);
    group.append(stack);
    ephemeralElement.append(group);
  }
  const group = document.createElement('div');
  group.className = 'group in';
  group.setAttribute('aria-hidden', 'true');
  const spacer = document.createElement('div');
  spacer.className = 'avatar spacer';
  const stack = document.createElement('div');
  stack.className = 'stack';
  const typing = document.createElement('div');
  typing.className = 'typing enter';
  typing.append(...[0, 1, 2].map(() => document.createElement('i')));
  stack.append(typing);
  group.append(spacer, stack);
  ephemeralElement.append(group);
  return true;
};

// ---- 通知・エラー ----

const setPill = (element, text) => {
  element.textContent = text || '';
  element.hidden = !text;
};

const pillAction = (label, handler) => {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'pill-action';
  button.textContent = label;
  button.addEventListener('click', handler);
  return button;
};

const syncBanner = (snapshot) => {
  setPill(noticeElement, snapshot.notice);
  const error = snapshot.error;
  setPill(errorElement, error?.message);
  if (error) {
    if (['incompatible-state', 'invalid-state', 'storage-corrupt']
        .includes(error.code)) {
      errorElement.append(pillAction('最初から', () => {
        client.reset().catch(() => {});
      }));
    } else if (!snapshot.turns.length) {
      errorElement.append(pillAction('再読み込み', () => {
        client.start().catch(() => {});
      }));
    }
  }
  bannerElement.hidden = noticeElement.hidden && errorElement.hidden;
};

// ---- スクロール制御 ----

let stick = true;
let firstRender = true;

const scrollToBottom = (smooth) => {
  scroller.scrollTo({
    top: scroller.scrollHeight,
    behavior: smooth && !reducedMotion.matches ? 'smooth' : 'auto',
  });
  stick = true;
  jumpButton.hidden = true;
};

scroller.addEventListener('scroll', () => {
  stick = scroller.scrollTop + scroller.clientHeight
    >= scroller.scrollHeight - 64;
  if (stick) jumpButton.hidden = true;
}, { passive: true });

jumpButton.addEventListener('click', () => scrollToBottom(true));

bottomResizeFollower = createBottomResizeFollower({
  getScrollTop: () => scroller.scrollTop,
  getClientHeight: () => scroller.clientHeight,
  getScrollHeight: () => scroller.scrollHeight,
  isFollowing: () => stick,
  scrollToBottom: () => scrollToBottom(false),
});
if (typeof ResizeObserver === 'function') {
  messagesResizeObserver = new ResizeObserver(() => {
    bottomResizeFollower.sync();
  });
  messagesResizeObserver.observe(messagesElement);
}

// ---- 全体の描画 ----

const syncControls = (snapshot) => {
  const sending = snapshot.status === 'sending';
  resetButton.disabled = sending;
  updateSendState();
  const controls = messagesElement.querySelectorAll(
    'button.card-action, button.hotspot, .quick-replies button');
  for (const control of controls) {
    control.disabled = sending || control.dataset.alwaysDisabled === 'true';
  }
  const activeMessageIds = new Set(
    snapshot.activeResponse.map((message) => message.id));
  const quickReplyGroups = messagesElement.querySelectorAll(
    '.quick-replies[data-message-id]');
  for (const group of quickReplyGroups) {
    const active = activeMessageIds.has(group.dataset.messageId);
    const reserveSpace = sending && active;
    group.hidden = !active;
    group.classList.toggle('is-placeholder', reserveSpace);
    if (reserveSpace) {
      group.setAttribute('aria-hidden', 'true');
    } else {
      group.removeAttribute('aria-hidden');
    }
  }
};

const render = (snapshot) => {
  const wasStick = stick;
  const grewHistory = syncHistory(snapshot);
  const grewEphemeral = syncEphemeral(snapshot);
  syncBanner(snapshot);
  syncControls(snapshot);
  videoCompletionQueue.flush();
  if (grewHistory || grewEphemeral) {
    if (snapshot.status === 'sending' || wasStick || firstRender) {
      scrollToBottom(!firstRender);
    } else {
      jumpButton.hidden = false;
    }
  }
  bottomResizeFollower.refresh();
  firstRender = false;
};

client.subscribe(render);

// ---- 入力ボックス ----

composer.addEventListener('submit', (event) => {
  event.preventDefault();
  submitText();
});

draft.addEventListener('input', () => {
  autosize();
  updateSendState();
});

draft.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter') return;
  // 仮名漢字変換の確定Enterでは送信しない。長押しrepeatも無視。
  if (event.isComposing || event.keyCode === 229 || event.repeat) return;
  if (event.metaKey || event.ctrlKey) {
    event.preventDefault();
    submitText();
    return;
  }
  // Shift+Enterは改行。タッチ環境ではEnterも改行にして送信ボタンを使う。
  if (event.shiftKey || event.altKey || touchLike.matches) return;
  event.preventDefault();
  submitText();
});

resetButton.addEventListener('click', () => {
  const message = '会話を最初からはじめますか?\n'
    + 'このブラウザに保存された履歴も削除されます。';
  if (!window.confirm(message)) return;
  client.reset().catch(() => {});
});

autosize();

try {
  await client.initialize();
  await client.start();
} catch (_error) {
  // 正規化済みのerrorはsnapshot経由でbannerへ表示される。
}
