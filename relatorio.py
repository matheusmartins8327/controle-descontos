"""Relatório de inspeção ao fornecedor (Excel em inglês) e corpo do e-mail (HTML compatível com Outlook).

Funções puras: recebem os dados já calculados pelo app.py (versão do desconto, eventos com o defeito em inglês e os
itens do invoice) e devolvem o arquivo / o HTML. O fornecedor recebe só a categoria final de cada evento.
"""
import io
from collections import Counter, OrderedDict
from html import escape

CORES = {"A": "00C853", "B": "F2C200", "C": "E67E22", "D": "C0141C"}  # mesmas cores do Power BI
CORES_CLARAS = {"A": "D4F0DC", "B": "FFF3C4", "C": "FFE0C2", "D": "FBD5D5"}
LEGENDA = [("A", "The product is approved with no issues"), ("B", "There is a need for minor rework"),
           ("C", "There is a need for major rework"), ("D", "The item is rejected")]


def pct_br(v, casas=1):
    return f"{v * 100:.{casas}f}%".replace(".", ",")


def num_br(v, casas=0):
    """Quantidades sem separador de milhar (como no e-mail atual: "4675 units"); valores com 2 casas, com separador."""
    if casas == 0:
        return f"{v:.0f}"
    s = f"{v:,.{casas}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def defeitos(texto_en):
    """Defeitos de um evento ("Finish / Sharpening" -> ["Finish", "Sharpening"]); "Ok" não é defeito."""
    return [p.strip() for p in str(texto_en or "").split("/") if p.strip() and p.strip().lower() != "ok"]


