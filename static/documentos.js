/* Seleção de documento: abre a janela "Abrir" do Windows; como os navegadores não revelam a pasta do arquivo
   escolhido, a pasta da rede é informada/lembrada aqui e o sistema salva só o CAMINHO completo.
   Também calcula a "impressão digital" do arquivo (nome, tamanho, data, SHA-256) para a futura validação do conteúdo. */
(function () {
  const RE_REDE = /^(\\\\[^\\\/:*?"<>|]+\\[^\\\/:*?"<>|]+.*|[A-Za-z]:\\.+)$/;
  const guardar = (k, v) => { try { localStorage.setItem(k, v); } catch (e) {} };
  const ler = k => { try { return localStorage.getItem(k) || ''; } catch (e) { return ''; } };
  const esc = t => (t || '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const tamanho = n => n >= 1048576 ? (n / 1048576).toFixed(1).replace('.', ',') + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB';
  const pastaDe = c => { const i = c.lastIndexOf('\\'); return i > 0 ? c.slice(0, i + 1) : ''; };
  const juntar = (p, n) => !p ? n : (p.endsWith('\\') ? p : p + '\\') + n;
  async function sha256(arquivo) {
    if (!window.crypto || !crypto.subtle || arquivo.size > 200 * 1048576) return '';
    const buf = await crypto.subtle.digest('SHA-256', await arquivo.arrayBuffer());
    return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join('');
  }

  function abrirJanela(botao, arquivo) {
    const origem = botao.dataset.origem, form = botao.dataset.docForm ? document.getElementById(botao.dataset.docForm) : null;
    const atual = botao.dataset.atual || '';
    let pasta = (atual && pastaDe(atual)) || ler('pasta_doc_' + origem) || botao.dataset.pasta || '';
    const nome = arquivo ? arquivo.name : (atual ? atual.slice(pastaDe(atual).length) : '');
    const fundo = document.createElement('div'); fundo.className = 'modal-fundo';
    fundo.innerHTML = `<div class="modal modal-doc" role="dialog" aria-modal="true">
      <h2>${atual ? 'Corrigir caminho' : 'Selecionar documento'} <small>· ${esc(botao.dataset.rotulo || '')}</small></h2>
      ${arquivo ? `<div class="arq-escolhido">📄 <strong>${esc(arquivo.name)}</strong> <small>${tamanho(arquivo.size)} · modificado em ${new Date(arquivo.lastModified).toLocaleString('pt-BR')}</small></div>`
                : `<p class="sub">Sem arquivo selecionado: informe o caminho manualmente${atual ? '' : ' ou '}${atual ? '' : '<button type="button" class="btn pequeno link" data-acao="escolher">selecione o arquivo</button>'}.</p>`}
      <label>Pasta na rede da empresa<input name="pasta" value="${esc(pasta)}" placeholder="\\\\servidor\\Financeiro\\Swift\\2026\\" autocomplete="off">
        <small>O navegador não informa a pasta do arquivo escolhido: confira se é a pasta onde ele está salvo na rede (ela fica lembrada para a próxima vez).</small></label>
      <label>Caminho completo que será salvo<input name="completo" value="${esc(juntar(pasta, nome))}" autocomplete="off">
        <small>Dica: no Explorador de Arquivos, segure Shift, clique com o botão direito no arquivo → <em>Copiar como caminho</em> e cole aqui.</small></label>
      <p class="erro-texto" data-erro hidden>Use o caminho completo na rede: \\\\servidor\\pasta\\arquivo.pdf ou Z:\\pasta\\arquivo.pdf.</p>
      <div class="botoes"><button type="button" class="btn" data-acao="cancelar">Cancelar</button>
        <button type="button" class="btn primario" data-acao="confirmar">Confirmar</button></div></div>`;
    document.body.appendChild(fundo);
    const inPasta = fundo.querySelector('[name=pasta]'), inComp = fundo.querySelector('[name=completo]');
    let editouCompleto = false;
    inPasta.addEventListener('input', () => { if (!editouCompleto) inComp.value = juntar(inPasta.value.trim().replace(/^"|"$/g, ''), nome); });
    inComp.addEventListener('input', () => { editouCompleto = true; });
    (arquivo ? inPasta : inComp).focus();
    const fechar = () => fundo.remove();
    fundo.addEventListener('keydown', e => { if (e.key === 'Escape') fechar(); });
    fundo.addEventListener('click', async e => {
      const acao = e.target.dataset.acao;
      if (acao === 'cancelar') fechar();
      if (acao === 'escolher') { fechar(); escolher(botao); }
      if (acao !== 'confirmar') return;
      const caminho = inComp.value.trim().replace(/^"|"$/g, '').replace(/\//g, '\\');
      if (!RE_REDE.test(caminho)) { fundo.querySelector('[data-erro]').hidden = false; inComp.focus(); return; }
      guardar('pasta_doc_' + origem, pastaDe(caminho));
      if (botao.dataset.alvo) { document.getElementById(botao.dataset.alvo).value = caminho; fechar(); return; }
      form.caminho.value = caminho;
      if (arquivo) {
        e.target.disabled = true; e.target.textContent = 'Salvando…';
        form.nome_arquivo.value = arquivo.name; form.tamanho.value = arquivo.size;
        const d = new Date(arquivo.lastModified), z = n => String(n).padStart(2, '0');
        form.modificado_em.value = `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())} ${z(d.getHours())}:${z(d.getMinutes())}:${z(d.getSeconds())}`;
        try { form.sha256.value = await sha256(arquivo); } catch (err) { form.sha256.value = ''; }
      }
      form.submit();
    });
  }

  function escolher(botao) {
    const inp = document.createElement('input'); inp.type = 'file';
    inp.addEventListener('change', () => { if (inp.files[0]) abrirJanela(botao, inp.files[0]); });
    inp.click();
  }

  document.addEventListener('click', e => {
    const botao = e.target.closest('[data-doc-form]'); if (!botao) return;
    if (botao.tagName === 'FORM') return;
    e.preventDefault();
    if (botao.dataset.atual) abrirJanela(botao, null);   // corrigir: edita o caminho (ou escolhe outro arquivo)
    else escolher(botao);                                  // novo: abre a janela "Abrir" do Windows
  });
})();
