import { WebchatClientError } from './index.js';

const clone = value => JSON.parse(JSON.stringify(value));
const special = value => /^[#＃*＊]/.test(value);
const sanitize = value => /^[#＃*＊:：]/.test(value) ? ` ${value}` : value;
const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
const error = (message, code = 'invalid-state') => new WebchatClientError(message, { code });
const identifier = () => {
  if (typeof globalThis.crypto?.getRandomValues !== 'function') {
    throw error('このブラウザでは体験版を開始できません。別のブラウザで開いてください。', 'unsupported-browser');
  }
  // HTTP配信でも利用できる乱数から、衝突を避けるための128bitの識別子を作る。
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
};

export function createTrialRuntime(program) {
  if (program?.schema_version !== 1 || typeof program.bot !== 'string'
      || typeof program.epoch !== 'string' || !program.scenes || !program.blocks
      || !own(program.scenes, program.startup)) {
    throw error('体験版データの形式が不正です。', 'invalid-scenario');
  }
  const sourceText = source => source ? `${source.sheet}!${source.line}行目: ` : '';
  const regex = (pattern, flags, source) => {
    try { return new RegExp(pattern, flags); }
    catch (_cause) { throw error(`${sourceText(source)}JavaScriptで使えない正規表現です。`, 'invalid-scenario'); }
  };
  const ignore = program.ignore_pattern ? regex(program.ignore_pattern, '', null) : null;
  const operations = new Set(['emit', 'reply', 'wait', 'clear_wait', 'show_choices', 'reset', 'jump', 'next']);
  for (const block of Object.values(program.blocks)) {
    if (!Array.isArray(block.ops) || block.ops.some(op => !operations.has(op.op))
        || (block.next !== null && !own(program.blocks, block.next))) {
      throw error('体験版の命令形式が不正です。', 'invalid-scenario');
    }
  }
  const labels = new Set();
  const rules = Object.fromEntries(Object.entries(program.scenes).map(([name, scene]) => [name,
    scene.rules.map(rule => {
      const test = rule.test;
      if (!['exact', 'contains', 'regex', 'never'].includes(test?.kind)
          || (test.kind !== 'never' && typeof test.value !== 'string')) {
        throw error(`${sourceText(rule.source)}体験版の条件形式が不正です。`, 'invalid-scenario');
      }
      if (!own(program.blocks, rule.block)) throw error('体験版の参照先がありません。', 'invalid-scenario');
      if (test.kind === 'exact') labels.add(test.value);
      return { ...rule, regex: test.kind === 'regex' ? regex(test.value, test.ignore_case ? 'si' : 's', rule.source) : null };
    }),
  ]));
  const liffApps = Object.entries(program.liff_apps || {}).map(([id, app]) => ({ id, ...app }));

  const read = token => {
    let state;
    try { state = JSON.parse(token); }
    catch (_cause) { throw error('保存した進行を確認できません。「最初から」で再開してください。'); }
    if (state?.kind !== 'xstorybot-trial-v1' || state.bot !== program.bot) throw error('別の体験版の保存データです。');
    if (state.epoch !== program.epoch) throw error('シナリオが更新されました。「最初から」で再開してください。', 'incompatible-state');
    if ((state.scene !== null && !own(program.scenes, state.scene))
        || typeof state.generation !== 'string' || typeof state.conversation !== 'string'
        || !Number.isInteger(state.revision) || state.revision < 0) throw error('保存した進行の形式が不正です。');
    if (state.wait !== null && (!state.wait || typeof state.wait.label !== 'string'
        || !labels.has(`${state.wait.label}0`) || typeof state.wait.retry !== 'string'
        || (state.wait.guard && !labels.has(state.wait.retry))
        || typeof state.wait.guard !== 'boolean' || !Array.isArray(state.wait.choices)
        || !state.wait.choices.every(choice => Array.isArray(choice) && choice.length >= 2
          && choice.every(value => typeof value === 'string') && labels.has(choice.at(-1))))) {
      throw error('保存した選択待ちを再開できません。「最初から」で再開してください。', 'incompatible-state');
    }
    return state;
  };

  function execute(body) {
    const input = body?.input;
    if (!input || !['start', 'text', 'postback', 'menu', 'liff'].includes(input.type)) throw error('入力形式が不正です。', 'invalid-input');
    const starting = input.type === 'start';
    if (!starting && typeof body.state_token !== 'string') throw error('先に体験版を開始してください。');
    const state = starting ? {
      kind: 'xstorybot-trial-v1', bot: program.bot, epoch: program.epoch,
      conversation: identifier(), generation: identifier(), revision: -1, scene: null, wait: null,
    } : read(body.state_token);
    const originalGeneration = state.generation;
    const mode = input.type === 'liff' ? 'liff' : 'webchat';
    let action, echo = null;
    if (starting) action = program.start_action;
    else if (input.type === 'text') {
      if (typeof input.text !== 'string') throw error('入力は文字列にしてください。', 'invalid-input');
      action = sanitize(input.text); echo = input.text;
    } else if (input.type === 'postback') {
      let selected;
      try { selected = JSON.parse(input.postback_token); } catch (_cause) { throw error('選択肢の形式が不正です。', 'invalid-input'); }
      if (selected?.conversation !== state.conversation || selected?.generation !== state.generation
          || selected?.epoch !== program.epoch || typeof selected.action !== 'string'
          || !(selected.echo == null || typeof selected.echo === 'string')) {
        throw error('この選択肢は古くなりました。最新の会話を確認してください。', 'action-not-active');
      }
      action = selected.action; echo = selected.echo ?? null;
    } else if (input.type === 'menu') {
      const menu = program.richmenu;
      if (!menu || input.menu !== menu.id || input.revision !== menu.revision) {
        throw error('メニューが更新されました。画面を読み込み直してください。', 'action-not-active');
      }
      const selected = Number.isInteger(input.area) && input.area >= 0 ? program.menu_actions[input.area] : null;
      if (!selected || !['message', 'postback'].includes(selected.type)) throw error('メニューの領域が不正です。', 'invalid-input');
      action = selected.type === 'message' ? sanitize(selected.text) : selected.data;
      echo = selected.type === 'message' ? selected.text : selected.displayText ?? null;
    } else {
      const app = liffApps.find(candidate => candidate.id === input.app && candidate.url === input.app_url);
      if (!program.liff || !app || app.bot !== program.bot || typeof input.action !== 'string') {
        throw error('登録されたLIFFページではありません。', 'invalid-input');
      }
      action = program.liff.action_prefix + input.action;
    }

    let baseScene = state.scene || program.startup;
    if (state.scene === null) { state.scene = baseScene; state.generation = identifier(); }
    const messages = [], events = [];
    let count = 0, outputCount = 0, invalidLabel = false, handledError = false;
    const requestId = identifier();
    const output = () => {
      if (++outputCount > 100) throw error('一度の応答が多すぎるため進行を停止しました。', 'trial-execution-limit');
    };
    const reset = () => { state.scene = null; state.wait = null; state.generation = identifier(); };
    const resolve = value => {
      if (value.startsWith('*')) {
        const match = /^\*([^#]+)(#.*)?$/.exec(value);
        if (!match) throw error('シーン移動の形式が不正です。', 'invalid-scenario');
        const destination = match[1].includes('/') ? match[1] : `${program.scenes[baseScene].tab}/${match[1]}`;
        if (!own(program.scenes, destination)) { invalidLabel = true; return null; }
        baseScene = destination; state.scene = destination; state.generation = identifier();
        value = match[2] || program.scenes[destination].entry;
      }
      const normalized = value.normalize('NFKC').toLowerCase();
      for (const rule of rules[baseScene]) {
        const test = rule.test;
        let matched = test.kind === 'exact' && test.value === value;
        if (!special(value)) {
          if (test.kind === 'contains') matched = normalized.includes(test.value);
          if (test.kind === 'regex') {
            let target = test.normalize ? value.normalize('NFKC') : value;
            if (test.lower) target = target.toLowerCase();
            const match = rule.regex.exec(target);
            matched = !!match && (!test.exact || (match.index === 0 && match[0] === target));
          }
        }
        if (matched) return rule.block;
      }
      if (value.startsWith('#')) invalidLabel = true;
      return null;
    };
    const attachActions = value => {
      if (Array.isArray(value)) return value.map(attachActions);
      if (!value || typeof value !== 'object') return value;
      if (value.type === 'postback') {
        const { action: target, ...rest } = value;
        return { ...rest, token: JSON.stringify({ conversation: state.conversation,
          generation: state.generation, epoch: program.epoch, action: target, echo: value.echo_text }) };
      }
      return Object.fromEntries(Object.entries(value).map(([key, child]) => [key, attachActions(child)]));
    };
    const emit = message => {
      output(); messages.push({ ...message, id: `${requestId}:${messages.length}` });
    };

    if (program.reset_keyword && action === program.reset_keyword) {
      reset();
      if (mode === 'liff') { output(); events.push('リセットしました'); }
      else emit({ type: 'text', role: 'assistant', sender: null, text: 'リセットしました' });
      action = null;
    } else if (mode === 'webchat' && state.wait) {
      if (['##line.follow', '##line.join'].includes(action)) state.wait = null;
      else if (!action.startsWith(state.wait.label) && !(ignore && ignore.test(action))) {
        const choice = state.wait.choices.find(candidate => candidate[0] === action);
        if (choice) action = choice.at(-1);
        else if (state.wait.guard) action = state.wait.retry;
        else state.wait = null;
      }
    }

    let block = action === null ? null : resolve(action);
    const ignored = mode === 'liff' && program.liff.ignore_unhandled_action && block === null;
    while (action !== null && !ignored) {
      if (block === null) {
        if (handledError) break;
        handledError = true;
        block = resolve(invalidLabel ? '##error_invalid_label' : '##error_unhandled_action');
        if (block === null) break;
      }
      let next = null, jumped = false;
      for (const op of program.blocks[block].ops) {
        if (++count > 10000) throw error('進行が繰り返されているため停止しました。', 'trial-execution-limit');
        if (mode === 'liff' && (['reply', 'wait', 'clear_wait', 'show_choices'].includes(op.op)
            || (op.op === 'emit' && op.text === null))) {
          throw error(`${sourceText(op.source)}LIFFで会話用の表示命令は使えません。`, 'trial-unsupported-command');
        }
        if (op.op === 'emit') {
          if (mode === 'liff') { output(); events.push(op.text); }
          else for (const message of op.messages) emit(clone(message));
        } else if (op.op === 'reply') {
          if (!messages.length) emit(clone(op.fallback));
          messages.at(-1).quick_replies = clone(op.actions);
        } else if (op.op === 'wait') state.wait = clone(op.wait);
        else if (op.op === 'clear_wait') {
          if (state.wait && (!op.label || op.label === state.wait.label)) state.wait = null;
        } else if (op.op === 'reset') reset();
        else if (op.op === 'jump') { next = resolve(op.action); jumped = true; break; }
        else if (op.op === 'next') { next = program.blocks[block].next; jumped = next !== null; break; }
        else if (op.op === 'show_choices') {
          if (state.wait) { next = resolve(`${state.wait.label}0`); jumped = true; break; }
        } else throw error('体験版の命令が不正です。', 'invalid-scenario');
      }
      if (!jumped) break;
      block = next;
    }
    state.revision++;
    return {
      schema_version: 1, request_id: requestId,
      state: { id: identifier(), revision: state.revision }, state_token: JSON.stringify(state),
      messages: attachActions(messages), echo_message: echo, richmenu: program.richmenu, liff_apps: liffApps,
      chat_updated: mode === 'webchat' || state.generation !== originalGeneration,
      active_message_ids: mode === 'webchat' ? messages.map(message => message.id) : [],
      ...(mode === 'liff' ? { liff_result: events } : {}),
    };
  }

  return {
    execute,
    restore(head) {
      if (head) read(head.stateToken);
      return { richmenu: program.richmenu, liffApps };
    },
  };
}
