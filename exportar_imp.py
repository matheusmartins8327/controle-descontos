"""Exportação dos relatórios da importação (v4.66): Excel (.xlsx real, openpyxl) e PDF (reportlab).

Os arquivos são montados no servidor a cada download, a partir dos dados atuais (nunca de uma tela antiga).
"""
import io

AZUL = "327FEF"
RODAPE = "O essencial da saúde com um Q a mais"
CORES_STATUS = {"verde": "2E9E5B", "amarelo": "E0A800", "vermelho": "D64545", "azul": "327FEF", "neutro": "6B7785"}
CORES_CANAL = {"Verde": "2E9E5B", "Amarelo": "E0B400", "Vermelho": "D64545", "Sem canal": "9AA4AE"}


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------
def xlsx(abas):
    """abas: [{"nome", "cab", "linhas", "titulo" (opcional), "larguras" (opcional)}] -> bytes do .xlsx."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    wb.remove(wb.active)
    fino = Side(style="thin", color="C9D1D9")
    borda = Border(left=fino, right=fino, top=fino, bottom=fino)
    for aba in abas:
        ws = wb.create_sheet(aba["nome"][:31])
        lin = 1
        if aba.get("titulo"):
            ws.cell(row=1, column=1, value=aba["titulo"]).font = Font(bold=True, size=13, color=AZUL)
            lin = 3
        for j, c in enumerate(aba["cab"], start=1):
            cel = ws.cell(row=lin, column=j, value=c)
            cel.font = Font(bold=True, color="FFFFFF")
            cel.fill = PatternFill("solid", fgColor=AZUL)
            cel.alignment = Alignment(vertical="center", wrap_text=True)
            cel.border = borda
        for i, linha in enumerate(aba["linhas"], start=lin + 1):
            for j, v in enumerate(linha, start=1):
                cel = ws.cell(row=i, column=j, value=v)
                cel.border = borda
                cel.alignment = Alignment(vertical="top", wrap_text=isinstance(v, str) and len(v) > 40)
        ws.freeze_panes = ws.cell(row=lin + 1, column=1)
        if aba["linhas"]:
            ws.auto_filter.ref = f"A{lin}:{get_column_letter(len(aba['cab']))}{lin + len(aba['linhas'])}"
        for j, c in enumerate(aba["cab"], start=1):
            if aba.get("larguras"):
                larg = aba["larguras"][j - 1]
            else:
                maior = max([len(str(c))] + [len(str(l[j - 1])) for l in aba["linhas"][:300] if l[j - 1] is not None])
                larg = min(max(10, maior + 2), 60)
            ws.column_dimensions[get_column_letter(j)].width = larg
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def xlsx_canal(titulo, periodo, cab, linhas):
    """Planilha "IMPORTAÇÕES PARAMETRIZADAS EM CANAL ...": título, cabeçalho com fundo amarelo, bordas finas."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = "Importações por canal"
    n = len(cab)
    fino = Side(style="thin", color="000000")
    borda = Border(left=fino, right=fino, top=fino, bottom=fino)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=n)
    c = ws.cell(row=1, column=1, value=titulo)
    c.font = Font(bold=True, size=12)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=n)
    c = ws.cell(row=2, column=1, value=periodo)
    c.alignment = Alignment(horizontal="center")
    c.font = Font(italic=True, size=9, color="555555")
    for j, t in enumerate(cab, start=1):
        c = ws.cell(row=4, column=j, value=t)
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="FFFF00")
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = borda
    for i, linha in enumerate(linhas, start=5):
        for j, v in enumerate(linha, start=1):
            c = ws.cell(row=i, column=j, value=v)
            c.border = borda
            c.alignment = Alignment(horizontal="left" if j == 1 else "center")
    if not linhas:
        ws.merge_cells(start_row=5, start_column=1, end_row=5, end_column=n)
        ws.cell(row=5, column=1, value="Nenhuma importação parametrizada neste período.").alignment = Alignment(horizontal="center")
    larguras = {"SHIPPER": 30, "INVOICE": 22, "DATA REGISTRO": 16, "CANAL": 12, "STATUS": 26}
    for j, t in enumerate(cab, start=1):
        ws.column_dimensions[get_column_letter(j)].width = larguras.get(t, 16)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def csv_bytes(cab, linhas):
    """CSV para o Excel brasileiro (separador ";" e BOM)."""
    import csv
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(cab)
    for l in linhas:
        w.writerow(["" if v is None else (f"{v:g}".replace(".", ",") if isinstance(v, float) else v) for v in l])
    return ("﻿" + buf.getvalue()).encode("utf-8")


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
TROCAS = {"−": "-", "≤": "<=", "≥": ">=", "→": "->", "×": "x", "…": "...", "’": "'", "“": '"', "”": '"'}


def txt(v):
    """Texto seguro para as fontes padrão do PDF."""
    s = "" if v is None else str(v)
    for a, b in TROCAS.items():
        s = s.replace(a, b)
    return s.encode("cp1252", "replace").decode("cp1252")


