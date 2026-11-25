"""Inspeção de recebimento (extração do Agregador MEGA) e tradução dos defeitos por termo.

Funções puras (sem banco): leitura da planilha, normalização dos termos e montagem do texto em inglês.
O app.py grava os eventos e o glossário.
"""
import re
import unicodedata
from datetime import date, datetime

# critério do MEGA -> categoria de qualidade
CRITERIO_CATEGORIA = {"15": "A", "16": "B", "17": "C", "18": "D"}
CATEGORIAS = ["A", "B", "C", "D"]

# cabeçalho da planilha -> campo do evento (comparação sem acento, maiúsculas, espaços e _ ignorados)
COLUNAS = {
    "NUMERONF": "numero_nf", "DATANF": "data_nf", "CODIGO": "codigo_mega", "SUPPLIER": "fornecedor_excel",
    "BU": "bu", "PRODUCT": "produto", "SEQ": "seq", "DESCRICAO": "descricao", "GRUPOESTOQUE": "grupo_estoque",
    "QTDEPEDIDA": "qtde_pedida", "QUANTITY": "quantidade", "CRITERIO": "criterio", "CATEGORY": "categoria",
    "COMMENTARY": "comentario", "DATAINCLUSAO": "data_inclusao", "NOMEUSUARIOINCLUSAO": "usuario_inclusao",
    "DATAINSPECAO": "data_inspecao", "CODUSUARIOINSPECAO": "cod_inspetor", "NOMEUSUARIOINSPECAO": "inspetor",
    "REPORTDATE": "report_date", "ITEMINPECIONADO": "item_inspecionado", "ITEMINSPECIONADO": "item_inspecionado",
    "DESCRSTATUSLOTE": "status_lote", "QTDENF": "qtde_nf", "VLRUNITARIONF": "vlr_unit_nf", "VLRITEMNF": "vlr_item_nf",
    "INVOICE": "invoice_excel", "LOTE": "lote", "AREA": "area", "FAMILIA": "familia", "FAMILY": "family",
}
OBRIGATORIAS = ["numero_nf", "produto", "quantidade", "comentario"]
CAMPOS_EVENTO = list(dict.fromkeys(COLUNAS.values()))


def _chave_coluna(nome):
    t = unicodedata.normalize("NFKD", str(nome or "")).encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z]", "", t)


def _valor(v):
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S") if (v.hour or v.minute or v.second) else v.strftime("%Y-%m-%d")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def ler_planilha(arquivo):
    """Lê a aba de eventos (a primeira que tiver as colunas obrigatórias) e, se existir, a aba de traduções
    ("Tradução Ações": TEXTO_PORTUGUES / TEXTO_INGLES). Devolve (eventos, traducoes, avisos)."""
    from openpyxl import load_workbook
    wb = load_workbook(arquivo, read_only=True, data_only=True)
    eventos, traducoes, avisos = [], [], []
    aba_eventos = None
    for ws in wb.worksheets:
        linhas = ws.iter_rows(values_only=True)
        try:
            cab = next(linhas)
        except StopIteration:
            continue
        chaves = [_chave_coluna(c) for c in cab]
        if "TEXTOPORTUGUES" in chaves and "TEXTOINGLES" in chaves:
            ip, ie = chaves.index("TEXTOPORTUGUES"), chaves.index("TEXTOINGLES")
            for r in linhas:
                if r and len(r) > max(ip, ie) and r[ip] and r[ie]:
                    traducoes.append((str(r[ip]), str(r[ie])))
            continue
        mapa = {i: COLUNAS[k] for i, k in enumerate(chaves) if k in COLUNAS}
        if aba_eventos is None and all(c in mapa.values() for c in OBRIGATORIAS):
            aba_eventos = ws.title
            for n, r in enumerate(linhas, start=2):
                if not r or not any(v is not None for v in r):
                    continue
                ev = {c: None for c in CAMPOS_EVENTO}
                for i, campo in mapa.items():
                    if i < len(r) and ev.get(campo) is None:
                        ev[campo] = _valor(r[i])
                ev["linha"] = n
                eventos.append(ev)
    wb.close()
    if aba_eventos is None:
        avisos.append("Não encontrei a aba de eventos: são necessárias as colunas NUMERO_NF, PRODUCT, QUANTITY e "
                      "COMMENTARY.")
    return eventos, traducoes, avisos, aba_eventos


def categoria_do_criterio(criterio):
    c = str(criterio or "").strip().lstrip("0")
    return CRITERIO_CATEGORIA.get(c)


# ---------------------------------------------------------------------------
# Tradução por termo
# O comentário do MEGA junta vários defeitos ("ACABAMENTO / AFIAÇÃO;"). Cada parte separada por "/" ou ";" é um
# termo, traduzido uma única vez no glossário; o texto em inglês junta as traduções com " / " (formato atual).
# ---------------------------------------------------------------------------
SEM_DEFEITO = {"", "OK"}


def chave_termo(texto):
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().upper()
    t = t.replace(".", " ")
    t = re.sub(r"\s+", " ", t).strip(" ;-,:")
    return t


def termos(comentario):
    """[(texto original da parte, chave)] do comentário, sem as partes vazias."""
    partes = []
    for p in re.split(r"[/;]+", str(comentario or "")):
        k = chave_termo(p)
        if k:
            partes.append((p.strip(" ;"), k))
    return partes


def traduzir(comentario, glossario):
    """(texto em inglês, chaves sem tradução). Comentário vazio, ';' ou 'OK' vira 'Ok'."""
    partes = termos(comentario)
    if not partes or all(k in SEM_DEFEITO for _t, k in partes):
        return "Ok", []
    saida, faltando = [], []
    for _t, k in partes:
        if k in SEM_DEFEITO:
            continue
        en = glossario.get(k)
        if en:
            saida.append(en)
        else:
            faltando.append(k)
    return (" / ".join(saida) if not faltando else None), faltando


def termos_das_traducoes(pares):
    """Glossário inicial a partir do cadastro antigo (frase inteira PT -> EN): só quando as duas frases têm o
    mesmo número de partes. Em caso de traduções diferentes para o mesmo termo, fica a mais frequente."""
    from collections import Counter
    votos = {}
    for pt, en in pares:
        a = [k for _t, k in termos(pt)]
        b = [p.strip() for p in re.split(r"[/;]+", str(en)) if p.strip()]
        if not a or len(a) != len(b):
            continue
        for k, e in zip(a, b):
            if k in SEM_DEFEITO:
                continue
            votos.setdefault(k, Counter())[e] += 1
    return {k: c.most_common(1)[0][0] for k, c in votos.items()}


def numero_invoice_excel(txt):
    """'26000069 - DR.FRIGZ' -> '26000069' (o Excel acrescenta o nome do fornecedor no fim)."""
    t = str(txt or "").strip()
    return re.split(r"\s+-\s+", t)[0].strip() if t else ""
