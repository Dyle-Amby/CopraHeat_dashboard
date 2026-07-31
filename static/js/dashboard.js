document.addEventListener('DOMContentLoaded', function () {

  // ---- Sensor logs: metric datasets (only runs if the chart canvas exists) ----
  const ctx = document.getElementById('logChart');
  if (ctx) {
    const labels = ['0h', '1h', '2h', '3h', '4h', '5h', '5.9h'];
    const datasets = {
      chamber:  { data: [42, 58, 66, 69, 68, 67, 68], unit: '\u00b0C', color: '#a9822f', min: 30, max: 80 },
      ambient:  { data: [31, 32, 33, 34, 34, 33, 32], unit: '\u00b0C', color: '#8c8577', min: 20, max: 50 },
      weight:   { data: [0, 4, 8, 12, 15, 17, 18],    unit: '%',   color: '#5f7a4f', min: 0,  max: 50 },
      humidity: { data: [70, 60, 52, 46, 44, 44, 43], unit: '%',   color: '#a3452c', min: 0,  max: 90 },
      moisture: { data: [48, 39, 30, 22, 15, 9, 7],   unit: '%',   color: '#2b2a25', min: 0,  max: 55 }
    };

    let currentChart = null;

    function renderChart(key) {
      const d = datasets[key];
      if (currentChart) currentChart.destroy();
      currentChart = new Chart(ctx, {
        type: 'line',
        data: { labels: labels, datasets: [{ data: d.data, borderColor: d.color, backgroundColor: 'transparent', tension: 0.35, pointRadius: 0, borderWidth: 2.5 }] },
        options: {
          plugins: { legend: { display: false } },
          scales: {
            y: { min: d.min, max: d.max, grid: { color: '#e3ddca' }, ticks: { font: { size: 11 }, callback: v => v + d.unit } },
            x: { grid: { display: false }, ticks: { font: { size: 11 } } }
          },
          maintainAspectRatio: false
        }
      });

      const peak = Math.max(...d.data);
      const low = Math.min(...d.data);
      const avg = (d.data.reduce((a, b) => a + b, 0) / d.data.length).toFixed(1);
      const statPeak = document.getElementById('statPeak');
      const statAvg = document.getElementById('statAvg');
      const statLow = document.getElementById('statLow');
      if (statPeak) statPeak.textContent = peak + d.unit;
      if (statAvg) statAvg.textContent = avg + d.unit;
      if (statLow) statLow.textContent = low + d.unit;
    }

    renderChart('chamber');

    document.querySelectorAll('#metricList .list-group-item').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('#metricList .list-group-item').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        renderChart(btn.dataset.metric);
      });
    });
  }

  // ---- Batch history: CSV export ----
  const exportBtn = document.getElementById('exportBtn');
  if (exportBtn) {
    exportBtn.addEventListener('click', () => {
      const rows = [...document.querySelectorAll('#historyTable tr')].map(tr =>
        [...tr.children].map(td => td.textContent.trim().replace(/\s+/g, ' ')).join(',')
      );
      const csv = rows.join('\n');
      const blob = new Blob([csv], { type: 'text/csv' });
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = 'cords_batch_history.csv';
      link.click();
    });
  }

  // ---- Control panel: diverter flap test position (visual only) ----
  document.querySelectorAll('.flap-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.flap-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
    });
  });

  // ---- Control panel: momentary actions (hopper hatch, trapdoor, staging gate) ----
  document.querySelectorAll('.momentary-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const row = btn.closest('.override-row');
      if (row) {
        row.classList.add('pulse-ok');
        setTimeout(() => row.classList.remove('pulse-ok'), 500);
      }
    });
  });

});