def resumo(eventos):
    """Totais por categoria, por item e matriz defeito × item."""
    tot = Counter()
    por_item = OrderedDict()
    matriz = {}
    for e in eventos:
        q = e["quantidade"] or 0
        tot[e["categoria"]] += q
        it = por_item.setdefault(e["produto"], Counter())
        it[e["categoria"]] += q
        for d in defeitos(e["en"]):
            matriz.setdefault(d, Counter())[e["produto"]] += q
    return tot, por_item, matriz


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------
def gerar_excel(info, linhas, eventos, itens_fob):
    """info: fornecedor, invoice, moeda, versao, data. linhas: itens da versão do desconto."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    negrito = Font(bold=True)
    cab = PatternFill("solid", fgColor="1F3864")
    fonte_cab = Font(bold=True, color="FFFFFF")
    fino = Side(style="thin", color="D0D7E5")
    borda = Border(left=fino, right=fino, top=fino, bottom=fino)
    moeda = info["moeda"]
    fmt_moeda = '#,##0.00'
    rotulo_inv = f"{info['invoice']} - {info['fornecedor']}"

    def cabecalho(ws, linha, titulos):
        for i, t in enumerate(titulos, start=1):
            c = ws.cell(row=linha, column=i, value=t)
            c.fill, c.font, c.border = cab, fonte_cab, borda
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    # --- Inspection Results (item -> defeito, como a tabela dinâmica) ---
    ws = wb.active
    ws.title = "Inspection Results"
    ws["A1"], ws["B1"] = "SUPPLIER", info["fornecedor"]
    ws["A2"], ws["B2"] = "INVOICE", rotulo_inv
    if info["versao"] > 1:
        ws["A3"], ws["B3"] = "REVISION", info["versao"]
    for c in ("A1", "A2", "A3"):
        ws[c].font = negrito
    cabecalho(ws, 5, ["Item / Defect", "A", "B", "C", "D", "Total"])
    lin = 6
    tot, por_item, _m = resumo(eventos)
    por_item_def = OrderedDict()
    for e in eventos:
        nomes = defeitos(e["en"]) or ["Ok"]
        alvo = por_item_def.setdefault(e["produto"], OrderedDict())
        chave = " / ".join(nomes)
        alvo.setdefault(chave, Counter())[e["categoria"]] += e["quantidade"] or 0
    for produto, defs in por_item_def.items():
        c = por_item[produto]
        valores = [produto] + [c[k] or None for k in "ABCD"] + [sum(c.values())]
        for i, v in enumerate(valores, start=1):
            cel = ws.cell(row=lin, column=i, value=v)
            cel.font, cel.border = negrito, borda
            cel.fill = PatternFill("solid", fgColor="EEF2F9")
        lin += 1
        for nome, cd in defs.items():
            valores = ["    " + nome] + [cd[k] or None for k in "ABCD"] + [sum(cd.values())]
            for i, v in enumerate(valores, start=1):
                ws.cell(row=lin, column=i, value=v).border = borda
            ws.row_dimensions[lin].outlineLevel = 1
            lin += 1
    total = sum(tot.values())
    valores = ["Total"] + [tot[k] for k in "ABCD"] + [total]
    for i, v in enumerate(valores, start=1):
        cel = ws.cell(row=lin, column=i, value=v)
        cel.font, cel.border = negrito, borda
        cel.fill = PatternFill("solid", fgColor="DCE3F0")
    lin += 1
    for i, k in enumerate("ABCD", start=2):
        cel = ws.cell(row=lin, column=i, value=(tot[k] / total) if total else 0)
        cel.number_format = "0.0%"
        cel.fill = PatternFill("solid", fgColor=CORES_CLARAS[k])
    ws.sheet_properties.outlinePr.summaryBelow = False
    ws.column_dimensions["A"].width = 46
    for col in "BCDEF":
        ws.column_dimensions[col].width = 11
    ws.freeze_panes = "B6"

    # --- Discount Policy ---
    ws = wb.create_sheet("Discount Policy")
    ws["A1"] = "Discount Report" + (f" - Revision {info['versao']}" if info["versao"] > 1 else "")
    ws["A1"].font = Font(bold=True, size=14)
    produtos = sum((it["preco"] or 0) * (it["quantidade"] or 0) for it in itens_fob)
    desconto = sum(l["desconto"] or 0 for l in linhas)
    ws["A2"], ws["B2"] = "Supplier", info["fornecedor"]
    ws["A3"], ws["B3"] = "Invoice", rotulo_inv
    ws["D2"], ws["E2"] = f"Products Price {moeda}", round(produtos, 2)
    ws["D3"], ws["E3"] = "Total Discount", round(desconto, 2)
    ws["F2"], ws["G2"] = f"Final Value {moeda}", round(produtos - desconto, 2)
    for c in ("A2", "A3", "D2", "D3", "F2"):
        ws[c].font = negrito
    for c in ("E2", "E3", "G2"):
        ws[c].number_format = fmt_moeda
    ws["E3"].font = Font(bold=True, color="C0141C")
    titulos = ["Item", "A", "B", "Rework Quantity (C)", "Rejected Quantity (D)", "A + B %", f"Price (FOB) {moeda}",
               "Rework Cost (C %)", "Rejection (D %)", f"Discount Price {moeda}"]
    cabecalho(ws, 5, titulos)
    lin = 6
    for l in linhas:
        tot_item = l["A"] + l["B"] + l["C"] + l["D"]
        valores = [l["codigo"], l["A"] or None, l["B"] or None, l["C"] or None, l["D"] or None,
                   ((l["A"] + l["B"]) / tot_item) if tot_item else None, l["preco"], (l["pct_c"] or 0) / 100,
                   (l["pct_d"] or 0) / 100, l["desconto"] or 0]
        for i, v in enumerate(valores, start=1):
            cel = ws.cell(row=lin, column=i, value=v)
            cel.border = borda
            if i in (6, 8, 9):
                cel.number_format = "0%"
            if i in (7, 10):
                cel.number_format = fmt_moeda
        lin += 1
    cel = ws.cell(row=lin, column=1, value="Total")
    cel.font = negrito
    cel = ws.cell(row=lin, column=10, value=round(desconto, 2))
    cel.font, cel.number_format = negrito, fmt_moeda
    larguras = [16, 9, 9, 12, 12, 10, 14, 13, 13, 16]
    for i, w in enumerate(larguras, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # --- Data Base (oculta): só os eventos deste invoice, com a categoria final ---
    ws = wb.create_sheet("Data Base")
    cols = ["INVOICE", "NF", "PRODUCT", "DESCRIPTION", "QUANTITY", "CATEGORY", "FOUND DEFECT", "REPORT DATE", "LOT", "FAMILY"]
    cabecalho(ws, 1, cols)
    for i, e in enumerate(eventos, start=2):
        valores = [rotulo_inv, e.get("numero_nf"), e["produto"], e.get("descricao"), e["quantidade"], e["categoria"],
                   e["en"] or "Ok", (e.get("report_date") or "")[:10] or None, e.get("lote"), e.get("family")]
        for j, v in enumerate(valores, start=1):
            ws.cell(row=i, column=j, value=v)
    ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{max(2, len(eventos) + 1)}"
    for j, w in enumerate([22, 10, 14, 40, 10, 10, 40, 12, 10, 22], start=1):
        ws.column_dimensions[get_column_letter(j)].width = w
    ws.sheet_state = "hidden"

    # --- Price (FOB) (oculta) ---
    ws = wb.create_sheet("Price (FOB)")
    cabecalho(ws, 1, ["REFERENCE", "DESCRIPTION", "QTY", "UNITY PRICE", "TOTAL PRICE"])
    for i, it in enumerate(itens_fob, start=2):
        ws.cell(row=i, column=1, value=it["codigo"])
        ws.cell(row=i, column=2, value=it["descricao"])
        ws.cell(row=i, column=3, value=it["quantidade"])
        ws.cell(row=i, column=4, value=it["preco"]).number_format = fmt_moeda
        ws.cell(row=i, column=5, value=round((it["preco"] or 0) * (it["quantidade"] or 0), 2)).number_format = fmt_moeda
    for j, w in enumerate([16, 46, 10, 13, 14], start=1):
        ws.column_dimensions[get_column_letter(j)].width = w
    ws.sheet_state = "hidden"

    saida = io.BytesIO()
    wb.save(saida)
    return saida.getvalue()


def nome_arquivo(info):
    base = f"Inspection Results {info['invoice']} - {info['fornecedor']}"
    if info["versao"] > 1:
        base += f" - Revision {info['versao']}"
    return "".join(ch for ch in base if ch not in '\\/:*?"<>|') + ".xlsx"


# ---------------------------------------------------------------------------
# E-mail (HTML com tabelas e estilos inline: funciona no Outlook, sem imagens)
# ---------------------------------------------------------------------------
def frase_categorias(tot):
    total = sum(tot.values())
    partes = [f"{num_br(tot[k])} ({pct_br(tot[k] / total)}) {'were classified as' if i == 0 else 'as'} {k}"
              for i, k in enumerate("ABCD") if total]
    if len(partes) > 1:
        partes[-1] = "and " + partes[-1]
    return f"We have analysed a total of {num_br(total)} units. " + ", ".join(partes) + "."


def textos_padrao(info, total_desconto, moeda_txt):
    rev = info["versao"] > 1
    return {
        "saudacao": f"Dear {info['fornecedor_curto']} team,",
        "introducao": (f"We have revised the inspection results of invoice {info['invoice']} - {info['fornecedor_curto']} "
                       f"(revision {info['versao']}), and below you can find the updated results:" if rev else
                       f"We have finished inspecting the items from invoice {info['invoice']} - {info['fornecedor_curto']}, "
                       "and below you can find the results:"),
        "desconto": (f"The total discount for this invoice is {moeda_txt}. We will deduct the discounted value for all units "
                     "classified as C and D from the shipment's invoice. Once we agree on a new shipment, we will deduct the "
                     "remaining amount from the current invoice from the new invoice, allowing us to settle the outstanding "
                     "balance on the current invoice. Please review the discount value and return to us with your approval. "
                     "If we don't receive it within a 7-day period, we will consider it approved."
                     if total_desconto > 0.005 else
                     "No discount will be applied to this invoice."),
        "fechamento": ("We hope this analysis helps you understand the issues we found and the solution. Feel free to get in "
                       "touch and ask any questions, we will gladly answer as soon as possible. Thank you very much for your "
                       "time and effort."),
    }


def corpo_email(info, eventos, textos, link, assinatura_nome, assinatura_cargo):
    tot, por_item, matriz = resumo(eventos)
    p = 'style="margin:0 0 12px 0;font-family:Calibri,Arial,sans-serif;font-size:11pt;color:#1f1f1f"'
    h = 'style="margin:18px 0 8px 0;font-family:Calibri,Arial,sans-serif;font-size:12pt;color:#1F3864"'
    td = 'font-family:Calibri,Arial,sans-serif;font-size:10pt;padding:3px 6px;'
    partes = [f"<p {p}>{escape(textos['saudacao'])}</p>", f"<p {p}>{escape(textos['introducao'])}</p>",
              f"<p {p}>{escape(frase_categorias(tot))}</p>",
              f"<p {p}><b>Category legend:</b><br>" + "<br>".join(f"{k} – {escape(t)};" for k, t in LEGENDA) + "</p>"]
    # qualidade por item: barra 100% empilhada A/B/C/D
    linhas = []
    for produto, c in por_item.items():
        total = sum(c.values()) or 1
        celulas = "".join(
            f'<td width="{max(1, round(c[k] / total * 100))}%" style="{td}background:#{CORES[k]};color:#ffffff;'
            f'text-align:center;white-space:nowrap">{num_br(c[k])} {pct_br(c[k] / total)}</td>'
            for k in "ABCD" if c[k])
        linhas.append(f'<tr><td style="{td}white-space:nowrap;font-weight:bold">{escape(str(produto))}</td>'
                      f'<td width="100%" style="padding:2px 0"><table width="100%" cellpadding="0" cellspacing="0" '
                      f'style="border-collapse:collapse"><tr>{celulas}</tr></table></td></tr>')
    total = sum(tot.values()) or 1
    resumo_cat = "".join(f'<td style="{td}background:#{CORES[k]};color:#fff;text-align:center"><b>{k}</b> '
                         f'{num_br(tot[k])} ({pct_br(tot[k] / total)})</td>' for k in "ABCD")
    partes.append(f"<p {h}><b>Supplier's product quality</b> · {num_br(sum(tot.values()))} units</p>"
                  f'<table cellpadding="0" cellspacing="2" style="border-collapse:separate"><tr>{resumo_cat}</tr></table>'
                  f'<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-top:6px">'
                  + "".join(linhas) + "</table>")
    # análise de defeitos: defeitos / 1000 peças por item e matriz defeito × item
    if matriz:
        itens_def = [pr for pr in por_item if any(matriz[d][pr] for d in matriz)]
        por_mil = sorted(((pr, sum(matriz[d][pr] for d in matriz) / (sum(por_item[pr].values()) or 1) * 1000)
                          for pr in itens_def), key=lambda x: -x[1])
        maior = max((v for _pr, v in por_mil), default=1) or 1
        barras = "".join(
            f'<tr><td style="{td}white-space:nowrap">{escape(str(pr))}</td><td width="100%" style="padding:2px 0">'
            f'<table cellpadding="0" cellspacing="0" style="border-collapse:collapse"><tr>'
            f'<td width="{max(4, round(v / maior * 300))}" style="background:#1E88E5;height:14px;font-size:1px">&nbsp;</td>'
            f'<td style="{td}">{num_br(v)}</td></tr></table></td></tr>' for pr, v in por_mil)
        ordem = sorted(matriz, key=lambda d: -sum(matriz[d].values()))
        cab = "".join(f'<th style="{td}background:#1F3864;color:#fff;white-space:nowrap">{escape(str(pr))}</th>' for pr in itens_def)
        corpo = "".join(
            f'<tr><td style="{td}border-bottom:1px solid #e3e7ef">{escape(d)}</td>'
            + "".join(f'<td style="{td}text-align:center;border-bottom:1px solid #e3e7ef">{num_br(matriz[d][pr]) if matriz[d][pr] else ""}</td>'
                      for pr in itens_def)
            + f'<td style="{td}text-align:center;font-weight:bold;border-bottom:1px solid #e3e7ef">{num_br(sum(matriz[d].values()))}</td></tr>'
            for d in ordem)
        tot_col = "".join(f'<td style="{td}text-align:center;font-weight:bold">{num_br(sum(matriz[d][pr] for d in matriz))}</td>' for pr in itens_def)
        partes.append(f"<p {h}><b>Defect Analysis</b> · number of defects / 1000 products</p>"
                      f'<table cellpadding="0" cellspacing="0" style="border-collapse:collapse">{barras}</table>'
                      f'<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-top:10px">'
                      f'<tr><th style="{td}background:#1F3864;color:#fff;text-align:left">Defect</th>{cab}'
                      f'<th style="{td}background:#1F3864;color:#fff">Total</th></tr>{corpo}'
                      f'<tr><td style="{td}font-weight:bold">Total</td>{tot_col}'
                      f'<td style="{td}text-align:center;font-weight:bold">{num_br(sum(sum(v.values()) for v in matriz.values()))}</td></tr></table>')
    if link:
        partes.append(f"<p {p}>The individual item quality reports are available at the following link: "
                      f'<a href="{escape(link)}">{escape(info["invoice"])} - {escape(info["fornecedor_curto"])}</a>. '
                      "We offer full support to further understand the issues and solve them.</p>")
    partes.append(f'<p style="margin:16px 0 12px 0;font-family:Calibri,Arial,sans-serif;font-size:11pt;color:#1f1f1f">'
                  f"{escape(textos['desconto'])}</p>")
    partes.append(f"<p {p}>{escape(textos['fechamento'])}</p>")
    partes.append(f"<p {p}>Best regards,<br><br><b>{escape(assinatura_nome)}</b><br>{escape(assinatura_cargo)}</p>")
    return ('<div style="font-family:Calibri,Arial,sans-serif;font-size:11pt;color:#1f1f1f;max-width:900px">'
            + "".join(partes) + "</div>")