class Pdf:
    """Documento com cabeçalho (título), rodapé da Quinelato e numeração de páginas."""

    def __init__(self, titulo, subtitulo="", paisagem=False):
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        self.colors = colors
        self.tamanho = landscape(A4) if paisagem else A4
        self.titulo, self.subtitulo = titulo, subtitulo
        self.azul = colors.HexColor("#" + AZUL)
        base = getSampleStyleSheet()
        self.st = {
            "h1": ParagraphStyle("h1", parent=base["Heading1"], fontName="Helvetica-Bold", fontSize=16,
                                 textColor=self.azul, spaceAfter=2),
            "sub": ParagraphStyle("sub", parent=base["Normal"], fontSize=9, textColor=colors.HexColor("#555555"),
                                  spaceAfter=8),
            "h2": ParagraphStyle("h2", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=12,
                                 textColor=self.azul, spaceBefore=10, spaceAfter=4),
            "p": ParagraphStyle("p", parent=base["Normal"], fontSize=9, leading=12),
            "cel": ParagraphStyle("cel", parent=base["Normal"], fontSize=7.5, leading=9),
            "celb": ParagraphStyle("celb", parent=base["Normal"], fontSize=7.5, leading=9, fontName="Helvetica-Bold",
                                   textColor=colors.white),
            "card_v": ParagraphStyle("cv", parent=base["Normal"], fontSize=14, leading=17, fontName="Helvetica-Bold"),
            "card_r": ParagraphStyle("cr", parent=base["Normal"], fontSize=7.5, leading=9,
                                     textColor=colors.HexColor("#555555")),
        }
        self.historia = []
        self.largura = self.tamanho[0] - 30 * 2.835  # área útil: margens de 15 mm

    # elementos -----------------------------------------------------------
    def p(self, texto, estilo="p"):
        from reportlab.platypus import Paragraph
        from xml.sax.saxutils import escape
        self.historia.append(Paragraph(escape(txt(texto)), self.st[estilo]))

    def h2(self, texto):
        self.p(texto, "h2")

    def espaco(self, mm=3):
        from reportlab.platypus import Spacer
        self.historia.append(Spacer(1, mm * 2.835))

    def quebra(self):
        from reportlab.platypus import PageBreak
        self.historia.append(PageBreak())

    def cartoes(self, cartoes, por_linha=5):
        """cartoes: [(rótulo, valor, cor, observação)]"""
        from reportlab.platypus import Paragraph, Table, TableStyle
        from xml.sax.saxutils import escape
        cels = []
        for rot, val, cor, obs in cartoes:
            c = self.colors.HexColor("#" + CORES_STATUS.get(cor, "333333")) if cor != "neutro" else self.colors.HexColor("#1F2933")
            st = self.st["card_v"].clone("cv2", textColor=c)
            cels.append([Paragraph(escape(txt(val)), st), Paragraph(escape(txt(rot)) + (f"<br/><font size=6.5>{escape(txt(obs))}</font>" if obs else ""),
                                                                     self.st["card_r"])])
        linhas = [cels[i:i + por_linha] for i in range(0, len(cels), por_linha)]
        larg = self.largura / por_linha
        for linha in linhas:
            linha = linha + [""] * (por_linha - len(linha))
            t = Table([linha], colWidths=[larg] * por_linha)
            t.setStyle(TableStyle([("BOX", (0, 0), (len([x for x in linha if x]) - 1, 0), 0.4, self.colors.HexColor("#C9D1D9")),
                                   ("INNERGRID", (0, 0), (len([x for x in linha if x]) - 1, 0), 0.4, self.colors.HexColor("#C9D1D9")),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 5),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
            self.historia.append(t)
            self.espaco(2)

    def tabela(self, cab, linhas, larguras=None, vazio="Nenhum registro."):
        from reportlab.platypus import Paragraph, Table, TableStyle
        from xml.sax.saxutils import escape
        if not linhas:
            self.p(vazio)
            return
        dados = [[Paragraph(escape(txt(c)), self.st["celb"]) for c in cab]]
        for l in linhas:
            dados.append([Paragraph(escape(txt(_fmt(v))), self.st["cel"]) for v in l])
        if larguras:
            tot = sum(larguras)
            larguras = [self.largura * x / tot for x in larguras]
        t = Table(dados, colWidths=larguras, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), self.azul),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [self.colors.white, self.colors.HexColor("#F3F7FA")]),
            ("GRID", (0, 0), (-1, -1), 0.3, self.colors.HexColor("#C9D1D9")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        self.historia.append(t)

    def grafico(self, titulo, serie, formato="n", largura=None, altura=150, cores=None):
        """Barras com o valor escrito em cima de cada uma (no máximo 12). serie: [(rótulo, valor)]"""
        self.historia.append(grafico(titulo, serie, formato, largura or self.largura, altura, cores))

    def graficos_lado_a_lado(self, itens, altura=150):
        from reportlab.platypus import Table, TableStyle
        larg = self.largura / 2 - 6
        cels = [grafico(t, s, f, larg, altura, c) for t, s, f, c in itens]
        for i in range(0, len(cels), 2):
            par = cels[i:i + 2] + ([""] if len(cels[i:i + 2]) == 1 else [])
            t = Table([par], colWidths=[larg + 6, larg + 6])
            t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
            self.historia.append(t)
            self.espaco(3)

    # montagem ------------------------------------------------------------
    def bytes(self, gerado_em=""):
        from reportlab.platypus import SimpleDocTemplate, Paragraph
        from xml.sax.saxutils import escape
        buf = io.BytesIO()
        mm = 2.835
        doc = SimpleDocTemplate(buf, pagesize=self.tamanho, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm,
                                bottomMargin=16 * mm, title=txt(self.titulo), author="Quinelato")
        azul, titulo = self.azul, self.titulo

        def rodape(canvas, d):
            canvas.saveState()
            canvas.setStrokeColor(azul)
            canvas.setLineWidth(0.8)
            canvas.line(d.leftMargin, 11 * mm, d.leftMargin + d.width, 11 * mm)
            canvas.setFont("Helvetica-Oblique", 7.5)
            canvas.setFillColor(azul)
            canvas.drawString(d.leftMargin, 7 * mm, txt(RODAPE))
            canvas.setFillColorRGB(0.4, 0.4, 0.4)
            canvas.drawRightString(d.leftMargin + d.width, 7 * mm, txt(f"{titulo} · página {d.page}"))
            canvas.restoreState()

        cab = [Paragraph(escape(txt(self.titulo)), self.st["h1"]),
               Paragraph(escape(txt(" · ".join(x for x in (self.subtitulo, gerado_em and f"gerado em {gerado_em}") if x))),
                         self.st["sub"])]
        doc.build(cab + self.historia, onFirstPage=rodape, onLaterPages=rodape)
        return buf.getvalue()


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:,.2f}".rstrip("0").rstrip(",").replace(",", "X").replace(".", ",").replace("X", ".") if v % 1 else \
            f"{v:,.0f}".replace(",", ".")
    return v


