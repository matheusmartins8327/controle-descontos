"""Leitura do extrato da DUIMP (PDF gerado pelo Portal Único Siscomex).

Duas fontes dentro do mesmo PDF:
- partes padrão do Siscomex (iguais para qualquer despachante): número da DUIMP, data de registro, itens (valor na
  condição de venda) e, no extrato completo, o "Demonstrativo dos Débitos da Duimp" (II, IPI, PIS, COFINS e Taxa de
  Utilização do Siscomex);
- "Informações Complementares", texto livre escrito pelo despachante: taxa de câmbio, FOB em R$, frete, valor
  aduaneiro/CIF e ICMS. Cada despachante escreve de um jeito: os padrões abaixo cobrem os modelos já vistos
  (M3 e Fischer/FER) e o que não for encontrado fica em branco para a Importação preencher na conferência.

Nada aqui grava no banco: a função devolve um dicionário para a tela de conferência.
"""
import re

CAMPOS = ["numero", "data_registro", "moeda", "taxa_cambio", "fob_moeda", "fob_brl", "frete_brl", "valor_aduaneiro",
          "ii", "ipi", "pis", "cofins", "icms", "taxa_siscomex", "multa"]

MOEDAS_TEXTO = {"US$": "USD", "USD": "USD", "DOLAR": "USD", "DÓLAR": "USD", "EUR": "EUR", "EURO": "EUR", "€": "EUR",
                "CHF": "CHF", "FRANCO": "CHF"}


def numero_br(txt):
    """'1.234,56' -> 1234.56 ; '5,46210' -> 5.4621 ; '192,79' -> 192.79"""
    if txt is None:
        return None
    t = txt.strip().replace(" ", "")
    if not t:
        return None
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


VALOR = r"([\d.]+,\d+)"


def _primeiro(padroes, texto, flags=re.I):
    for p in padroes:
        m = re.search(p, texto, flags)
        if m:
            v = numero_br(m.group(1))
            if v is not None:
                return v
    return None


def texto_do_pdf(caminho):
    from pypdf import PdfReader  # importado aqui: só é preciso quando há uma DUIMP para ler
    return "\n".join((p.extract_text() or "") for p in PdfReader(caminho).pages)


