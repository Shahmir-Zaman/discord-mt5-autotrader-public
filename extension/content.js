/**
 * AutoTrader Discord Connector - Content Script
 * Runs in Discord web tabs (https://discord.com/*).
 * Observes real-time DOM mutations to capture new trading signals with near 0ms latency.
 * Supports concurrent broadcast to multiple AutoTrader ports/instances (e.g. 8000, 8001).
 */

(function () {
  'use strict';

  function normalizeUrl(str) {
    if (!str) return '';
    str = String(str).trim();
    // Auto-heal any legacy $1 corruption
    str = str.replace(/\$1/g, ':').replace(/::+/g, ':');
    if (/^\d{2,5}$/.test(str)) {
      return `http://127.0.0.1:${str}`;
    }
    // Replace localhost with 127.0.0.1 for Discord CSP connect-src compliance
    str = str.replace(/^(https?:\/\/)?localhost(?::|\/|$)/i, (match, proto) => {
      const p = proto || 'http://';
      return match.replace(/^https?:\/\/localhost/i, `${p}127.0.0.1`).replace(/^localhost/i, 'http://127.0.0.1');
    });
    if (!/^https?:\/\//i.test(str)) {
      str = 'http://' + str;
    }
    return str.replace(/\/+$/, '');
  }

  function parseServerUrls(input) {
    if (!input) return ['http://127.0.0.1:8000', 'http://127.0.0.1:8001'];
    let arr = [];
    if (Array.isArray(input)) {
      arr = input;
    } else if (typeof input === 'string') {
      arr = input.split(/[,;\s\n]+/).map((s) => s.trim()).filter(Boolean);
    }
    const clean = arr.map(normalizeUrl).filter(Boolean);
    return clean.length ? clean : ['http://127.0.0.1:8000', 'http://127.0.0.1:8001'];
  }

  let config = {
    serverUrls: ['http://127.0.0.1:8000', 'http://127.0.0.1:8001'],
    enabled: true
  };

  const sentMessageIds = new Set();
  let currentChannelId = '';
  let currentChannelName = '';
  let observer = null;
  let heartbeatTimer = null;
  let lastUrl = window.location.href;

  // Silent Web Audio keep-alive: Chrome explicitly exempts tabs playing audio from being discarded or throttled
  let keepAliveAudio = null;
  function startAudioKeepAlive() {
    try {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (!AudioCtx) return;
      if (keepAliveAudio && keepAliveAudio.state !== 'closed') return;

      const ctx = new AudioCtx();
      // Generate empty buffer of zeros (completely inaudible, 0 volume)
      const buffer = ctx.createBuffer(1, ctx.sampleRate * 2, ctx.sampleRate);
      const source = ctx.createBufferSource();
      source.buffer = buffer;
      source.loop = true;
      source.connect(ctx.destination);
      source.start();
      keepAliveAudio = ctx;
      console.log('[AutoTrader Extension] Inaudible background audio keep-alive active.');
    } catch (e) {
      // Handled silently
    }
  }

  // Auto-start or resume audio on user click
  window.addEventListener('click', () => {
    if (keepAliveAudio && keepAliveAudio.state === 'suspended') {
      keepAliveAudio.resume();
    } else if (!keepAliveAudio) {
      startAudioKeepAlive();
    }
  }, { passive: true });
  startAudioKeepAlive();

  // Load config from storage
  function loadConfig(cb) {
    if (chrome && chrome.storage && chrome.storage.local) {
      chrome.storage.local.get(['serverUrls', 'serverUrl', 'enabled'], (res) => {
        if (res.serverUrls) {
          config.serverUrls = parseServerUrls(res.serverUrls);
        } else if (res.serverUrl) {
          config.serverUrls = parseServerUrls(res.serverUrl);
        }
        if (typeof res.enabled === 'boolean') config.enabled = res.enabled;
        if (cb) cb();
      });
    } else if (cb) {
      cb();
    }
  }

  // Listen for config changes from popup
  if (chrome && chrome.storage && chrome.storage.onChanged) {
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area === 'local') {
        if (changes.serverUrls) config.serverUrls = parseServerUrls(changes.serverUrls.newValue);
        else if (changes.serverUrl) config.serverUrls = parseServerUrls(changes.serverUrl.newValue);
        if (changes.enabled) config.enabled = changes.enabled.newValue;
      }
    });
  }

  function cleanChannelName(raw) {
    if (!raw) return '';
    return raw
      .replace(/^[\s#\u{1F300}-\u{1F9FF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}┃|•—\-_/:]+/u, '')
      .replace(/[\s\u{1F300}-\u{1F9FF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}┃|•—\-_/:]+$/u, '')
      .trim()
      .toLowerCase();
  }

  function getChannelInfo() {
    // 1. Channel ID from URL: https://discord.com/channels/{guildId}/{channelId}
    const pathParts = window.location.pathname.split('/');
    const channelId = pathParts.length >= 4 ? pathParts[3] : '';

    let channelName = '';

    // 2. Prioritize document.title if it contains a channel indicator '#'
    // Format is typically: "#🏡┃channel-a | inspiredanalyst's server" or "(1) #channel | Server"
    if (document.title && document.title.includes('#')) {
      const titleMatch = document.title.match(/#\s*([^|•]+)/);
      if (titleMatch && titleMatch[1]) {
        const cleaned = cleanChannelName(titleMatch[1]);
        if (cleaned && !cleaned.includes('discord')) {
          channelName = cleaned;
        }
      }
    }

    // 3. If title didn't match '#', check main chat area header elements
    if (!channelName) {
      const mainChatHeader = document.querySelector(
        'section[aria-label*="Channel header"] h1, ' +
        'section[aria-label*="Channel header"] h2, ' +
        'section[aria-label*="Channel header"] [class*="title-"], ' +
        'main [class*="titleWrapper-"] h1, ' +
        'main [class*="title-"], ' +
        '[class*="chat-"] [class*="title-"], ' +
        '[class*="channelName-"]'
      );
      if (mainChatHeader && mainChatHeader.innerText) {
        const cleaned = cleanChannelName(mainChatHeader.innerText);
        if (cleaned && !cleaned.includes('server')) {
          channelName = cleaned;
        }
      }
    }

    // 4. Fallback to active channel in sidebar
    if (!channelName) {
      const activeChannelEl = document.querySelector('[class*="selected-"] [class*="name-"], [aria-selected="true"] [class*="name-"]');
      if (activeChannelEl && activeChannelEl.innerText) {
        channelName = cleanChannelName(activeChannelEl.innerText);
      }
    }

    if (!channelName && channelId) {
      channelName = `channel-${channelId}`;
    }

    return { channelId, channelName };
  }

  function extractMessageDetails(msgEl) {
    if (!msgEl || !msgEl.id || !msgEl.id.startsWith('chat-messages-')) {
      return null;
    }

    const msgId = msgEl.id;
    if (sentMessageIds.has(msgId)) {
      return null;
    }

    // Extract text content
    const contentEl = msgEl.querySelector('[id^="message-content-"], [class*="messageContent"]');
    const text = contentEl && contentEl.innerText ? contentEl.innerText.trim() : (msgEl.innerText ? msgEl.innerText.trim() : '');

    if (!text) {
      return null;
    }

    // Extract author
    const authorEl = msgEl.querySelector('[id^="message-username-"], [class*="username-"]');
    const author = authorEl ? authorEl.innerText.trim() : '';

    // Compute Discord Snowflake timestamp (ms since epoch)
    let msgTime = Date.now();
    try {
      const parts = msgId.split('-');
      const snowflake = BigInt(parts[parts.length - 1]);
      msgTime = Number((snowflake >> 22n) + 1420070400000n);
    } catch (e) {
      msgTime = Date.now();
    }

    return {
      message_id: msgId,
      text: text,
      author: author,
      timestamp: msgTime
    };
  }

  async function postSignal(msgData) {
    if (!config.enabled) return;

    const { channelId, channelName } = getChannelInfo();
    const payload = {
      channel: channelName,
      channel_id: channelId,
      message_id: msgData.message_id,
      text: msgData.text,
      author: msgData.author,
      timestamp: msgData.timestamp
    };

    sentMessageIds.add(msgData.message_id);
    if (sentMessageIds.size > 200) {
      const firstKey = sentMessageIds.values().next().value;
      sentMessageIds.delete(firstKey);
    }

    const targetUrls = config.serverUrls && config.serverUrls.length ? config.serverUrls : ['http://127.0.0.1:8000'];
    console.log(`[AutoTrader Extension] Broadcasting signal from #${channelName} to ${targetUrls.length} instance(s) (${targetUrls.join(', ')}):`, msgData.text);

    // 1. Relay through background service worker (bypasses webpage CSP)
    try {
      if (chrome && chrome.runtime && chrome.runtime.sendMessage) {
        chrome.runtime.sendMessage({ type: 'RELAY_SIGNAL', payload });
      }
    } catch (e) {}

    // 2. Direct fetch fallback (using 127.0.0.1 which is permitted in Discord CSP)
    const postPromises = targetUrls.map(async (url) => {
      try {
        const res = await fetch(`${url}/api/extension/message`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        return { url, ok: res.ok, data };
      } catch (err) {
        return { url, ok: false, error: err.message };
      }
    });

    const results = await Promise.allSettled(postPromises);

    // Save to recent message log in storage
    if (chrome && chrome.storage && chrome.storage.local) {
      chrome.storage.local.get(['recentLogs'], (storage) => {
        const logs = storage.recentLogs || [];
        const portStatuses = results.map((r) => {
          if (r.status === 'fulfilled') {
            const val = r.value;
            const port = val.url.split(':').pop().replace(/\/.*$/, '');
            return val.ok ? `${port}: ${val.data?.status || 'ok'}` : `${port}: offline`;
          }
          return 'error';
        }).join(' | ');

        logs.unshift({
          time: new Date().toLocaleTimeString(),
          channel: channelName,
          text: msgData.text.slice(0, 120),
          status: portStatuses,
          ports: targetUrls.map((u) => u.split(':').pop().replace(/\/.*$/, '')).join(', ')
        });
        chrome.storage.local.set({ recentLogs: logs.slice(0, 25) });
      });
    }
  }

  function processMessageElement(el, isInitialScan = false) {
    const details = extractMessageDetails(el);
    if (!details) return;

    // During initial page load or channel navigation, ignore messages older than 2.5 minutes
    const ageMs = Date.now() - details.timestamp;
    if (isInitialScan && ageMs > 150000) {
      sentMessageIds.add(details.message_id);
      return;
    }

    postSignal(details);
  }

  function scanExistingMessages() {
    const messageElements = document.querySelectorAll('li[id^="chat-messages-"]');
    messageElements.forEach((el) => processMessageElement(el, true));
  }

  function setupMutationObserver() {
    if (observer) {
      observer.disconnect();
      observer = null;
    }

    // Dismiss jump to present bar if active
    try {
      const jumpBtn = document.querySelector('[class*="jumpToPresentBar"] button, [class*="jumpToPresentBar"]');
      if (jumpBtn) jumpBtn.click();
    } catch (e) {}

    scanExistingMessages();

    // Target the main chat scroller or fall back to document.body
    const targetNode = document.querySelector('[data-list-id="chat-messages"]') ||
      document.querySelector('[class*="messagesWrapper"]') ||
      document.body;

    if (!targetNode) return;

    observer = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        if (mutation.type === 'childList') {
          for (const node of mutation.addedNodes) {
            if (node.nodeType === Node.ELEMENT_NODE) {
              if (node.tagName === 'LI' && node.id && node.id.startsWith('chat-messages-')) {
                processMessageElement(node, false);
              } else {
                const nested = node.querySelectorAll ? node.querySelectorAll('li[id^="chat-messages-"]') : [];
                nested.forEach((m) => processMessageElement(m, false));
              }
            }
          }
        }
      }
    });

    observer.observe(targetNode, { childList: true, subtree: true });
    console.log('[AutoTrader Extension] Observer attached to Discord chat DOM.');
  }

  let consecutiveServerFailures = 0;
  let lastServerAlertTime = 0;
  let serverWasOffline = false;
  let cachedTgToken = '';
  let cachedTgChatId = '';

  async function sendTelegramDirectAlert(token, chatId, text) {
    if (!token || !chatId || !text) return;
    try {
      await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat_id: chatId, text: text })
      });
    } catch (e) {
      console.warn('[AutoTrader Extension] Direct Telegram alert failed:', e);
    }
  }

  async function sendHeartbeat() {
    if (!config.enabled) return;

    const { channelId, channelName } = getChannelInfo();
    const targetUrls = config.serverUrls && config.serverUrls.length ? config.serverUrls : ['http://127.0.0.1:8000'];
    const connectionStatus = {};

    await Promise.allSettled(targetUrls.map(async (url) => {
      try {
        const res = await fetch(`${url}/api/extension/heartbeat`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            channel: channelName,
            channel_id: channelId,
            url: window.location.href
          })
        });
        connectionStatus[url] = res.ok;
        if (res.ok) {
          try {
            const data = await res.json();
            if (data.tg_token) cachedTgToken = data.tg_token;
            if (data.tg_chat_id) cachedTgChatId = data.tg_chat_id;
          } catch (_) {}
        }
      } catch (err) {
        connectionStatus[url] = false;
      }
    }));

    const anyConnected = Object.values(connectionStatus).some(Boolean);
    const now = Date.now();

    if (anyConnected) {
      consecutiveServerFailures = 0;
      if (serverWasOffline && cachedTgToken && cachedTgChatId) {
        serverWasOffline = false;
        sendTelegramDirectAlert(
          cachedTgToken, cachedTgChatId,
          `✅ [AutoTrader Alert]\nAutoTrader server connection restored!\nDiscord extension is back online and monitoring #${channelName}.`
        );
      }
    } else {
      consecutiveServerFailures++;
      // Alert if disconnected for >= 5 consecutive heartbeats (30 seconds)
      if (consecutiveServerFailures >= 5 && (!serverWasOffline || (now - lastServerAlertTime) > 300000) && cachedTgToken && cachedTgChatId) {
        serverWasOffline = true;
        lastServerAlertTime = now;
        sendTelegramDirectAlert(
          cachedTgToken, cachedTgChatId,
          `🚨 [AutoTrader Alert]\nAutoTrader server (${targetUrls.join(', ')}) is UNREACHABLE or DOWN!\nDiscord extension cannot connect. Please check if your Python bot crashed or terminal closed.`
        );
      }
    }

    if (chrome && chrome.storage && chrome.storage.local) {
      chrome.storage.local.set({
        connected: anyConnected,
        connectionStatus: connectionStatus,
        serverUrls: targetUrls,
        currentChannel: channelName,
        currentChannelId: channelId,
        discordActive: true,
        lastHeartbeat: Date.now()
      });
    }
  }

  // URL Watcher for Discord Single-Page App navigation
  function checkUrlChange() {
    if (window.location.href !== lastUrl) {
      lastUrl = window.location.href;
      const { channelId, channelName } = getChannelInfo();
      currentChannelId = channelId;
      currentChannelName = channelName;
      console.log(`[AutoTrader Extension] Channel switched to: #${channelName} (${channelId})`);
      setTimeout(setupMutationObserver, 500);
      sendHeartbeat();
    }
  }

  // Initialization
  loadConfig(() => {
    const info = getChannelInfo();
    currentChannelId = info.channelId;
    currentChannelName = info.channelName;

    // Retry finding chat messages until Discord renders
    let attempts = 0;
    const initInterval = setInterval(() => {
      attempts++;
      const chat = document.querySelector('[data-list-id="chat-messages"], [class*="messagesWrapper"]');
      if (chat || attempts > 20) {
        clearInterval(initInterval);
        setupMutationObserver();
      }
    }, 500);

    // Heartbeat every 6s
    sendHeartbeat();
    if (heartbeatTimer) clearInterval(heartbeatTimer);
    heartbeatTimer = setInterval(sendHeartbeat, 6000);

    // URL change detection every 1s
    setInterval(checkUrlChange, 1000);

    console.log(`[AutoTrader Extension] Initialized Discord Signal Relay targeting: ${config.serverUrls.join(', ')}`);
  });
})();
