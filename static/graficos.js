/* v4.66: gráficos de barras dos relatórios (Chart.js), com o valor escrito em cima de cada barra.
   Cada <canvas data-serie='[["rótulo", valor], ...]' data-formato="n|dias|pct" data-cores="canal|"> vira um gráfico.
   Se a biblioteca não carregar, aparece um aviso no lugar (nunca um gráfico em branco). */
(function () {
  var CORES_CANAL = { 'Verde': '#2e9e5b', 'Amarelo': '#e0b400', 'Vermelho': '#d64545', 'Sem canal': '#9aa4ae' };
  function rotulo(v, f) {
    if (v === null || v === undefined) return '';
    if (f === 'pct') return Math.round(v) + '%';
    if (f === 'dias') return (Math.round(v * 10) / 10).toString().replace('.', ',');
    return String(v).replace('.', ',');
  }
  var valores = {
    id: 'valoresNasBarras',
    afterDatasetsDraw: function (chart) {
      var ctx = chart.ctx, f = chart.options._formato;
      ctx.save();
      ctx.font = '600 11px system-ui, sans-serif';
      ctx.fillStyle = '#212529';
      ctx.textAlign = 'center';
      chart.getDatasetMeta(0).data.forEach(function (bar, i) {
        var v = chart.data.datasets[0].data[i];
        var y = v < 0 ? bar.y + 13 : bar.y - 4;
        ctx.fillText(rotulo(v, f), bar.x, y);
      });
      ctx.restore();
    }
  };
  function aviso(c, texto) {
    var d = document.createElement('p');
    d.className = 'grafico-aviso';
    d.textContent = texto;
    c.parentNode.replaceChild(d, c);
  }
  document.querySelectorAll('canvas[data-serie]').forEach(function (c) {
    var serie = JSON.parse(c.dataset.serie || '[]');
    if (!serie.length) { aviso(c, '— sem dados suficientes para este gráfico'); return; }
    if (typeof Chart === 'undefined') { aviso(c, 'Não foi possível carregar a biblioteca de gráficos (sem acesso à internet?). Os números estão nas tabelas e nos arquivos Excel/PDF.'); return; }
    var cores = serie.map(function (s) { return (c.dataset.cores === 'canal' && CORES_CANAL[s[0]]) || '#327fef'; });
    new Chart(c, {
      type: 'bar',
      data: { labels: serie.map(function (s) { return s[0]; }),
              datasets: [{ data: serie.map(function (s) { return s[1]; }), backgroundColor: cores, borderRadius: 3, maxBarThickness: 46 }] },
      options: {
        _formato: c.dataset.formato, responsive: true, maintainAspectRatio: false, animation: false,
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: function (x) { return rotulo(x.raw, c.dataset.formato); } } } },
        scales: { y: { beginAtZero: true, grace: '12%', ticks: { precision: 0 } },
                  x: { ticks: { autoSkip: false, maxRotation: 40, font: { size: 11 } } } }
      },
      plugins: [valores]
    });
  });
})();
