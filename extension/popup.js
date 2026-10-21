document.addEventListener('DOMContentLoaded', () => {
  const serverUrlsInput = document.getElementById('serverUrls');
  const enabledToggle = document.getElementById('enabledToggle');
  const saveBtn = document.getElementById('saveBtn');
  const statusBadge = document.getElementById('statusBadge');
  const statusDot = document.getElementById('statusDot');
  const statusText = document.getElementById('statusText');
  const portStatusList = document.getElementById('portStatusList');
  const activeChannel = document.getElementById('activeChannel');
  const discordNotice = document.getElementById('discordNotice');
  const reloadDiscordBtn = document.getElementById('reloadDiscordBtn');
  const logsContainer = document.getElementById('logsContainer');

  let activeDiscordTabId = null;

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

  function formatUrlsForInput(urls) {
    if (!urls || !urls.length) return '8000, 8001';
    return urls.map((u) => {
      const match = u.match(/^https?:\/\/(?:127\.0\.0\.1|localhost):(\d+)$/i);
      return match ? match[1] : u;
    }).join(', ');
  }

  // Active ping to all target URLs directly from popup
  async function pingTargets(urls) {
    const statusMap = {};
    await Promise.allSettled(urls.map(async (u) => {
      try {
        const res = await fetch(`${u}/api/extension/status`, { method: 'GET' });
        statusMap[u] = res.ok;
      } catch (e) {
        statusMap[u] = false;
      }
    }));
    return statusMap;
  }

  // Check Discord tab status directly via chrome.tabs
  function checkDiscordTab(callback) {
    if (chrome && chrome.tabs && chrome.tabs.query) {
      chrome.tabs.query({ url: ["https://discord.com/*", "https://*.discord.com/*"] }, (tabs) => {
        if (tabs && tabs.length > 0) {
          activeDiscordTabId = tabs[0].id;
          callback(tabs[0]);
        } else {
          activeDiscordTabId = null;
          callback(null);
        }
      });
    } else {
      callback(null);
    }
  }

  async function updateUI() {
    chrome.storage.local.get(
      ['serverUrls', 'serverUrl', 'enabled', 'connected', 'connectionStatus', 'currentChannel', 'recentLogs'],
      async (data) => {
        const rawUrls = data.serverUrls || (data.serverUrl ? [data.serverUrl] : ['http://127.0.0.1:8000', 'http://127.0.0.1:8001']);
        const urls = parseServerUrls(rawUrls);
        if (JSON.stringify(rawUrls) !== JSON.stringify(urls)) {
          chrome.storage.local.set({ serverUrls: urls, serverUrl: urls[0] || 'http://127.0.0.1:8000' });
        }

        if (document.activeElement !== serverUrlsInput) {
          serverUrlsInput.value = formatUrlsForInput(urls);
        }
        if (typeof data.enabled === 'boolean') enabledToggle.checked = data.enabled;

        // Perform active direct ping to servers for immediate live feedback
        const liveStatus = await pingTargets(urls);
        const connMap = Object.keys(liveStatus).length ? liveStatus : (data.connectionStatus || {});

        let onlineCount = 0;
        urls.forEach((u) => {
          if (connMap[u]) onlineCount++;
        });

        if (urls.length === 0 || data.enabled === false) {
          statusBadge.className = 'status-badge status-disconnected';
          statusDot.className = 'dot dot-red';
          statusText.textContent = data.enabled === false ? 'Disabled' : 'Disconnected';
        } else if (onlineCount === urls.length) {
          statusBadge.className = 'status-badge status-connected';
          statusDot.className = 'dot dot-green';
          statusText.textContent = 'Connected';
        } else if (onlineCount > 0) {
          statusBadge.className = 'status-badge status-partial';
          statusDot.className = 'dot dot-amber';
          statusText.textContent = `Partial (${onlineCount}/${urls.length})`;
        } else {
          statusBadge.className = 'status-badge status-disconnected';
          statusDot.className = 'dot dot-red';
          statusText.textContent = 'Disconnected';
        }

        // Render per-port pills
        if (portStatusList) {
          portStatusList.innerHTML = urls.map((u) => {
            const isOnline = !!connMap[u];
            const portNum = u.replace(/^https?:\/\/(?:127\.0\.0\.1|localhost):/, 'Port ');
            const dotClass = isOnline ? 'dot-green' : 'dot-red';
            const pillClass = isOnline ? 'online' : 'offline';
            const text = isOnline ? 'Online' : 'Offline';
            return `<span class="port-pill ${pillClass}"><span class="dot ${dotClass}"></span>${portNum}: ${text}</span>`;
          }).join('');
        }

        // Update Discord tab display
        checkDiscordTab((discordTab) => {
          if (data.currentChannel) {
            activeChannel.textContent = `#${data.currentChannel}`;
            activeChannel.style.color = '#34d399';
            if (discordNotice) discordNotice.style.display = 'none';
          } else if (discordTab) {
            // Discord tab is open in Chrome but content script has not connected yet (needs F5 reload)
            activeChannel.textContent = 'Open (Needs Refresh)';
            activeChannel.style.color = '#fbbf24';
            if (discordNotice) discordNotice.style.display = 'block';
          } else {
            activeChannel.textContent = 'No Discord Tab';
            activeChannel.style.color = '#f87171';
            if (discordNotice) discordNotice.style.display = 'none';
          }
        });

        const logs = data.recentLogs || [];
        if (logs.length === 0) {
          logsContainer.innerHTML = '<div style="color: #64748b; font-style: italic; padding: 8px 0; text-align: center;">No signals captured yet</div>';
        } else {
          logsContainer.innerHTML = logs.map((log) => {
            let badgeClass = 'status-unknown';
            const st = String(log.status || '').toLowerCase();
            if (st.includes('executed') || st.includes('ok')) badgeClass = 'status-executed';
            else if (st.includes('filtered') || st.includes('ignore')) badgeClass = 'status-filtered';
            else if (st.includes('offline') || st.includes('error')) badgeClass = 'status-unknown';

            return `
              <div class="log-item">
                <div class="log-header">
                  <span>[${log.time}] #${log.channel}</span>
                  <span class="log-status ${badgeClass}" title="${log.status}">${log.status}</span>
                </div>
                <div class="log-text" title="${log.text}">${log.text}</div>
              </div>
            `;
          }).join('');
        }
      }
    );
  }

  saveBtn.addEventListener('click', async () => {
    const rawInput = serverUrlsInput.value;
    const parsed = parseServerUrls(rawInput);
    const enabled = enabledToggle.checked;

    saveBtn.textContent = 'Checking...';
    const liveStatus = await pingTargets(parsed);
    const anyConnected = Object.values(liveStatus).some(Boolean);

    chrome.storage.local.set({
      serverUrls: parsed,
      serverUrl: parsed[0] || 'http://127.0.0.1:8000',
      connectionStatus: liveStatus,
      connected: anyConnected,
      enabled: enabled
    }, () => {
      saveBtn.textContent = 'Saved & Connected!';
      setTimeout(() => { saveBtn.textContent = 'Save & Reconnect'; }, 1200);
      updateUI();
    });
  });

  if (reloadDiscordBtn) {
    reloadDiscordBtn.addEventListener('click', () => {
      if (activeDiscordTabId && chrome && chrome.tabs && chrome.tabs.reload) {
        reloadDiscordBtn.textContent = 'Refreshing...';
        chrome.tabs.reload(activeDiscordTabId, () => {
          setTimeout(() => {
            reloadDiscordBtn.textContent = '🔄 Refresh Discord Tab';
            updateUI();
          }, 1500);
        });
      }
    });
  }

  updateUI();
  setInterval(updateUI, 2500);
});
