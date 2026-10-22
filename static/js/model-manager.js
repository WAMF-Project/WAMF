/* Classification stays within Settings. Install and selection never submit it. */
(function () {
  'use strict';
  const manager = document.getElementById('model-manager');
  if (!manager) return;
  const message = document.getElementById('model-message');
  const progress = document.getElementById('model-progress');
  const custom = document.getElementById('classification_custom_enabled');
  const path = document.getElementById('classification_model');
  const cards = Array.from(manager.querySelectorAll('.model-card'));
  const phaseLabels = {preparing: 'Preparing', downloading: 'Downloading', verifying: 'Verifying',
    validating: 'Validating', installing: 'Installing'};
  let state = JSON.parse(document.getElementById('model-manager-initial').textContent);
  let pending = false;
  let pollTimer = null;
  let requestOwned = false;
  let pageActive = true;
  let statusUnavailable = false;

  custom.addEventListener('change', function () { path.disabled = !custom.checked; });

  function announce(text, error) {
    message.setAttribute('role', error ? 'alert' : 'status');
    message.textContent = text;
  }

  async function jsonRequest(url, options) {
    const response = await fetch(url, Object.assign({cache: 'no-store', headers: {
      'Accept': 'application/json', 'X-CSRFToken': document.querySelector('#settings-form [name="csrf_token"]').value
    }}, options || {}));
    if (response.redirected || response.status === 401 || response.status === 403) {
      throw new Error('Admin login required. Refresh the page and sign in again.');
    }
    if (!(response.headers.get('Content-Type') || '').includes('application/json')) {
      throw new Error('Admin login may have expired. Refresh the page and sign in again.');
    }
    const data = await response.json();
    if (response.status === 400 && !data.code) {
      throw new Error('Security token expired. Refresh the page and try again.');
    }
    if (!response.ok && !data.code) throw new Error('The model request could not be completed. Refresh and try again.');
    return data;
  }

  function render() {
    const focused = document.activeElement;
    document.getElementById('model-running').textContent = state.running_label;
    document.getElementById('model-selected').textContent = state.selected_label;
    document.getElementById('model-restart-required').hidden = !state.restart_required;
    const error = document.getElementById('model-selected-error');
    error.hidden = !state.selected_error;
    error.textContent = state.selected_error || '';
    cards.forEach(function (card) {
      const item = state.models.find(model => model.model_id === card.dataset.modelId && model.version === card.dataset.modelVersion);
      if (!item) return;
      const badges = [];
      if (item.bundled) badges.push('Bundled');
      badges.push(item.valid ? 'Installed' : item.installed || item.issue ? 'Invalid / unavailable' : 'Available to install');
      if (item.active) badges.push('Active');
      // Older running web workers still provide the authoritative active flag.
      const selectedForNextStart = item.selected_for_next_start ?? (item.selected && !item.active);
      if (selectedForNextStart) badges.push('Selected for next start');
      if (item.can_select) badges.push('Ready to select');
      card.querySelector('.model-badges').textContent = badges.join(' · ');
      const issue = card.querySelector('.model-issue');
      issue.hidden = !item.issue;
      issue.textContent = item.issue || '';
      const button = card.querySelector('.model-action');
      button.hidden = !(item.can_install || item.can_select);
      button.dataset.action = item.can_install ? 'install' : 'select';
      button.textContent = item.can_install ? 'Install model' : 'Use after restart';
      button.disabled = pending;
      if (focused === button && button.hidden) {
        card.tabIndex = -1;
        card.focus({preventScroll: true});
      }
    });
  }

  async function refresh() {
    state = await jsonRequest(manager.dataset.statusUrl);
    render();
  }

  function schedulePoll() {
    window.clearTimeout(pollTimer);
    if (pageActive) pollTimer = window.setTimeout(poll, 1000);
  }

  async function poll() {
    try {
      const data = await jsonRequest(manager.dataset.installStatusUrl);
      const operation = data.operation;
      if (operation && !operation.terminal_result) {
        pending = true;
        render();
        progress.hidden = false;
        const percent = operation.percent;
        if (typeof percent === 'number') progress.value = percent;
        else progress.removeAttribute('value');
        announce((phaseLabels[operation.phase] || 'Preparing') + ' model' +
          (typeof percent === 'number' ? ' · ' + percent + '%' : '') + '…', false);
        schedulePoll();
      } else if (!requestOwned) {
        const wasPending = pending;
        pending = false;
        progress.hidden = true;
        if (operation && operation.terminal_result) {
          announce((operation.phase === 'failed' ? 'Install failed: ' : '') + operation.terminal_result.message,
            operation.phase === 'failed');
        } else if (statusUnavailable || wasPending) {
          announce('No model installation is currently reported. Local model state has been refreshed.', false);
        }
        await refresh();
      } else schedulePoll();
      statusUnavailable = false;
    } catch (error) {
      // No endless spinner after a lost server/session; rediscovery on reload
      // is authoritative. Do not claim that a vanished request succeeded.
      statusUnavailable = true;
      progress.hidden = true;
      announce(error.message || 'Installation status is unavailable. Refresh to check local models.', true);
      if (!requestOwned) { pending = false; render(); }
    }
  }

  cards.forEach(function (card) {
    card.querySelector('.model-action').addEventListener('click', async function () {
      if (pending) return;
      const button = this;
      const action = button.dataset.action;
      let followOtherInstall = false;
      const startedFocused = document.activeElement === button;
      let focusMoved = false;
      function trackFocus(event) {
        if (event.target !== button && event.target !== document.body) focusMoved = true;
      }
      document.addEventListener('focusin', trackFocus);
      pending = true;
      requestOwned = true;
      render();
      announce(action === 'install' ? 'Preparing model installation…' : 'Saving model selection…', false);
      if (action === 'install') { progress.hidden = false; progress.removeAttribute('value'); schedulePoll(); }
      try {
        const result = await jsonRequest(action === 'install' ? button.dataset.installUrl : button.dataset.selectUrl, {method: 'POST'});
        announce((!result.success && action === 'install' ? 'Install failed: ' : '') + result.message, !result.success);
        if (result.manager) {
          state = result.manager;
          // Only this selection field changes; unsaved threshold and other
          // Settings values remain in the page until the normal Save action.
          custom.checked = false;
          path.disabled = true;
          path.value = '';
          document.getElementById('classification_model_original').value = '';
        } else await refresh();
        followOtherInstall = result.code === 'install_in_progress';
      } catch (error) {
        announce(error.message || 'The response was not received. Refresh to check the saved state.', true);
      } finally {
        requestOwned = false;
        pending = followOtherInstall;
        progress.hidden = true;
        window.clearTimeout(pollTimer);
        render();
        document.removeEventListener('focusin', trackFocus);
        if (startedFocused && !focusMoved &&
            (document.activeElement === document.body || document.activeElement === button)) {
          if (button.hidden) { card.tabIndex = -1; card.focus({preventScroll: true}); }
          else if (!pending) button.focus({preventScroll: true});
        }
        if (followOtherInstall) schedulePoll();
      }
    });
  });
  window.addEventListener('pagehide', function () { pageActive = false; window.clearTimeout(pollTimer); });
  render();
  // Restore observation of an operation started by another tab/request.
  poll();
}());
