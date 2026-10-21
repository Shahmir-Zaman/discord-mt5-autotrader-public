/**
 * AutoTrader Discord Connector - Background Service Worker
 * Runs in the Chrome extension process.
 * Bypasses all webpage CSP restrictions to reliably broadcast to local AutoTrader instances.
 */

function normalizeUrl(str) {
  if (!str) return '';
  str = String(str).trim();
  // Auto-heal any legacy $1 corruption
  str = str.replace(/\$1/g, ':').replace(/::+/g, ':');
  if (/^\d{2,5}$/.test(str)) {
    return `http://127.0.0.1:${str}`;
  }
  // Convert localhost to 127.0.0.1 for Discord CSP compliance
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

// Ping all target ports directly from the background service worker
async function pingTargets(urls) {
  const statusMap = {};
  await Promise.allSettled(urls.map(async (url) => {
    try {
      const res = await fetch(`${url}/api/extension/status`, { method: 'GET' });
      statusMap[url] = res.ok;
    } catch (e) {
      statusMap[url] = false;
    }
  }));
  return statusMap;
}

// Broadcast message/signal to all configured instances concurrently
async function broadcastSignal(payload, targetUrls) {
  const promises = targetUrls.map(async (url) => {
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
  return Promise.allSettled(promises);
}

// Check Discord tab status
async function checkDiscordTabs() {
  if (!chrome.tabs) return null;
  return new Promise((resolve) => {
    chrome.tabs.query({ url: ["https://discord.com/*", "https://*.discord.com/*"] }, (tabs) => {
      if (tabs && tabs.length > 0) {
        resolve(tabs[0]);
      } else {
        resolve(null);
      }
    });
  });
}

// Periodic check
async function runHealthCheck() {
  chrome.storage.local.get(['serverUrls', 'enabled'], async (data) => {
    if (data.enabled === false) return;
    const urls = parseServerUrls(data.serverUrls);
    const connMap = await pingTargets(urls);
    const anyConnected = Object.values(connMap).some(Boolean);
    const discordTab = await checkDiscordTabs();

    chrome.storage.local.set({
      connectionStatus: connMap,
      connected: anyConnected,
      serverUrls: urls,
      discordTabFound: !!discordTab,
      discordTabTitle: discordTab ? (discordTab.title || '') : '',
      lastHealthCheck: Date.now()
    });
  });
}

// Listen for messages from content script or popup
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'PING_SERVERS') {
    const urls = parseServerUrls(msg.urls);
    pingTargets(urls).then((res) => {
      const anyConnected = Object.values(res).some(Boolean);
      chrome.storage.local.set({
        connectionStatus: res,
        connected: anyConnected,
        serverUrls: urls
      });
      sendResponse({ statusMap: res, connected: anyConnected });
    });
    return true; // asynchronous response
  }

  if (msg.type === 'RELAY_SIGNAL') {
    chrome.storage.local.get(['serverUrls'], async (data) => {
      const urls = parseServerUrls(data.serverUrls);
      const results = await broadcastSignal(msg.payload, urls);
      sendResponse({ results });
    });
    return true;
  }

  if (msg.type === 'RELOAD_DISCORD_TAB') {
    checkDiscordTabs().then((tab) => {
      if (tab && tab.id) {
        chrome.tabs.reload(tab.id, () => sendResponse({ success: true, tabId: tab.id }));
      } else {
        sendResponse({ success: false });
      }
    });
    return true;
  }
});

// Run health check every 4 seconds
setInterval(runHealthCheck, 4000);
runHealthCheck();