def grafico(titulo, serie, formato, largura, altura, cores=None):
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.shapes import Drawing, String
    from reportlab.lib import colors
    d = Drawing(largura, altura + 22)
    d.add(String(0, altura + 10, txt(titulo), fontName="Helvetica-Bold", fontSize=9, fillColor=colors.HexColor("#1F2933")))
    serie = serie[:12]
    if not serie:
        d.add(String(largura / 2, altura / 2, "—  sem dados suficientes", fontName="Helvetica", fontSize=9,
                     textAnchor="middle", fillColor=colors.HexColor("#777777")))
        return d
    valores = [v for _r, v in serie]
    rotulos = [txt(r) for r, _v in serie]
    ch = VerticalBarChart()
    ch.x, ch.y = 28, 46
    ch.width, ch.height = largura - 36, altura - 62
    ch.data = [valores]
    menor, maior = min(0, min(valores)), max(0, max(valores))
    folga = (maior - menor) * 0.18 or 1
    ch.valueAxis.valueMin = menor - (folga if menor < 0 else 0)
    ch.valueAxis.valueMax = maior + folga
    ch.valueAxis.labels.fontSize = 6.5
    ch.valueAxis.labels.fontName = "Helvetica"
    ch.valueAxis.visibleGrid = True
    ch.valueAxis.gridStrokeColor = colors.HexColor("#E5E9ED")
    ch.categoryAxis.categoryNames = rotulos
    ch.categoryAxis.labels.fontSize = 6 if len(serie) > 6 else 6.5
    ch.categoryAxis.labels.fontName = "Helvetica"
    ch.categoryAxis.labels.angle = 30 if len(serie) > 4 else 0
    ch.categoryAxis.labels.boxAnchor = "ne" if len(serie) > 4 else "n"
    ch.categoryAxis.labels.dy = -2
    ch.barSpacing = 2
    ch.bars[0].fillColor = colors.HexColor("#" + AZUL)
    ch.bars[0].strokeColor = None
    for i, (r, _v) in enumerate(serie):
        cor = (cores or {}).get(r) or CORES_CANAL.get(r) if cores is not False else None
        if cor:
            ch.bars[(0, i)].fillColor = colors.HexColor("#" + cor)
    ch.barLabelFormat = (lambda v: _rotulo(v, formato))
    ch.barLabels.fontSize = 7
    ch.barLabels.fontName = "Helvetica-Bold"
    ch.barLabels.nudge = 7
    d.add(ch)
    return d


def _rotulo(v, formato):
    if v is None:
        return ""
    if formato == "pct":
        return f"{v:.0f}%"
    if formato == "dias":
        return f"{v:.1f}".replace(".", ",")
    return f"{v:g}".replace(".", ",")
