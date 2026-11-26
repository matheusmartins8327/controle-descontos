"""Relatórios da importação (v4.66): status, indicadores, itens por situação, importações por canal,
relatório mensal, resumo semanal e alertas.

Funções puras (sem banco). O app.py monta a lista de processos (dicionários com os campos abaixo) e todos os
relatórios — tela, Excel e PDF — usam estas mesmas funções, recalculadas a cada pedido: assim nunca se contradizem.

Campos de um processo (todas as datas como texto aaaa-mm-dd; vazio = None):
  id, fornecedor, pais, invoice, valor, moeda, agente, despachante, consolidacao, duimp,
  coleta_prevista, data_coleta, etd_previsto, etd_real, hawb, eta_prevista, eta_real, data_duimp,
  data_parametrizacao, canal (Verde/Amarelo/Vermelho), exig_descricao, exig_acao, exig_status
  (PENDENTE/CUMPRIDA/FINALIZADA), exig_prazo, data_desembaraco, entrega_prevista, entrega_real, obs,
  criado_em, ultima_atualizacao, itens [{codigo, descricao, quantidade}]
Qualquer cálculo sem dados suficientes devolve None, que aparece como "—" (nunca zero nem valor inventado).
"""
from datetime import date

MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
MESES_LONGO = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
               "novembro", "dezembro"]

# Status, do mais avançado para o menos avançado: (chave, título, texto curto, cor)
STATUS = [
    ("ENTREGUE", "Entregue na Quinelato", "ENTREGUE", "verde"),
    ("DESEMBARACADO", "Desembaraçado — em transporte para a Quinelato", "DESEMBARAÇADO", "verde"),
    ("EXIG_FINALIZADA", "Exigência finalizada — aguardando desembaraço", "AGUARDANDO DESEMBARAÇO", "azul"),
    ("EXIG_CUMPRIDA", "Exigência cumprida — aguardando análise", "EXIGÊNCIA CUMPRIDA", "amarelo"),
    ("RETIDO", "Retido — exigência pendente", "RETIDO", "vermelho"),
    ("ANALISE", "Em análise pelo fiscal — sem exigência até o momento", "EM ANÁLISE FISCAL", "amarelo"),
    ("LIBERADO", "Liberado — canal verde", "LIBERADO", "verde"),
    ("EM_CANAL", "Em canal — aguardando parametrização", "EM CANAL", "neutro"),
    ("DUIMP", "DUIMP emitida — aguardando parametrização", "DUIMP EMITIDA", "neutro"),
    ("CHEGOU", "Chegou no Brasil — aguardando DUIMP", "CHEGOU NO BRASIL", "neutro"),
    ("TRANSITO", "Em trânsito", "EM TRÂNSITO", "neutro"),
    ("COLETADO", "Coletado — aguardando embarque", "COLETADO", "neutro"),
    ("AGUARDANDO", "Aguardando coleta", "AGUARDANDO COLETA", "neutro"),
]
TITULO = {k: t for k, t, _c, _x in STATUS}
CURTO = {k: c for k, _t, c, _x in STATUS}
ORDEM = {k: i for i, (k, *_r) in enumerate(STATUS)}
RETIDOS = ("RETIDO", "EXIG_CUMPRIDA", "ANALISE")  # itens "retidos" (relatório de itens)
CANAIS = ["Verde", "Amarelo", "Vermelho"]

# Datas-marco da linha do tempo (relatório mensal), na ordem do fluxo
MARCOS = [("data_coleta", "Coleta"), ("hawb", "Emissão da HAWB"), ("etd_real", "Embarque"),
          ("eta_real", "Chegada no Brasil"), ("data_duimp", "DUIMP emitida"), ("data_parametrizacao", "Parametrização"),
          ("data_desembaraco", "Desembaraço"), ("entrega_real", "Entrega na Quinelato")]
# datas ignoradas pelo "como estava na data" (as previsões também: uma previsão feita depois não existia ainda)
CAMPOS_DATA = ["coleta_prevista", "data_coleta", "etd_previsto", "etd_real", "hawb", "eta_prevista", "eta_real",
               "data_duimp", "data_parametrizacao", "exig_prazo", "data_desembaraco", "entrega_prevista", "entrega_real"]


# ---------------------------------------------------------------------------
# Datas
# ---------------------------------------------------------------------------
def _d(x):
    if not x:
        return None
    try:
        return date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def dias_entre(a, b):
    """b − a em dias (None se faltar uma das datas)."""
    da, db = _d(a), _d(b)
    return (db - da).days if da and db else None


def dm(x):
    """'2026-09-05' -> '05/09'"""
    d = _d(x)
    return d.strftime("%d/%m") if d else "—"


