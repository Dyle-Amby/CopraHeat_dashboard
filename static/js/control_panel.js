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
    skip_intake: ['intake'],
    end_drying: ['drying'],
    override_grade: ['sorting'],
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
      let disabled = busy || !running || !allowed.includes(live.state);
      if (btn.dataset.command === 'start' && live && live.calibration_locked) {
        disabled = true;
        btn.title = 'Calibration in progress (locked)';
      } else if (btn.dataset.command === 'start') {
        btn.removeAttribute('title');
      }
      btn.disabled = disabled;
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

    // Toggle calibration in progress banner
    const calBanner = $('calibrationBanner');
    if (calBanner) {
      if (state.calibration_locked) {
        calBanner.classList.remove('d-none');
      } else {
        calBanner.classList.add('d-none');
      }
    }

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
      const payload = name === 'abort'
        ? $('abortReason').value
        : (btn.dataset.payload || null);
      const result = await run(name, payload);
      if (name === 'abort' && result.ok) $('abortReason').value = '';
    });
  });

  $('targetForm').addEventListener('submit', async event => {
    event.preventDefault();
    await run('set_target_kg', $('targetInput').value);
    $('targetInput').blur();
  });

  // --- Supervisor Service Card Controls -------------------------------------
  const supervisorBadge = $('supervisorBadge');
  const supervisorSubstate = $('supervisorSubstate');
  const supervisorStartBtn = $('supervisorStartBtn');
  const supervisorStopBtn = $('supervisorStopBtn');
  const supervisorRestartBtn = $('supervisorRestartBtn');
  const supervisorFeedbackEl = $('supervisorFeedback');

  function supervisorFeedback(message, kind) {
    if (!supervisorFeedbackEl) return;
    supervisorFeedbackEl.textContent = message;
    supervisorFeedbackEl.dataset.kind = kind;
  }

  async function pollSupervisor() {
    try {
      const res = await fetch('/api/supervisor');
      if (!res.ok) return;
      const data = await res.json();
      updateSupervisorUI(data);
    } catch (_) {}
  }

  function updateSupervisorUI(data) {
    if (!supervisorBadge || !supervisorSubstate) return;
    const active = data.active && data.heartbeat_ok;
    supervisorBadge.textContent = active ? 'Running' : (data.active ? 'Unresponsive' : 'Stopped');
    supervisorBadge.dataset.state = active ? 'running' : 'offline';

    let sub = data.substate || (data.active ? 'running' : 'stopped');
    if (data.pid) sub += ` (PID ${data.pid})`;
    if (data.active && !data.heartbeat_ok) sub += ' [stale heartbeat]';
    supervisorSubstate.textContent = sub;

    if (supervisorStartBtn) supervisorStartBtn.disabled = data.active;
    if (supervisorStopBtn) supervisorStopBtn.disabled = !data.active;
    if (supervisorRestartBtn) supervisorRestartBtn.disabled = !data.active;
  }

  async function handleSupervisorAction(action) {
    supervisorFeedback('Sending ' + action + '…', 'pending');
    let payload = {};

    const activeBatch = live && ['intake', 'drying', 'cooldown', 'sorting'].includes(live.state);
    if ((action === 'stop' || action === 'restart') && activeBatch) {
      const phrase = window.prompt(
        `A batch is currently running (${live.state}).\n\n` +
        `Stopping or restarting the supervisor will interrupt the batch and shut off heaters and fans while the chamber may be hot.\n\n` +
        `Type "INTERRUPT BATCH" to confirm:`
      );
      if (phrase !== 'INTERRUPT BATCH') {
        supervisorFeedback('Action cancelled: confirmation not matched.', 'refused');
        return;
      }
      payload.confirm = phrase;
    }

    try {
      const res = await fetch(`/api/supervisor/${action}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (res.ok) {
        supervisorFeedback(data.message || action + ' succeeded', 'ok');
      } else {
        supervisorFeedback(data.error || 'Failed: ' + res.statusText, 'refused');
      }
      if (data.supervisor) {
        updateSupervisorUI(data.supervisor);
      } else {
        await pollSupervisor();
      }
    } catch (err) {
      supervisorFeedback('Network error: ' + err.message, 'refused');
    }
  }

  if (supervisorStartBtn) supervisorStartBtn.addEventListener('click', () => handleSupervisorAction('start'));
  if (supervisorStopBtn) supervisorStopBtn.addEventListener('click', () => handleSupervisorAction('stop'));
  if (supervisorRestartBtn) supervisorRestartBtn.addEventListener('click', () => handleSupervisorAction('restart'));

  setInterval(pollSupervisor, 4000);
  pollSupervisor();

});