def ler_texto(texto):
    """Extrai os campos da DUIMP do texto do PDF. Devolve (dados, avisos)."""
    d = {c: None for c in CAMPOS}
    avisos = []
    t = texto.replace("\r", "")

    m = re.search(r"Extrato da Duimp\s+(\d{2}BR\d{10}-\d)", t, re.I)
    d["numero"] = m.group(1) if m else None

    # data de registro: demonstrativo ("Data de Registro: 17/08/2026 16:41") ou histórico ("Declaração registrada")
    m = re.search(r"Data de Registro:?\s*(?:\S+\s+\S+\s+)?(\d{2})/(\d{2})/(\d{4})", t, re.I) or \
        re.search(r"(\d{2})/(\d{2})/(\d{4}),?\s*\n?\s*\d{2}:\d{2}\s*\n?\s*Declara\S+ registrada", t, re.I)
    if m:
        d["data_registro"] = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"

    # moeda negociada (itens)
    m = re.search(r"Moeda negociada:.*?\n?\s*(DOLAR DOS EUA|DÓLAR DOS EUA|EURO|FRANCO SUI[ÇC]O)", t, re.I | re.S)
    if m:
        nome = m.group(1).upper()
        d["moeda"] = "USD" if "DOLAR" in nome or "DÓLAR" in nome else ("EUR" if "EURO" in nome else "CHF")

    # FOB na moeda: soma dos itens (parte padrão do Siscomex)
    itens = [numero_br(v) for v in re.findall(r"Valor total na condi\S+ de venda:\s*\n?\s*" + VALOR, t, re.I)]
    itens = [v for v in itens if v is not None]
    if itens:
        d["fob_moeda"] = round(sum(itens), 2)

    # demonstrativo dos débitos (extrato completo): a última coluna é o valor recolhido
    demo = re.search(r"Demonstrativo dos D\S+bitos(.*?)(?:Total Geral|$)", t, re.I | re.S)
    if demo:
        bloco = demo.group(1)
        for campo, rotulo in (("ii", r"II"), ("ipi", r"IPI"), ("pis", r"PIS"), ("cofins", r"Cofins"),
                              ("taxa_siscomex", r"Taxa de Utiliza\S+")):
            m = re.search(r"^\s*" + rotulo + r"\b([^\n]*)", bloco, re.I | re.M)
            if m:
                valores = re.findall(r"R\$\s*" + VALOR, m.group(1))
                if valores:
                    d[campo] = numero_br(valores[-1])
    else:
        avisos.append("Esta é a versão resumida do extrato (sem o \"Demonstrativo dos Débitos\"): os tributos foram "
                      "lidos do texto do despachante. Prefira anexar o extrato completo da DUIMP.")

    # texto do despachante (resumo dos tributos), usado quando o demonstrativo não trouxe o valor
    texto_livre = t[:demo.start()] if demo else t
    for campo, rotulos in (("ii", [r"II"]), ("ipi", [r"IPI"]), ("pis", [r"PIS/PASEP", r"PIS"]),
                           ("cofins", [r"COFINS"])):
        if d[campo] is None:
            d[campo] = _primeiro([r"^\s*" + r + r"\s+R\$\s*" + VALOR for r in rotulos], texto_livre, re.I | re.M)
    if d["taxa_siscomex"] is None:
        d["taxa_siscomex"] = _primeiro([r"TAXA SISCOMEX\s*:?\s*R?\$?\s*" + VALOR, r"^\s*SISCOMEX\s*:?\s*R?\$?\s*" + VALOR],
                                       texto_livre, re.I | re.M)
    d["icms"] = _primeiro([r"ICMS A RECOLHER\s*:?\s*R\$\s*" + VALOR, r"VALOR TOTAL ICMS\s*:?\s*R\$\s*" + VALOR,
                           r"^\s*ICMS\s+R\$\s*" + VALOR], texto_livre, re.I | re.M)
    d["multa"] = _primeiro([r"MULTA[^\n]*?R\$\s*" + VALOR], texto_livre)

    # taxa de câmbio
    d["taxa_cambio"] = _primeiro([r"TAXA MOEDA FOB\s*:?\s*[A-Z$€]*\s*" + r"([\d.]*,\d+)",
                                  r"TAXA\(S\) UTILIZADA\(S\)\s*:?\s*\n?\s*[A-Z$€]+\s*:?\s*([\d.]*,\d+)",
                                  r"TAXA (?:DO )?D[OÓ]LAR\s*:?\s*[A-Z$€]*\s*([\d.]*,\d+)",
                                  r"TAXA (?:DO )?EURO\s*:?\s*[A-Z$€]*\s*([\d.]*,\d+)"], texto_livre)

    # FOB em R$ e frete
    m = re.search(r"MERCADORIA\s*:?\s*(?:US\$|USD|EUR|€|CHF)\s*" + VALOR + r"\s*R\$\s*" + VALOR, texto_livre, re.I)
    if m:
        d["fob_brl"] = numero_br(m.group(2))
        if d["fob_moeda"] is None:
            d["fob_moeda"] = numero_br(m.group(1))
    if d["fob_brl"] is None:
        d["fob_brl"] = _primeiro([r"TOTAL MLE\s*R\$\s*" + VALOR, r"VMLE\s*:?\s*R\$\s*" + VALOR], texto_livre)
    if d["fob_moeda"] is None:
        d["fob_moeda"] = _primeiro([r"TOTAL MLE\s*(?:US\$|USD|EUR|€|CHF)\s*" + VALOR], texto_livre)
    d["valor_aduaneiro"] = _primeiro([r"VALOR ADUANEIRO\s*:?\s*R\$\s*" + VALOR, r"VALOR CIF\s*:?\s*R\$\s*" + VALOR],
                                     texto_livre)
    m = re.search(r"FRETE\s*:?\s*(?:US\$|USD|EUR|€|CHF)\s*" + VALOR + r"\s*R\$\s*" + VALOR, texto_livre, re.I)
    if m:
        d["frete_brl"] = numero_br(m.group(2))
    elif d["valor_aduaneiro"] is not None and d["fob_brl"] is not None and d["valor_aduaneiro"] > d["fob_brl"]:
        d["frete_brl"] = round(d["valor_aduaneiro"] - d["fob_brl"], 2)
        avisos.append("Frete internacional calculado como valor aduaneiro (CIF) − FOB em R$ (inclui o seguro, se houver).")

    if d["taxa_cambio"] is None and d["fob_brl"] and d["fob_moeda"]:
        d["taxa_cambio"] = round(d["fob_brl"] / d["fob_moeda"], 6)
        avisos.append("Taxa de câmbio calculada pela divisão FOB em R$ ÷ FOB na moeda (não estava escrita na DUIMP).")
    if d["fob_brl"] is None and d["taxa_cambio"] and d["fob_moeda"]:
        d["fob_brl"] = round(d["fob_moeda"] * d["taxa_cambio"], 2)
    # conferência interna: FOB na moeda × taxa ≈ FOB em R$
    if d["taxa_cambio"] and d["fob_moeda"] and d["fob_brl"]:
        if abs(d["fob_moeda"] * d["taxa_cambio"] - d["fob_brl"]) > max(1.0, d["fob_brl"] * 0.002):
            avisos.append("Atenção: FOB na moeda × taxa de câmbio não bate com o FOB em R$ da DUIMP. Confira os valores.")
    return d, avisos


def ler_pdf(caminho):
    texto = texto_do_pdf(caminho)
    if not texto.strip():
        return {c: None for c in CAMPOS}, ["Não foi possível ler o texto do PDF (pode ser uma imagem escaneada)."]
    if "duimp" not in texto.lower():
        return {c: None for c in CAMPOS}, ["O arquivo não parece ser um extrato da DUIMP."]
    return ler_texto(texto)