def dma(x):
    d = _d(x)
    return d.strftime("%d/%m/%Y") if d else "—"


def dia_mes_abrev(x):
    """'2026-09-05' -> '05/set' (planilha de importações por canal)"""
    d = _d(x)
    return f"{d.day:02d}/{MESES[d.month - 1]}" if d else "—"


def mes_ano(x):
    d = _d(x)
    return f"{MESES[d.month - 1]}/{d.year % 100:02d}" if d else None


def num(v, casas=1, sufixo=""):
    """Número para exibir: None -> '—'."""
    if v is None:
        return "—"
    t = f"{v:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return t + sufixo


def pct(v):
    return "—" if v is None else num(v * 100, 0, "%")


def plural(n, um, varios):
    return f"{n} {um if abs(n) == 1 else varios}"


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def status(p):
    """(chave, título, detalhe, cor) — etapa mais avançada preenchida."""
    canal = p.get("canal")
    if p.get("entrega_real"):
        return "ENTREGUE", TITULO["ENTREGUE"], f"entregue em {dm(p['entrega_real'])}", "verde"
    if p.get("data_desembaraco"):
        return ("DESEMBARACADO", TITULO["DESEMBARACADO"],
                f"entrega prevista para {dma(p['entrega_prevista'])}" if p.get("entrega_prevista")
                else "sem previsão de entrega", "verde")
    if canal in ("Amarelo", "Vermelho"):
        det = f"canal {canal.lower()}"
        exig = p.get("exig_status")
        if exig == "PENDENTE":
            return "RETIDO", TITULO["RETIDO"], det, "vermelho"
        if exig == "CUMPRIDA":
            return "EXIG_CUMPRIDA", TITULO["EXIG_CUMPRIDA"], det, "amarelo"
        if exig == "FINALIZADA":
            return "EXIG_FINALIZADA", TITULO["EXIG_FINALIZADA"], det, "azul"
        return "ANALISE", TITULO["ANALISE"], det, "amarelo" if canal == "Amarelo" else "vermelho"
    if p.get("data_parametrizacao") and canal == "Verde":
        return "LIBERADO", TITULO["LIBERADO"], f"parametrizado em {dm(p['data_parametrizacao'])}", "verde"
    if p.get("data_parametrizacao"):
        return "EM_CANAL", TITULO["EM_CANAL"], f"parametrizado em {dm(p['data_parametrizacao'])}", "neutro"
    if p.get("data_duimp"):
        return "DUIMP", TITULO["DUIMP"], f"DUIMP emitida em {dm(p['data_duimp'])}", "neutro"
    if p.get("eta_real"):
        return "CHEGOU", TITULO["CHEGOU"], f"chegou no BR em {dm(p['eta_real'])}", "neutro"
    if p.get("etd_real"):
        return ("TRANSITO", TITULO["TRANSITO"], f"deve chegar no BR em {dm(p['eta_prevista'])}" if p.get("eta_prevista")
                else "sem previsão de chegada", "neutro")
    if p.get("data_coleta"):
        return ("COLETADO", TITULO["COLETADO"], f"coletado em {dm(p['data_coleta'])} · "
                + (f"embarque previsto para {dm(p['etd_previsto'])}" if p.get("etd_previsto") else "sem previsão de embarque"),
                "neutro")
    return ("AGUARDANDO", TITULO["AGUARDANDO"], f"coleta programada para {dm(p['coleta_prevista'])}"
            if p.get("coleta_prevista") else "sem coleta programada", "neutro")


def com_status(processos):
    for p in processos:
        p["status"], p["status_titulo"], p["status_detalhe"], p["status_cor"] = status(p)
        p["status_curto"] = CURTO[p["status"]]
    return processos


def na_data(p, data_final):
    """O processo como estava na data final: datas posteriores são ignoradas (asOfDate)."""
    q = dict(p)
    for c in CAMPOS_DATA:
        if q.get(c) and q[c][:10] > data_final:
            q[c] = None
    if not q.get("data_parametrizacao"):
        q["canal"] = None  # o canal só existe a partir da parametrização
    if not q.get("canal") or q["canal"] == "Verde":
        q["exig_status"] = None
    return q


def ultima_data(p):
    datas = [p[c][:10] for c, _r in MARCOS if p.get(c)]
    return max(datas) if datas else None


def inicio(p):
    datas = [p[c][:10] for c in CAMPOS_DATA if p.get(c)] + ([p["criado_em"][:10]] if p.get("criado_em") else [])
    return min(datas) if datas else None


def atrasado(p, hoje):
    """ETA prevista vencida, ainda não chegou no Brasil e não foi entregue."""
    return bool(p.get("eta_prevista") and p["eta_prevista"] < hoje and not p.get("eta_real") and not p.get("entrega_real"))


