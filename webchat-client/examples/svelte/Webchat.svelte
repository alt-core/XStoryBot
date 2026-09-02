<script lang="ts">
  import { onMount } from 'svelte';
  import { createWebchatClient } from '../../index.js';

  export let apiBaseUrl: string;
  export let bot: string;

  const chat = createWebchatClient({ apiBaseUrl, bot });
  let draft = '';

  onMount(() => {
    chat.initialize().then(() => chat.start()).catch(() => {});
    return () => chat.destroy();
  });

  async function send() {
    const text = draft;
    if (!text.trim() || chat.getSnapshot().status === 'sending') return;
    draft = '';
    try {
      await chat.sendText(text);
    } catch (_error) {
      if (!draft) draft = text;
    }
  }
</script>

{#if $chat.notice}<p>{$chat.notice}</p>{/if}
{#if $chat.error}<p role="alert">{$chat.error.message}</p>{/if}
{#each $chat.turns as turn (turn.id)}
  {#if turn.echoMessage}<p class="user">{turn.echoMessage}</p>{/if}
  {#each turn.messages as message (message.id)}
    {#if message.type === 'text'}<p>{message.text}</p>{/if}
  {/each}
{/each}

<form on:submit|preventDefault={send}>
  <input bind:value={draft} />
  <button disabled={$chat.status === 'sending' || !draft.trim()}>送信</button>
</form>
