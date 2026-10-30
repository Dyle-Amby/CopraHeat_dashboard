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
    // Drying timer button: only active during drying
    const timerBtn = $('dryingTimerBtn');
    if (timerBtn) {
      timerBtn.disabled = busy || !running || !(live && live.state === 'drying');
    }
  }

  function feedback(message, kind) {
    const el = $('cmdFeedback');
    el.textContent = message;
    el.dataset.kind = kind;
  }

  async function run(name, payload) {
    busy = true;
    refreshButtons();
    feedback('Sending\u2026', 'pending');
    const result = await C.sendCommand(name, payload);
    feedback(result.message, result.ok ? 'ok' : 'refused');
    busy = false;
    refreshButtons();
    return result;
  }

  // ── Active Prompt ──────────────────────────────────────────────────────────
  let currentPromptId = null;

  function showPrompt(prompt) {
    const card = $('activePromptCard');
    const msgEl = $('promptMessage');
    const optEl = $('promptOptions');
    if (!card || !msgEl || !optEl) return;

    if (currentPromptId === prompt.id) return;
    currentPromptId = prompt.id;

    msgEl.textContent = prompt.message || 'Machine awaiting operator decision';
    const review = $('promptReview');
    const reviewImage = $('promptReviewImage');
    const reviewDetails = $('promptReviewDetails');
    const context = prompt.context || {};
    const showReview = Boolean(context.image_url || context.predicted_grade || context.scores);
    if (review) review.classList.toggle('d-none', !showReview);
    if (reviewImage) {
      reviewImage.classList.toggle('d-none', !context.image_url);
      if (context.image_url) reviewImage.src = context.image_url;
      else reviewImage.removeAttribute('src');
    }
    if (reviewDetails) {
      const prediction = context.predicted_grade || context.grade || 'No confident classification';
      const confidence = Number.isFinite(Number(context.confidence))
        ? ` · ${(Number(context.confidence) * 100).toFixed(1)}% confidence`
        : '';
      const scoreText = context.scores
        ? Object.entries(context.scores).map(([grade, score]) => `${grade}: ${(Number(score) * 100).toFixed(1)}%`).join(' · ')
        : '';
      reviewDetails.textContent = [`Suggested bin: ${prediction}${confidence}`, scoreText, context.warning].filter(Boolean).join('\n');
    }
    optEl.innerHTML = '';

    (prompt.options || []).forEach(opt => {
      const btn = document.createElement('button');
      btn.className = opt === 'abort' ? 'btn-danger-muted' : 'btn-outline-ink';
      btn.style.cssText = 'font-size:0.85rem; padding:4px 12px;';
      const labels = {
        jog_reverse: 'Jog back 250 steps',
        jog_forward: 'Jog forward 250 steps',
        piece_staged: 'Piece is staged',
        accept_suggested: context.predicted_grade ? `Accept ${context.predicted_grade}` : 'Accept suggested bin',
        bad: 'Bad bin',
        good: 'Good bin',
        great: 'Great bin',
        retake: 'Retake photo',
        yes_more: 'Yes, more copra',
        no_more: 'No, finish sorting',
        retry_capture: 'Retry photo',
      };
      btn.textContent = labels[opt] || opt.replace(/_/g, ' ');
      btn.addEventListener('click', async () => {
        if (opt === 'abort' && !window.confirm('Abort the sorting operation?')) return;
        btn.disabled = true;
        await run('answer_prompt', JSON.stringify({ id: prompt.id, action: opt }));
      });
      optEl.appendChild(btn);
    });

    card.classList.remove('d-none');
  }

  function hidePrompt() {
    const card = $('activePromptCard');
    if (card) card.classList.add('d-none');
    currentPromptId = null;
  }

  // ── Live state handler ─────────────────────────────────────────────────────
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

    // Active prompt
    if (state.active_prompt) {
      showPrompt(state.active_prompt);
    } else {
      hidePrompt();
    }

    // don't overwrite what the operator is typing
    const input = $('targetInput');
    if (document.activeElement !== input && state.target_kg != null) {
      input.value = state.target_kg;
    }
    refreshButtons();
  });

  // ── Generic command buttons ───────────────────────────────────────────────
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

  // ── Drying timer form ──────────────────────────────────────────────────────
  const dryingTimerForm = $('dryingTimerForm');
  if (dryingTimerForm) {
    dryingTimerForm.addEventListener('submit', async event => {
      event.preventDefault();
      const minutes = parseFloat($('dryingTimerInput').value);
      if (!isFinite(minutes) || minutes <= 0) {
        feedback('Enter a positive number of minutes for the drying timer.', 'refused');
        return;
      }
      await run('set_drying_timer', { duration_min: minutes });
      $('dryingTimerInput').blur();
    });
  }

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
    supervisorFeedback('Sending ' + action + '\u2026', 'pending');
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
      const headers = { 'Content-Type': 'application/json' };
      const savedPin = sessionStorage.getItem('cords_pin');
      if (savedPin) headers['X-CORDS-PIN'] = savedPin;

      let res = await fetch(`/api/supervisor/${action}`, {
        method: 'POST',
        headers: headers,
        body: JSON.stringify(payload)
      });

      // If PIN is required and missing or invalid, prompt the operator and retry
      if (res.status === 403) {
        const pin = window.prompt('Supervisor action requires security PIN:');
        if (pin) {
          sessionStorage.setItem('cords_pin', pin);
          headers['X-CORDS-PIN'] = pin;
          res = await fetch(`/api/supervisor/${action}`, {
            method: 'POST',
            headers: headers,
            body: JSON.stringify(payload)
          });
        } else {
          supervisorFeedback('Action cancelled: PIN required.', 'refused');
          return;
        }
      }

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