def exigencia_pendente(p):
    """Canal amarelo/vermelho cuja exigência não está finalizada."""
    return p.get("canal") in ("Amarelo", "Vermelho") and p.get("exig_status") != "FINALIZADA" \
        and not p.get("data_desembaraco") and not p.get("entrega_real")


# ---------------------------------------------------------------------------
# Indicadores (computeStats)
# ---------------------------------------------------------------------------
def _media(valores):
    v = [x for x in valores if x is not None]
    return (sum(v) / len(v), len(v)) if v else (None, 0)


def _razao(sim, total):
    return (sim / total, total) if total else (None, 0)


INDICADORES = [
    # chave, rótulo, formato ('dias' | 'pct'), explicação
    ("atraso_chegada", "Atraso médio na chegada", "dias", "ETA real − ETA prevista (positivo = atrasou)"),
    ("atraso_embarque", "Atraso médio no embarque", "dias", "ETD real − ETD previsto"),
    ("transito", "Trânsito médio", "dias", "ETA real − ETD real"),
    ("dias_parametrizar", "Dias para parametrizar", "dias", "Parametrização − ETA real"),
    ("no_prazo", "Chegou no prazo", "pct", "ETA real ≤ ETA prevista"),
    ("pct_canal", "% em canal amarelo/vermelho", "pct", "entre os processos que já têm canal"),
    ("dias_retido", "Dias retido em canal", "dias", "Desembaraço − parametrização (canal amarelo/vermelho)"),
    ("desemb_entrega", "Dias do desembaraço até a entrega", "dias", "Entrega real − desembaraço"),
    ("atraso_entrega", "Atraso médio na entrega", "dias", "Entrega real − entrega prevista"),
]
ROTULO_IND = {k: r for k, r, _f, _e in INDICADORES}
FORMATO_IND = {k: f for k, _r, f, _e in INDICADORES}


def estatisticas(processos):
    """Indicadores de um conjunto. Cada valor é (valor, n de processos usados); só entram processos com as duas datas."""
    ps = processos
    e = {"total": len(ps), "entregues": sum(1 for p in ps if p.get("entrega_real"))}
    e["andamento"] = e["total"] - e["entregues"]
    e["atraso_chegada"] = _media([dias_entre(p.get("eta_prevista"), p.get("eta_real")) for p in ps])
    e["atraso_embarque"] = _media([dias_entre(p.get("etd_previsto"), p.get("etd_real")) for p in ps])
    e["transito"] = _media([dias_entre(p.get("etd_real"), p.get("eta_real")) for p in ps])
    e["dias_parametrizar"] = _media([dias_entre(p.get("eta_real"), p.get("data_parametrizacao")) for p in ps])
    com_eta = [p for p in ps if p.get("eta_real") and p.get("eta_prevista")]
    e["no_prazo"] = _razao(sum(1 for p in com_eta if p["eta_real"] <= p["eta_prevista"]), len(com_eta))
    com_canal = [p for p in ps if p.get("canal") in CANAIS]
    e["pct_canal"] = _razao(sum(1 for p in com_canal if p["canal"] != "Verde"), len(com_canal))
    e["dias_retido"] = _media([dias_entre(p.get("data_parametrizacao"), p.get("data_desembaraco"))
                               for p in ps if p.get("canal") in ("Amarelo", "Vermelho")])
    e["desemb_entrega"] = _media([dias_entre(p.get("data_desembaraco"), p.get("entrega_real")) for p in ps])
    e["atraso_entrega"] = _media([dias_entre(p.get("entrega_prevista"), p.get("entrega_real")) for p in ps])
    return e


def fmt_ind(chave, par):
    v = par[0] if isinstance(par, tuple) else par
    if v is None:
        return "—"
    if FORMATO_IND.get(chave) == "pct":
        return pct(v)
    return num(v, 1, " d")


def cor_prazo(v):
    if v is None:
        return "neutro"
    return "verde" if v >= 0.8 else ("amarelo" if v >= 0.5 else "vermelho")


DIMENSOES = {"fornecedor": "Por fornecedor", "agente": "Por agente de carga", "despachante": "Por despachante"}
SEM_GRUPO = {"fornecedor": "(sem fornecedor)", "agente": "(agente não informado)",
             "despachante": "(despachante não informado)"}
# indicadores dos gráficos de cada visão
GRAFICOS_DIMENSAO = {
    "fornecedor": ["atraso_chegada", "no_prazo", "transito", "dias_parametrizar", "pct_canal"],
    "despachante": ["dias_parametrizar", "dias_retido", "pct_canal", "desemb_entrega", "atraso_entrega"],
    "agente": ["atraso_embarque", "atraso_chegada", "no_prazo", "transito", "desemb_entrega"],
}
MAX_BARRAS = 12


