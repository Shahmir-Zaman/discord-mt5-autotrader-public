/**
 * AutoTrader Discord Connector - Keep-Alive Script (MAIN World)
 * Injected at document_start in the Discord tab's execution context.
 *
 * Prevents Discord from detecting that the tab is in the background:
 * 1. Spoofs document.hidden = false and document.visibilityState = 'visible'
 * 2. Intercepts visibilitychange and blur events
 * 3. Keeps Discord's WebSocket and React message renderer active 24/7 even when backgrounded/minimized
 */

(function () {
  'use strict';

  try {
    // 1. Override document.hidden and document.visibilityState
    Object.defineProperty(document, 'hidden', {
      get: () => false,
      configurable: true
    });

    Object.defineProperty(document, 'visibilityState', {
      get: () => 'visible',
      configurable: true
    });

    Object.defineProperty(document, 'webkitVisibilityState', {
      get: () => 'visible',
      configurable: true
    });

    // 2. Stop visibilitychange events from reaching Discord's idle detectors
    window.addEventListener('visibilitychange', (e) => {
      e.stopImmediatePropagation();
    }, true);

    window.addEventListener('webkitvisibilitychange', (e) => {
      e.stopImmediatePropagation();
    }, true);

    // 3. Prevent Discord from throttling on window blur
    window.addEventListener('blur', (e) => {
      e.stopImmediatePropagation();
    }, true);

    console.log('[AutoTrader Extension] Tab Visibility Spoofing Active (Discord will never enter idle/background sleep).');
  } catch (err) {
    console.warn('[AutoTrader Extension] Visibility override error:', err);
  }
})();
