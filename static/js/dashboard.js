// Dashboard page: renders /api/live into the phase track and readout cards.
document.addEventListener('DOMContentLoaded', function () {
  const C = window.CORDS;
  const $ = id => document.getElementById(id);
  let currentDashboardPromptId = null;

  const PHASES = ['intake', 'drying', 'cooldown', 'sorting'];

  const PHASE_NOTES = {
    idle: 'Waiting for Start',
    intake: 'Filling the hopper, then carrying the batch in',
    drying: 'Static tray, closed-loop 60–70°C',
    cooldown: 'Heaters off, fans purging heat',
    sorting: 'Operator stages, grades and routes each piece',
    complete: 'Finished — reset to start another'
  };

  function renderPhaseTrack(state) {
    // a finished batch has been through every phase; idle and aborted light none
    const current = state === 'complete' ? PHASES.length : PHASES.indexOf(state);
    document.querySelectorAll('#phaseTrack .phase-node').forEach((node, i) => {
      const dot = node.querySelector('.phase-dot');
      dot.classList.toggle('done', current >= 0 && i < current);
      dot.classList.toggle('current', i === current);
      node.classList.toggle('is-current', i === current);
    });
  }

  function chamberNote(state, temp) {
    if (temp == null) return 'No reading';
    if (temp > 75) return 'Over-temperature — fans forced on';
    if (state !== 'drying') return '';
    if (temp < 60) return 'Heating up to the 60–70°C band';
    if (temp <= 70) return 'Within the 60–70°C band';
    return 'Above band — heaters off';
  }

  function fanNote(state, fansOn) {
    if (state === 'drying') return fansOn ? 'Continuous airflow while drying' : 'Held off until the chamber reaches 60°C';
    if (state === 'cooldown') return 'Purging residual heat';
    return '';
  }

  function onOff(el, on) {
    el.textContent = on ? 'On' : 'Off';
    el.classList.toggle('text-muted-soft', !on);
  }

  function renderAlarms(alarms) {
    const list = $('alarmList');
    list.innerHTML = '';
    if (!alarms.length) {
      const li = document.createElement('li');
      li.className = 'alarm-none';
      li.textContent = 'No active alarms';
      list.appendChild(li);
      return;
    }
    alarms.forEach(name => {
      const li = document.createElement('li');
      li.innerHTML = '<i class="bi bi-exclamation-triangle-fill"></i>';
      li.appendChild(document.createTextNode(C.alarmText(name)));
      list.appendChild(li);
    });
  }

  C.onLive(function (live) {
    // ── Phase 6: Milestone Checklist Rendering ────────────────────────────────
    const ALL_MILESTONES = [
      'batch_started', 'gate_opened', 'intake_advance_done',
      'heater_on', 'temp_60c_reached', 'fans_pulsing', 'drying_endpoint_reached',
      'cooldown_started', 'sorting_positioning_done',
      'piece_captured', 'piece_reviewed', 'piece_drop_verified', 'piece_sorting_finished'
    ];

    function renderMilestones(events, state) {
      const card = $('milestoneChecklistCard');
      if (!card) return;

      if (!events || !events.length) {
        ALL_MILESTONES.forEach(m => {
          const el = $('m-' + m);
          if (el) {
            el.className = 'milestone-item mb-1 text-muted';
            el.innerHTML = `<i class="bi bi-circle text-muted me-1"></i> ${el.textContent.trim()}`;
          }
        });
        const badge = $('milestonesProgressBadge');
        if (badge) badge.textContent = '0 / 13 Milestones';
        return;
      }

      const eventNames = new Set(events.map(e => e.event));
      let completedCount = 0;

      ALL_MILESTONES.forEach(m => {
        const el = $('m-' + m);
        if (!el) return;
        const cleanLabel = el.textContent.replace(/^[\s\S]*?\s\s*/, '').trim() || el.textContent.trim();

        if (eventNames.has(m)) {
          completedCount++;
          el.className = 'milestone-item mb-1 text-success fw-bold';
          el.innerHTML = `<i class="bi bi-check-circle-fill text-success me-1"></i> ${cleanLabel}`;
        } else {
          el.className = 'milestone-item mb-1 text-muted';
          el.innerHTML = `<i class="bi bi-circle text-muted me-1"></i> ${cleanLabel}`;
        }
      });

      const badge = $('milestonesProgressBadge');
      if (badge) badge.textContent = `${completedCount} / 13 Completed`;
    }

    // ── Phase 6: Dashboard Prompt Handler ─────────────────────────────────────
    function renderDashboardPrompt(prompt) {
      const card = $('dashPromptCard');
      const msgEl = $('dashPromptMessage');
      const optEl = $('dashPromptOptions');
      if (!card || !msgEl || !optEl) return;

      if (!prompt) {
        card.classList.add('d-none');
        currentDashboardPromptId = null;
        return;
      }
      if (currentDashboardPromptId === prompt.id) return;
      currentDashboardPromptId = prompt.id;

      msgEl.textContent = prompt.message || 'Machine awaiting operator decision';
      const review = $('dashPromptReview');
      const reviewImage = $('dashPromptReviewImage');
      const reviewDetails = $('dashPromptReviewDetails');
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
      const options = prompt.options || ['confirm'];
      options.forEach(opt => {
        const btn = document.createElement('button');
        btn.className = (opt === 'abort' || opt === 'discard') ? 'btn btn-sm btn-outline-danger' : 'btn btn-sm btn-primary';
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
        btn.textContent = labels[opt] || opt.charAt(0).toUpperCase() + opt.slice(1);
        btn.addEventListener('click', async () => {
          btn.disabled = true;
          try {
            await C.sendCommand('answer_prompt', { id: prompt.id, action: opt });
          } catch (err) {
            alert('Failed to submit: ' + err.message);
          }
        });
        optEl.appendChild(btn);
      });
      card.classList.remove('d-none');
    }
    renderMilestones(live.batch_events || [], live.state);
    renderDashboardPrompt(live.active_prompt);
    
    const state = live.state;
    renderPhaseTrack(state);

    if (live.supervisor === 'never_run') {
      $('phaseValue').textContent = '—';
      $('phaseNote').textContent = 'Supervisor stopped — start it from the Control Panel';
      return;
    }

    $('phaseValue').textContent = C.STATE_LABELS[state] || '—';
    const batch = live.batch;
    $('phaseNote').textContent = state === 'aborted' && batch && batch.abort_reason
      ? batch.abort_reason
      : (PHASE_NOTES[state] || '');

    onOff($('heaterValue'), live.heater_on);
    onOff($('fanValue'), live.fans_on);
    $('fanNote').textContent = fanNote(state, live.fans_on);

    $('batchNo').textContent = batch ? batch.id : '—';
    $('batchStarted').textContent = batch ? C.date(batch.started_at) : 'No batch running';
    $('batchElapsed').textContent = batch ? C.elapsed(live.elapsed_s) : '—';

    $('chamberValue').textContent = C.num(live.chamber_c, 1, '°C');
    $('chamberNote').textContent = chamberNote(state, live.chamber_c);
    $('exhaustValue').textContent = C.num(live.exhaust_c, 1, '°C') + ' · ' + C.num(live.exhaust_rh, 0, '% RH');
    $('exhaustNote').textContent = 'DHT22 at the exhaust fan';
    $('ahValue').textContent = C.num(live.exhaust_ah, 1, ' g/m³');
    $('hopperValue').textContent = C.num(live.hopper_kg, 2, ' kg');
    $('hopperNote').textContent = 'Target ' + C.num(live.target_kg, 2, ' kg') + (live.gate_open ? ' · gate open' : '');

    renderAlarms(live.alarms || []);

    const pieceEvents = live.batch_events || [];
    const countEvent = name => pieceEvents.filter(event => event.event === name).length;
    $('pieceCapturedCount').textContent = countEvent('piece_captured');
    $('pieceReviewedCount').textContent = countEvent('piece_reviewed');
    $('pieceDroppedCount').textContent = countEvent('piece_drop_verified');
    const prompt = live.active_prompt;
    const promptContext = prompt && prompt.context ? prompt.context : {};
    const lastPieceEvent = pieceEvents.slice().reverse().find(event => event.event.startsWith('piece_'));
    let lastPieceDetails = lastPieceEvent ? lastPieceEvent.details : null;
    if (typeof lastPieceDetails === 'string') {
      try { lastPieceDetails = JSON.parse(lastPieceDetails); } catch { lastPieceDetails = null; }
    }
    const currentPiece = promptContext.piece || (lastPieceDetails && lastPieceDetails.piece);
    $('pieceCurrentNumber').textContent = currentPiece || '—';
    if (state !== 'sorting') {
      $('pieceSortStatus').textContent = state === 'complete' ? 'Sorting complete.' : 'Sorting starts after cooldown.';
    } else if (prompt && prompt.id.startsWith('stage_piece_')) {
      $('pieceSortStatus').textContent = 'Awaiting one-piece staging.';
    } else if (prompt && prompt.id.startsWith('piece_review_')) {
      $('pieceSortStatus').textContent = 'Awaiting piece grade confirmation.';
    } else if (prompt && prompt.id.startsWith('more_pieces_')) {
      $('pieceSortStatus').textContent = 'Awaiting next-piece decision.';
    } else {
      $('pieceSortStatus').textContent = 'Sorting in progress.';
    }
  });
});
