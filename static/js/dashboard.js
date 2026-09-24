// Dashboard page: renders /api/live into the phase track and readout cards.
document.addEventListener('DOMContentLoaded', function () {
  const C = window.CORDS;
  const $ = id => document.getElementById(id);

  const PHASES = ['intake', 'drying', 'cooldown', 'sorting'];

  const PHASE_NOTES = {
    idle: 'Waiting for Start',
    intake: 'Filling the hopper, then carrying the batch in',
    drying: 'Static tray, closed-loop 60–70°C',
    cooldown: 'Heaters off, fans purging heat',
    sorting: 'Belt running past the camera',
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
    const state = live.state;
    renderPhaseTrack(state);

    if (live.supervisor === 'never_run') {
      $('phaseValue').textContent = '—';
      $('phaseNote').textContent = 'Start the supervisor: python -m hardware.run_cords';
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

    if (batch && (batch.great != null || batch.good != null || batch.bad != null)) {
      $('sortNote').textContent = 'Batch #' + batch.id;
      $('tallyGreat').textContent = batch.great ?? '—';
      $('tallyGood').textContent = batch.good ?? '—';
      $('tallyBad').textContent = batch.bad ?? '—';
    }
  });
});