def por_grupo(processos, dimensao):
    """[(nome, estatísticas)] ordenado pelo volume (maior primeiro)."""
    grupos = {}
    for p in processos:
        nome = (p.get(dimensao) or "").strip() or SEM_GRUPO[dimensao]
        grupos.setdefault(nome, []).append(p)
    res = [(n, estatisticas(ps)) for n, ps in grupos.items()]
    res.sort(key=lambda x: (-x[1]["total"], x[0].lower()))
    return res


def serie_grupo(grupos, chave):
    """Barras de um indicador por grupo (no máximo 12; grupos sem o dado ficam de fora). Rótulo "Nome (n)"."""
    linhas = []
    for nome, e in grupos:
        if chave == "total":
            linhas.append((f"{nome} ({e['total']})", e["total"]))
        elif e[chave][0] is not None:
            v = e[chave][0] * 100 if FORMATO_IND[chave] == "pct" else e[chave][0]
            linhas.append((f"{nome} ({e[chave][1]})", round(v, 1)))
    return linhas[:MAX_BARRAS]


def por_mes(processos, campo, meses=12, hoje=None):
    """Contagem de processos por mês da data (últimos 12 meses com dado)."""
    cont = {}
    for p in processos:
        d = _d(p.get(campo))
        if d:
            cont[(d.year, d.month)] = cont.get((d.year, d.month), 0) + 1
    chaves = sorted(cont)[-meses:]
    return [(f"{MESES[m - 1]}/{a % 100:02d}", cont[(a, m)]) for a, m in chaves]


def contagem_canal(processos):
    res = [(c, sum(1 for p in processos if p.get("canal") == c)) for c in CANAIS]
    res.append(("Sem canal", sum(1 for p in processos if p.get("canal") not in CANAIS)))
    return res


def contagem_status(processos):
    return [(TITULO[k], n) for k in ORDEM if (n := sum(1 for p in processos if p["status"] == k))]


def pontualidade(processos):
    """Chegadas com ETA prevista: (no prazo, atrasou)."""
    com = [p for p in processos if p.get("eta_real") and p.get("eta_prevista")]
    no = sum(1 for p in com if p["eta_real"] <= p["eta_prevista"])
    return [("No prazo", no), ("Atrasou", len(com) - no)]


def indicadores(processos, dimensao="fornecedor"):
    e = estatisticas(processos)
    grupos = por_grupo(processos, dimensao)
    graficos = [
        ("Processos por canal", contagem_canal(processos), "n"),
        ("Pontualidade na chegada ao Brasil", pontualidade(processos), "n"),
        ("Processos por situação", contagem_status(processos), "n"),
        ("Chegadas ao Brasil por mês", por_mes(processos, "eta_real"), "n"),
    ]
    largo = ("Entregas na Quinelato por mês", por_mes(processos, "entrega_real"), "n")
    titulo_dim = {"fornecedor": "fornecedor", "agente": "agente de carga", "despachante": "despachante"}[dimensao]
    graf_dim = [(f"Volume de processos por {titulo_dim}", serie_grupo(grupos, "total"), "n")]
    for k in GRAFICOS_DIMENSAO[dimensao]:
        graf_dim.append((f"{ROTULO_IND[k]} por {titulo_dim}", serie_grupo(grupos, k), FORMATO_IND[k]))
    return {"estat": e, "grupos": grupos, "graficos": graficos, "largo": largo, "graf_dim": graf_dim,
            "dimensao": dimensao, "titulo_dim": titulo_dim}


def cartoes_indicadores(e):
    """Os 10 cartões do topo: (rótulo, valor, cor, observação)."""
    def n(par):
        return par[1] if isinstance(par, tuple) else 0
    return [
        ("Processos no total", str(e["total"]), "neutro", ""),
        ("Entregues na Quinelato", str(e["entregues"]), "neutro", ""),
        ("Em andamento", str(e["andamento"]), "neutro", ""),
        ("Chegaram no prazo", fmt_ind("no_prazo", e["no_prazo"]), cor_prazo(e["no_prazo"][0]),
         f"{plural(n(e['no_prazo']), 'chegada', 'chegadas')} com ETA prevista"),
        ("Atraso médio na chegada", fmt_ind("atraso_chegada", e["atraso_chegada"]), "neutro",
         plural(n(e["atraso_chegada"]), "processo", "processos")),
        ("Trânsito médio", fmt_ind("transito", e["transito"]), "neutro", plural(n(e["transito"]), "processo", "processos")),
        ("Dias para parametrizar", fmt_ind("dias_parametrizar", e["dias_parametrizar"]), "neutro",
         plural(n(e["dias_parametrizar"]), "processo", "processos")),
        ("% em canal amarelo/vermelho", fmt_ind("pct_canal", e["pct_canal"]), "neutro",
         f"{plural(n(e['pct_canal']), 'processo', 'processos')} com canal"),
        ("Dias retido em canal", fmt_ind("dias_retido", e["dias_retido"]), "neutro",
         plural(n(e["dias_retido"]), "processo", "processos")),
        ("Dias do desembaraço até a entrega", fmt_ind("desemb_entrega", e["desemb_entrega"]), "neutro",
         plural(n(e["desemb_entrega"]), "processo", "processos")),
    ]


