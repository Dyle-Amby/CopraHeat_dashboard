// Batch history: CSV export of the table as shown.
document.addEventListener('DOMContentLoaded', function () {
  const exportBtn = document.getElementById('exportBtn');
  if (!exportBtn) return;

  // quote every field: dates like "Aug 20, 2026" carry commas of their own
  const cell = text => '"' + text.trim().replace(/\s+/g, ' ').replace(/"/g, '""') + '"';

  exportBtn.addEventListener('click', () => {
    const rows = [...document.querySelectorAll('#historyTable tr')].map(tr =>
      [...tr.children].map(td => cell(td.textContent)).join(',')
    );
    const blob = new Blob([rows.join('\n')], { type: 'text/csv' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = 'cords_batch_history.csv';
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  });
});
