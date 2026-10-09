// Shared by every page: formatting, the /api/live poller, the header pill and
// bell, and sending operator commands. Page scripts build on window.CORDS.
window.CORDS = (function () {

  const LIVE_POLL_MS = 2000;
  const COMMAND_POLL_MS = 300;
  const COMMAND_TIMEOUT_MS = 10000;

  const STATE_LABELS = {
    idle: 'Idle', intake: 'Intake', drying: 'Drying', cooldown: 'Cooldown',
    sorting: 'Sorting', complete: 'Complete', aborted: 'Aborted'
  };

  // keys match the alarm names the state machine raises (hardware/batch.py)
  const ALARM_TEXT = {
    overtemp: 'Chamber above 75°C — fans forced on',
    chamber_sensor_fault: 'Chamber sensor (DS18B20) not reading — heaters held off',
    exhaust_sensor_fault: 'Exhaust sensor (DHT22) not reading — drying endpoint cannot be judged',
    hopper_jam: 'Hopper gate open over 30 s without emptying — check for a jam',
    intake_stall: 'Hopper never reached the target weight',
    conveyor_stall: 'Conveyor did not acknowledge a move',
    drying_timeout: 'Drying hit its time ceiling without reaching the endpoint',
    sorting_timeout: 'Sorting ran past its time limit',
    aborted: 'Batch aborted'
  };

  const SUPERVISOR_TEXT = {
    running: 'Machine online',
    stopped: 'Supervisor stopped',
    not_responding: 'Not responding',
    never_run: 'Supervisor never started',
    unreachable: 'Dashboard offline'
  };

  function num(value, digits = 1, unit = '') {
    return value == null ? '—' : value.toFixed(digits) + unit;
  }

  function elapsed(seconds) {
    if (seconds == null) return '—';
    const s = Math.floor(seconds);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return (h ? h + 'h ' : '') + (h || m ? m + 'm ' : '') + sec + 's';
  }

  function date(iso) {
    if (!iso) return '—';
    return new Date(iso).toLocaleString(undefined, {
      month: 'short', day: 'numeric', year: 'numeric', weekday: 'short', hour: '2-digit', minute: '2-digit'
    });
  }

  function alarmText(name) {
    return ALARM_TEXT[name] || name.replace(/_/g, ' ');
  }

  async function getJSON(url, options) {
    const resp = await fetch(url, Object.assign({ cache: 'no-store' }, options));
    let body = null;
    try { body = await resp.json(); } catch (e) { /* non-JSON error page */ }
    if (!resp.ok) {
      const err = new Error((body && body.error) || resp.statusText);
      err.status = resp.status;
      throw err;
    }
    return body;
  }

  // ---- live state: one poller per page, any number of listeners ----

  const listeners = [];
  let last = null;

  function onLive(fn) {
    listeners.push(fn);
    if (last) fn(last);
  }

  function renderHeader(live) {
    const pill = document.getElementById('supervisorPill');
    const text = document.getElementById('supervisorText');
    if (pill && text) {
      pill.dataset.status = live.supervisor;
      let label = SUPERVISOR_TEXT[live.supervisor] || live.supervisor;
      if (live.supervisor === 'not_responding' && live.age_s != null) {
        label += ' · ' + Math.round(live.age_s) + 's';
      }
      text.textContent = label;
    }
    const dot = document.getElementById('bellDot');
    const bell = document.getElementById('alarmBell');
    const alarms = live.alarms || [];
    if (dot) dot.classList.toggle('d-none', alarms.length === 0);
    if (bell) bell.title = alarms.length ? alarms.map(alarmText).join('\n') : 'No active alarms';
    // readings from a dead supervisor are history, not the machine's state
    document.body.classList.toggle('is-stale', live.supervisor !== 'running');
  }

  async function pollLive() {
    try {
      last = await getJSON('/api/live');
    } catch (e) {
      last = Object.assign({}, last || { alarms: [] }, { supervisor: 'unreachable' });
    }
    renderHeader(last);
    listeners.forEach(fn => fn(last));
    setTimeout(pollLive, LIVE_POLL_MS);
  }

  // ---- commands ----

  // Queue a command and wait for the machine's verdict. Resolves to
  // { ok, message }; never throws, so callers can show the message as-is.
  async function sendCommand(name, payload) {
    let row;
    try {
      row = await getJSON('/api/commands', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name, payload: payload })
      });
    } catch (e) {
      return { ok: false, message: e.message };
    }
    const deadline = Date.now() + COMMAND_TIMEOUT_MS;
    while (row.status === 'pending') {
      if (Date.now() > deadline) {
        return { ok: false, message: 'no answer from the machine yet — check the state before retrying' };
      }
      await new Promise(r => setTimeout(r, COMMAND_POLL_MS));
      try { row = await getJSON('/api/commands/' + row.id); } catch (e) { /* keep waiting */ }
    }
    return { ok: row.status === 'done', message: row.result };
  }

  document.addEventListener('DOMContentLoaded', pollLive);

  return { STATE_LABELS, num, elapsed, date, alarmText, getJSON, onLive, sendCommand };
})();