# ---------------------------------------------------------------------------
# A) Itens por situação
# ---------------------------------------------------------------------------
def eta_exibida(p):
    """A ETA real ou, se ainda não chegou, a prevista."""
    return p.get("eta_real") or p.get("eta_prevista")


def itens_por_situacao(processos, hoje):
    ativos = [p for p in processos if p["status"] != "ENTREGUE"]
    valores = {}
    for p in ativos:
        if p["status"] == "TRANSITO" and p.get("valor") is not None:
            valores[p["moeda"]] = round(valores.get(p["moeda"], 0) + p["valor"], 2)
    atrasados = [p for p in ativos if atrasado(p, hoje)]
    tabelas = []
    for k in ORDEM:
        ps = [p for p in ativos if p["status"] == k]
        if not ps:
            continue
        soma = {}
        for p in ps:
            for it in p["itens"]:
                chave = ((it.get("codigo") or "").strip(), (it.get("descricao") or "").strip())
                s = soma.setdefault(chave, {"codigo": chave[0], "descricao": chave[1], "quantidade": 0, "invoices": []})
                s["quantidade"] += it.get("quantidade") or 0
                if p["invoice"] not in s["invoices"]:
                    s["invoices"].append(p["invoice"])
        linhas = sorted(soma.values(), key=lambda s: (s["codigo"] or "~", s["descricao"]))
        tabelas.append({"status": k, "titulo": TITULO[k], "processos": len(ps), "linhas": linhas,
                        "sem_itens": sum(1 for p in ps if not p["itens"])})
    retidos = [{"processo": p, "itens": p["itens"]} for p in ativos if p["status"] in RETIDOS]
    return {
        "ativos": ativos, "em_transito": sum(1 for p in ativos if p["status"] == "TRANSITO"),
        "atrasados": len(atrasados), "no_prazo": len(ativos) - len(atrasados), "valor_transito": valores,
        "graf_status": contagem_status(ativos), "graf_canal": contagem_canal(ativos), "tabelas": tabelas,
        "retidos": retidos,
    }


def linhas_visao_geral(processos, hoje):
    """Excel aba "Visao Geral Processos": um processo não entregue por linha."""
    return [[p["invoice"], p.get("duimp") or "", p["fornecedor"], p.get("pais") or "", p["status_titulo"],
             p.get("canal") or "", dma(eta_exibida(p)) if eta_exibida(p) else "",
             "Sim" if atrasado(p, hoje) else "Não", "Sim" if exigencia_pendente(p) else "Não"]
            for p in ordenar(processos) if p["status"] != "ENTREGUE"]


CAB_VISAO_GERAL = ["Invoice", "DUIMP", "Fornecedor", "País", "Status", "Canal", "ETA", "Atrasado", "Exigência pendente"]
CAB_ITENS = ["Código", "Descrição", "Quantidade", "Status", "Canal", "Fornecedor", "Invoice", "DUIMP", "ETA"]


def linhas_itens(processos):
    """Uma linha por item dos processos não entregues (o item herda status e canal do processo)."""
    res = []
    for p in ordenar(processos):
        if p["status"] == "ENTREGUE":
            continue
        for it in p["itens"]:
            res.append([it.get("codigo") or "", it.get("descricao") or "", it.get("quantidade"), p["status_titulo"],
                        p.get("canal") or "", p["fornecedor"], p["invoice"], p.get("duimp") or "",
                        dma(eta_exibida(p)) if eta_exibida(p) else ""])
    return res


def ordenar(processos):
    return sorted(processos, key=lambda p: (ORDEM[p["status"]], p["fornecedor"].lower(), p["invoice"]))


