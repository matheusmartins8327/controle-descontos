/* Filtros e ordenação por coluna para tabelas com class="tabela-filtro".
   Em cada <th>: data-filtro="lista" (lista de valores) | "texto" (contém) | "nenhum";
   data-tipo="numero" | "data" ordena pelo data-ordem da célula. Em cada <td>, data-filtro substitui o texto
   usado na lista e data-ordem o valor usado na ordenação. */
(function () {
  const norm = s => (s || '').toString().normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().replace(/\s+/g, ' ').trim();
  const valorFiltro = td => (td && (td.dataset.filtro !== undefined ? td.dataset.filtro : td.textContent) || '').replace(/\s+/g, ' ').trim();

  document.querySelectorAll('table.tabela-filtro').forEach(tabela => {
    const cab = tabela.tHead.rows[0], ths = Array.from(cab.cells);
    const linhas = () => Array.from(tabela.tBodies).flatMap(tb => Array.from(tb.rows)).filter(tr => !tr.classList.contains('titulo-grupo') && tr.cells.length === ths.length);
    const filtros = new Array(ths.length).fill(null);
    const linhaFiltro = document.createElement('tr'); linhaFiltro.className = 'linha-filtros';
    ths.forEach((th, i) => {
      const cel = document.createElement('th'), tipo = th.dataset.filtro || 'texto';
      if (tipo === 'lista') {
        const sel = document.createElement('select'); sel.innerHTML = '<option value="">Todos</option>';
        const valores = [...new Set(linhas().map(tr => valorFiltro(tr.cells[i])).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'pt-BR'));
        valores.forEach(v => { const o = document.createElement('option'); o.value = v; o.textContent = v; sel.appendChild(o); });
        sel.addEventListener('change', aplicar); cel.appendChild(sel); filtros[i] = {tipo, el: sel};
      } else if (tipo === 'texto') {
        const inp = document.createElement('input'); inp.type = 'search';
        inp.placeholder = th.dataset.tipo === 'data' ? 'ex.: 10/2026' : 'Filtrar…';
        inp.addEventListener('input', aplicar); cel.appendChild(inp); filtros[i] = {tipo, el: inp};
      }
      linhaFiltro.appendChild(cel);
      if (th.textContent.trim()) {  // ordenação ao clicar no título
        th.classList.add('ordenavel'); th.title = 'Clique para ordenar';
        th.addEventListener('click', () => ordenar(i, th));
      }
    });
    tabela.tHead.appendChild(linhaFiltro);

    const info = document.createElement('div'); info.className = 'filtro-info';
    info.innerHTML = '<span></span> <button type="button" class="btn pequeno link">Limpar filtros</button>';
    tabela.closest('.rolagem')?.before(info) || tabela.before(info);
    info.querySelector('button').addEventListener('click', () => { filtros.forEach(f => f && (f.el.value = '')); aplicar(); });

    function aplicar() {
      let vis = 0, total = 0, ativo = filtros.some(f => f && f.el.value);
      linhas().forEach(tr => {
        total++;
        const ok = filtros.every((f, i) => {
          if (!f || !f.el.value) return true;
          const v = valorFiltro(tr.cells[i]);
          return f.tipo === 'lista' ? v === f.el.value : norm(v).includes(norm(f.el.value));
        });
        tr.hidden = !ok; if (ok) vis++;
      });
      Array.from(tabela.tBodies).forEach(tb => {  // grupos sem linhas visíveis somem
        const tit = tb.querySelector('tr.titulo-grupo'); if (!tit) return;
        const n = Array.from(tb.rows).filter(tr => tr !== tit && !tr.hidden).length;
        tit.hidden = n === 0; const c = tit.querySelector('.cont'); if (c) c.textContent = n;
      });
      info.querySelector('span').textContent = ativo ? `Mostrando ${vis} de ${total} ${tabela.dataset.rotulo || 'linhas'}` : '';
      info.classList.toggle('ativo', ativo);
    }

    function chave(td, tipo) {
      const o = td && td.dataset.ordem;
      if (tipo === 'numero') return o !== undefined && o !== '' ? parseFloat(o) : -Infinity;
      if (tipo === 'data') return o || '';
      return norm(td ? td.textContent : '');
    }
    function ordenar(i, th) {
      const asc = th.dataset.ordem !== 'asc';
      ths.forEach(t => { delete t.dataset.ordem; t.classList.remove('asc', 'desc'); });
      th.dataset.ordem = asc ? 'asc' : 'desc'; th.classList.add(asc ? 'asc' : 'desc');
      const tipo = th.dataset.tipo || 'texto';
      Array.from(tabela.tBodies).forEach(tb => {
        const tit = tb.querySelector('tr.titulo-grupo');
        const rows = Array.from(tb.rows).filter(tr => tr !== tit);
        rows.sort((a, b) => {
          const x = chave(a.cells[i], tipo), y = chave(b.cells[i], tipo);
          const vazioX = x === '' || x === -Infinity, vazioY = y === '' || y === -Infinity;
          if (vazioX !== vazioY) return vazioX ? 1 : -1;  // vazios sempre no fim
          return (x < y ? -1 : x > y ? 1 : 0) * (asc ? 1 : -1);
        });
        rows.forEach(tr => tb.appendChild(tr));
      });
    }
  });
})();
