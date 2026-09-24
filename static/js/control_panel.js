// Control panel: operator commands. Buttons are enabled from the live state as
// a convenience only; the machine re-checks every request and its answer is
// what gets shown.
document.addEventListener('DOMContentLoaded', function () {
  const C = window.CORDS;
  const $ = id => document.getElementById(id);

  // which phases each command makes sense in (mirrors hardware/batch.py)
  const ALLOWED = {
    start: ['idle'],
    set_target_kg: ['idle', 'intake'],
    end_drying: ['drying'],
    finish_sorting: ['sorting'],
    reset: ['complete', 'aborted'],
    abort: ['intake', 'drying', 'cooldown', 'sorting']
  };

  let live = null;
  let busy = false;

  function refreshButtons() {
    const running = live && live.supervisor === 'running';
    document.querySelectorAll('.cp-btn').forEach(btn => {
      const allowed = ALLOWED[btn.dataset.command] || [];
      btn.disabled = busy || !running || !allowed.includes(live.state);
    });
  }

  function feedback(message, kind) {
    const el = $('cmdFeedback');
    el.textContent = message;
    el.dataset.kind = kind;
  }

  async function run(name, payload) {
    busy = true;
    refreshButtons();
    feedback('Sending…', 'pending');
    const result = await C.sendCommand(name, payload);
    feedback(result.message, result.ok ? 'ok' : 'refused');
    busy = false;
    refreshButtons();
    return result;
  }

  C.onLive(function (state) {
    live = state;
    $('stateBadge').textContent = state.supervisor === 'running'
      ? (C.STATE_LABELS[state.state] || '—')
      : 'Offline';
    $('stateBadge').dataset.state = state.supervisor === 'running' ? state.state : 'offline';
    // don't overwrite what the operator is typing
    const input = $('targetInput');
    if (document.activeElement !== input && state.target_kg != null) {
      input.value = state.target_kg;
    }
    refreshButtons();
  });

  document.querySelectorAll('.cp-btn[data-command]').forEach(btn => {
    if (btn.dataset.command === 'set_target_kg') return;   // handled by the form
    btn.addEventListener('click', async () => {
      if (btn.dataset.confirm && !window.confirm(btn.dataset.confirm)) return;
      const name = btn.dataset.command;
      const payload = name === 'abort' ? $('abortReason').value : null;
      const result = await run(name, payload);
      if (name === 'abort' && result.ok) $('abortReason').value = '';
    });
  });

  $('targetForm').addEventListener('submit', async event => {
    event.preventDefault();
    await run('set_target_kg', $('targetInput').value);
    $('targetInput').blur();
  });
});