# ---------------------------------------------------------------------------
# B) Importações por canal
# ---------------------------------------------------------------------------
TIPOS_CANAL = {
    "amarelo": (["Amarelo"], "IMPORTAÇÕES PARAMETRIZADAS EM CANAL AMARELO"),
    "amarelo_vermelho": (["Amarelo", "Vermelho"], "IMPORTAÇÕES PARAMETRIZADAS EM CANAL AMARELO E VERMELHO"),
    "vermelho": (["Vermelho"], "IMPORTAÇÕES PARAMETRIZADAS EM CANAL VERMELHO"),
    "verde": (["Verde"], "IMPORTAÇÕES PARAMETRIZADAS EM CANAL VERDE"),
    "todos": (CANAIS, "IMPORTAÇÕES PARAMETRIZADAS"),
}


def importacoes_por_canal(processos, tipo, de, ate):
    canais, titulo = TIPOS_CANAL.get(tipo, TIPOS_CANAL["amarelo"])
    ps = [p for p in processos if p.get("fornecedor") and p.get("data_parametrizacao")
          and de <= p["data_parametrizacao"][:10] <= ate and p.get("canal") in canais]
    ps.sort(key=lambda p: (p["data_parametrizacao"], p["fornecedor"].lower(), p["invoice"]))
    mostra_canal = len(canais) > 1
    cab = ["SHIPPER", "INVOICE", "DATA REGISTRO"] + (["CANAL"] if mostra_canal else []) + ["STATUS"]
    linhas = [[p["fornecedor"], p["invoice"], dia_mes_abrev(p["data_parametrizacao"])]
              + ([p["canal"].upper()] if mostra_canal else []) + [p["status_curto"]] for p in ps]
    return {"titulo": titulo, "cab": cab, "linhas": linhas, "processos": ps, "mostra_canal": mostra_canal}


# ---------------------------------------------------------------------------
# C) Relatório mensal (buildMonthly)
# ---------------------------------------------------------------------------
def qtd_itens(p):
    return sum((it.get("quantidade") or 0) for it in p["itens"])


def relatorio_mensal(processos, de, ate):
    eventos = []
    for p in processos:
        for campo, rotulo in MARCOS:
            v = (p.get(campo) or "")[:10]
            if v and de <= v <= ate:
                eventos.append({"data": v, "evento": rotulo, "processo": p,
                                "detalhe": f"canal {p['canal'].lower()}" if campo == "data_parametrizacao" and p.get("canal") else ""})
    eventos.sort(key=lambda e: (e["data"], [c for c, _r in MARCOS].index(next(c for c, r in MARCOS if r == e["evento"])),
                                e["processo"]["fornecedor"].lower()))
    contagem_eventos = [(r, sum(1 for e in eventos if e["evento"] == r)) for _c, r in MARCOS]

    def no_periodo(p, campo):
        v = (p.get(campo) or "")[:10]
        return bool(v) and de <= v <= ate

    chegadas = sorted([p for p in processos if no_periodo(p, "eta_real")], key=lambda p: p["eta_real"])
    entregas = sorted([p for p in processos if no_periodo(p, "entrega_real")], key=lambda p: p["entrega_real"])
    params = sorted([p for p in processos if no_periodo(p, "data_parametrizacao")], key=lambda p: p["data_parametrizacao"])
    param_canal = [(c, sum(1 for p in params if p.get("canal") == c)) for c in CANAIS] + \
        [("Sem canal", sum(1 for p in params if p.get("canal") not in CANAIS))]
    pont = {"Atrasou": 0, "No prazo": 0, "Adiantou": 0}
    for p in chegadas:
        dif = dias_entre(p.get("eta_prevista"), p["eta_real"])
        if dif is None:
            continue
        pont["Atrasou" if dif > 0 else ("No prazo" if dif == 0 else "Adiantou")] += 1
    pont["Sem ETA prevista"] = sum(1 for p in chegadas if not p.get("eta_prevista"))

    # processos em aberto no fim do período (como estavam na data final)
    abertos = []
    for p in processos:
        ini = inicio(p)
        if not ini or ini > ate:
            continue  # ainda não existia
        q = com_status([na_data(p, ate)])[0]
        if q["status"] == "ENTREGUE":
            continue
        ult = ultima_data(q)
        q["ultima_mov"] = ult
        q["ultima_mov_txt"] = next((f"{r} em {dma(q[c])}" for c, r in reversed(MARCOS) if q.get(c) and q[c][:10] == ult),
                                   "Cadastro do processo") if ult else "Cadastro do processo"
        base = ult or (p.get("criado_em") or ini)[:10]
        q["dias_parado"] = max(dias_entre(base, ate) or 0, 0)
        q["herdada"] = ini < de
        abertos.append(q)
    abertos.sort(key=lambda q: (ORDEM[q["status"]], -q["dias_parado"], q["fornecedor"].lower()))
    transito_fim = [q for q in abertos if q["status"] == "TRANSITO"]

    def pecas(lista):
        com = [p for p in lista if p["itens"]]
        return {"pecas": sum(qtd_itens(p) for p in com), "processos": len(lista), "sem_itens": len(lista) - len(com)}

    return {
        "de": de, "ate": ate, "eventos": eventos, "contagem_eventos": contagem_eventos, "chegadas": chegadas,
        "entregas": entregas, "parametrizacoes": params, "param_canal": param_canal, "pontualidade": list(pont.items()),
        "pecas": {"Chegaram no Brasil": pecas(chegadas), "Entregues na Quinelato": pecas(entregas),
                  "Em trânsito no fim do período": pecas(transito_fim)},
        "abertos": abertos, "herdadas": [q for q in abertos if q["herdada"]],
        "status_abertos": [(TITULO[k], n) for k in ORDEM if (n := sum(1 for q in abertos if q["status"] == k))],
    }


