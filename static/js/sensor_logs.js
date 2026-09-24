// Sensor logs: one batch's sensor_log rows as a chart, one metric at a time.
// A batch that is still running is topped up with only the rows added since.
document.addEventListener('DOMContentLoaded', function () {
  const C = window.CORDS;
  const canvas = document.getElementById('logChart');
  if (!canvas) return;   // no batches yet

  const REFRESH_MS = 10000;
  const batchId = canvas.dataset.batch;

  const METRICS = {
    chamber_c:  { unit: '°C',    digits: 1, color: '#a9822f' },
    exhaust_c:  { unit: '°C',    digits: 1, color: '#8c8577' },
    exhaust_rh: { unit: '%',          digits: 0, color: '#a3452c' },
    exhaust_ah: { unit: ' g/m³', digits: 1, color: '#5f7a4f' },
    hopper_kg:  { unit: ' kg',        digits: 2, color: '#2b2a25' }
  };

  let samples = [];
  let metric = 'chamber_c';
  let chart = null;

  function render() {
    const m = METRICS[metric];
    // x in hours of batch time; a null reading leaves a gap, not a fake zero
    const points = samples.map(s => ({ x: (s.elapsed_s || 0) / 3600, y: s[metric] }));
    if (chart) chart.destroy();
    chart = new Chart(canvas, {
      type: 'line',
      data: { datasets: [{ data: points, borderColor: m.color, backgroundColor: 'transparent', tension: 0.3, pointRadius: 0, borderWidth: 2.5, spanGaps: false }] },
      options: {
        animation: false,
        parsing: false,
        plugins: { legend: { display: false } },
        scales: {
          y: { grid: { color: '#e3ddca' }, ticks: { font: { size: 11 }, callback: v => v + m.unit } },
          x: { type: 'linear', grid: { display: false }, ticks: { font: { size: 11 }, callback: v => v.toFixed(1) + 'h' } }
        },
        maintainAspectRatio: false
      }
    });

    const values = samples.map(s => s[metric]).filter(v => v != null);
    const stat = (id, v) => { document.getElementById(id).textContent = C.num(v, m.digits, m.unit); };
    if (values.length) {
      stat('statPeak', Math.max(...values));
      stat('statAvg', values.reduce((a, b) => a + b, 0) / values.length);
      stat('statLow', Math.min(...values));
    } else {
      ['statPeak', 'statAvg', 'statLow'].forEach(id => { document.getElementById(id).textContent = '—'; });
    }
    const lastElapsed = samples.length ? samples[samples.length - 1].elapsed_s : null;
    document.getElementById('statElapsed').textContent = C.elapsed(lastElapsed);
  }

  async function load() {
    const after = samples.length ? '?after=' + samples[samples.length - 1].id : '';
    try {
      const rows = await C.getJSON('/api/batches/' + batchId + '/samples' + after);
      if (rows.length || !chart) {
        samples = samples.concat(rows);
        render();
      }
      const batch = await C.getJSON('/api/batches/' + batchId);
      if (!batch.finished_at) setTimeout(load, REFRESH_MS);
    } catch (e) {
      setTimeout(load, REFRESH_MS);
    }
  }

  document.querySelectorAll('#metricList .list-group-item').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#metricList .list-group-item').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      metric = btn.dataset.metric;
      render();
    });
  });

  document.getElementById('batchSelect').addEventListener('change', event => {
    window.location.search = '?batch=' + event.target.value;
  });

  load();
});