# ---------------------------------------------------------------------------
# D) Resumo semanal
# ---------------------------------------------------------------------------
def _comparar(previsto, real, hoje, nome):
    """Texto de previsto × real: "Atraso de X dias" / "No prazo" / "Adiantado" / "<nome> atrasado há X dias"."""
    if real and previsto:
        dif = dias_entre(previsto, real)
        if dif > 0:
            return f"Atraso de {plural(dif, 'dia', 'dias')}"
        if dif == 0:
            return "No prazo"
        return f"Adiantado {plural(-dif, 'dia', 'dias')}"
    if previsto and not real and previsto < hoje:
        return f"{nome} atrasado há {plural(dias_entre(previsto, hoje), 'dia', 'dias')}"
    return ""


def _juntar(*partes):
    return " · ".join(x for x in partes if x)


def resumo_processo(p, hoje):
    """[(etapa, texto)] — só com o que existe no processo."""
    linhas = []
    if p.get("coleta_prevista") or p.get("data_coleta"):
        linhas.append(("Coleta", _juntar(p.get("coleta_prevista") and f"programada para {dma(p['coleta_prevista'])}",
                                         p.get("data_coleta") and f"realizada em {dma(p['data_coleta'])}",
                                         not p.get("data_coleta") and p.get("coleta_prevista") and p["coleta_prevista"] < hoje
                                         and f"Coleta atrasada há {plural(dias_entre(p['coleta_prevista'], hoje), 'dia', 'dias')}")))
    if p.get("etd_previsto") or p.get("etd_real"):
        linhas.append(("Embarque", _juntar(p.get("etd_previsto") and f"ETD previsto {dma(p['etd_previsto'])}",
                                           p.get("etd_real") and f"ETD real {dma(p['etd_real'])}",
                                           _comparar(p.get("etd_previsto"), p.get("etd_real"), hoje, "Embarque"))))
    if p.get("hawb"):
        linhas.append(("Emissão da HAWB", f"emitida em {dma(p['hawb'])}"))
    elif p.get("etd_real"):
        linhas.append(("Emissão da HAWB", "HAWB ainda não informada"))
    if p.get("eta_prevista") or p.get("eta_real"):
        linhas.append(("Chegada no Brasil", _juntar(p.get("eta_prevista") and f"ETA prevista {dma(p['eta_prevista'])}",
                                                    p.get("eta_real") and f"ETA real {dma(p['eta_real'])}",
                                                    _comparar(p.get("eta_prevista"), p.get("eta_real"), hoje, "Chegada"))))
    if p.get("data_duimp"):
        linhas.append(("DUIMP", _juntar(p.get("duimp") and f"nº {p['duimp']}", f"emitida em {dma(p['data_duimp'])}")))
    if p.get("data_parametrizacao"):
        dif = dias_entre(p.get("eta_real"), p["data_parametrizacao"])
        linhas.append(("Parametrização", _juntar(f"em {dma(p['data_parametrizacao'])}",
                                                 dif is not None and f"{plural(dif, 'dia', 'dias')} após a chegada",
                                                 p.get("canal") and f"canal {p['canal'].lower()}")))
    elif p.get("eta_real"):
        linhas.append(("Parametrização", "ainda não foi parametrizado"))
    if p.get("canal") in ("Amarelo", "Vermelho") and p.get("data_parametrizacao"):
        if p.get("data_desembaraco"):
            texto = f"{plural(dias_entre(p['data_parametrizacao'], p['data_desembaraco']), 'dia', 'dias')} em canal {p['canal'].lower()} até o desembaraço"
        else:
            texto = f"em andamento: {plural(dias_entre(p['data_parametrizacao'], hoje), 'dia', 'dias')} em canal {p['canal'].lower()}"
        linhas.append((f"Tempo em canal {p['canal'].lower()}", texto))
        if p.get("exig_status") or p.get("exig_descricao") or p.get("exig_acao"):
            st = {"PENDENTE": "pendente", "CUMPRIDA": "cumprida, aguardando análise",
                  "FINALIZADA": "finalizada"}.get(p.get("exig_status"), "")
            prazo = ""
            if p.get("exig_prazo"):
                prazo = f"prazo de resposta {dma(p['exig_prazo'])}"
                if p.get("exig_status") == "PENDENTE" and p["exig_prazo"] < hoje:
                    prazo += f" (vencido há {plural(dias_entre(p['exig_prazo'], hoje), 'dia', 'dias')})"
            linhas.append(("Exigência do fiscal", _juntar(p.get("exig_descricao"), p.get("exig_acao") and f"ação tomada: {p['exig_acao']}",
                                                          st and f"situação: {st}", prazo)))
        else:
            linhas.append(("Exigência do fiscal", "Sem exigência formal do fiscal — apenas análise do processo"))
    if p.get("data_desembaraco"):
        linhas.append(("Desembaraço", f"em {dma(p['data_desembaraco'])}"))
    if p.get("entrega_prevista") or p.get("entrega_real"):
        venc = ""
        if p.get("entrega_prevista") and not p.get("entrega_real") and p["entrega_prevista"] < hoje:
            venc = f"Previsão vencida há {plural(dias_entre(p['entrega_prevista'], hoje), 'dia', 'dias')}"
        linhas.append(("Entrega na Quinelato", _juntar(p.get("entrega_prevista") and f"prevista para {dma(p['entrega_prevista'])}",
                                                       p.get("entrega_real") and f"entregue em {dma(p['entrega_real'])}",
                                                       _comparar(p.get("entrega_prevista"), p.get("entrega_real"), hoje, "")
                                                       if p.get("entrega_real") else "", venc)))
    return linhas


CAB_SEMANAL = ["Fornecedor", "País", "Invoice", "DUIMP", "Agente", "Despachante", "Consolidação", "Status", "Embarque",
               "Chegada", "Parametrização", "Canal/desembaraço", "Exigência", "Entrega"]


def linha_semanal(p, hoje):
    r = dict(resumo_processo(p, hoje))
    canal = " · ".join(x for x in (r.get(f"Tempo em canal {(p.get('canal') or '').lower()}"), r.get("Desembaraço") and
                                   "desembaraço " + r["Desembaraço"]) if x)
    return [p["fornecedor"], p.get("pais") or "", p["invoice"], p.get("duimp") or "", p.get("agente") or "",
            p.get("despachante") or "", p.get("consolidacao") or "", p["status_titulo"],
            _juntar(r.get("Coleta") and "coleta " + r["Coleta"], r.get("Embarque")), r.get("Chegada no Brasil", ""),
            r.get("Parametrização", ""), canal, r.get("Exigência do fiscal", ""), r.get("Entrega na Quinelato", "")]


# ---------------------------------------------------------------------------
# Alertas (Painel)
# ---------------------------------------------------------------------------
DIAS_CANAL_ALERTA = 5
DIAS_SEM_ATUALIZACAO = 7


def alertas(processos, hoje):
    """[(processo, alerta, desde)] dos processos não entregues."""
    res = []
    for p in processos:
        if p["status"] == "ENTREGUE":
            continue
        if atrasado(p, hoje):
            res.append((p, "ETA vencida", f"prevista para {dma(p['eta_prevista'])}"))
        if p.get("exig_status") == "PENDENTE" and p.get("exig_prazo") and p["exig_prazo"] < hoje \
                and not p.get("data_desembaraco"):
            res.append((p, "Prazo de resposta da exigência vencido", f"venceu em {dma(p['exig_prazo'])}"))
        if p.get("canal") in ("Amarelo", "Vermelho") and p.get("data_parametrizacao") and not p.get("data_desembaraco"):
            dias = dias_entre(p["data_parametrizacao"], hoje)
            if dias is not None and dias >= DIAS_CANAL_ALERTA:
                res.append((p, f"Parado em canal {p['canal'].lower()} há {dias} dias",
                            f"parametrizado em {dma(p['data_parametrizacao'])}"))
        if p.get("entrega_prevista") and p["entrega_prevista"] < hoje and not p.get("entrega_real"):
            res.append((p, "Entrega prevista vencida", f"prevista para {dma(p['entrega_prevista'])}"))
        if p.get("etd_real") and not p.get("hawb"):
            res.append((p, "HAWB não preenchida", f"embarcou em {dma(p['etd_real'])}"))
        ult = p.get("ultima_atualizacao")
        if ult:
            dias = dias_entre(ult[:10], hoje)
            if dias is not None and dias >= DIAS_SEM_ATUALIZACAO:
                res.append((p, f"Sem atualização há {dias} dias", f"última em {dma(ult)}"))
    return res
