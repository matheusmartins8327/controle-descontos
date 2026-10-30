"""
Controle de Descontos - Fornecedores do Paquistão
Versão 1.0

Fluxo:
  1. Qualidade    -> registra o desconto após inspeção do invoice (invoice de origem)
  2. Financeiro   -> confirma a retenção do valor no pagamento do invoice de origem
  3. Compras      -> registra o(s) novo(s) invoice(s) firmado(s) com a dedução do valor.
                     O desconto pode ser dividido em várias deduções (ex.: 10.000 = 4.000 + 6.000)
  4. Recebimento  -> confirma a chegada de cada invoice deduzido na fábrica
  5. Financeiro   -> paga a parte correspondente do valor retido do invoice de origem

Todos os valores são em dólar (USD).
"""
import os
import re
import socket
import secrets
import sqlite3
from datetime import datetime
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
DB_PATH = os.environ.get("DESCONTOS_DB", os.path.join(INSTANCE_DIR, "descontos.db"))
MAX_USUARIOS = 5

# ---------------------------------------------------------------------------
# Perfis e fluxo
# ---------------------------------------------------------------------------
PERFIS = {
    "admin": "Administrador",
    "qualidade": "Qualidade / Inspeção",
    "financeiro": "Financeiro",
    "compras": "Compras",
    "recebimento": "Recebimento / Fábrica",
}

STATUS = {
    "AGUARDANDO_RETENCAO": "Aguardando retenção",
    "AGUARDANDO_DEDUCAO": "Aguardando dedução",
    "AGUARDANDO_CHEGADA": "Aguardando chegada",
    "AGUARDANDO_PAGAMENTO": "Aguardando pagamento",
    "CONCLUIDO": "Concluído",
    "CANCELADO": "Cancelado",
}

# Status de cada dedução (parcela do desconto abatida em um invoice)
STATUS_DEDUCAO = {
    "AGUARDANDO_CHEGADA": "Aguardando chegada",
    "AGUARDANDO_PAGAMENTO": "Aguardando pagamento",
    "PAGO": "Pago",
}

# Etapas e perfil responsável
ETAPAS = {
    "reter": {"titulo": "Confirmar retenção", "perfil": "financeiro"},
    "deduzir": {"titulo": "Registrar dedução em invoice", "perfil": "compras"},
    "receber": {"titulo": "Confirmar chegada na fábrica", "perfil": "recebimento"},
    "pagar": {"titulo": "Pagar valor retido", "perfil": "financeiro"},
}

TOLERANCIA = 0.005  # diferença de arredondamento aceita (meio centavo)

app = Flask(__name__)


def _secret_key():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    os.makedirs(INSTANCE_DIR, exist_ok=True)
    path = os.path.join(INSTANCE_DIR, "secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app.config["SECRET_KEY"] = _secret_key()

# ---------------------------------------------------------------------------
# Banco de dados
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS usuarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    senha_hash TEXT NOT NULL,
    perfil TEXT NOT NULL,
    ativo INTEGER NOT NULL DEFAULT 1,
    criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fornecedores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL UNIQUE,
    cidade TEXT,
    contato TEXT,
    ativo INTEGER NOT NULL DEFAULT 1,
    criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS descontos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fornecedor_id INTEGER NOT NULL REFERENCES fornecedores(id),
    status TEXT NOT NULL,
    -- Etapa 1: registro (Qualidade)
    invoice_origem TEXT NOT NULL,
    data_envio_relatorio TEXT,
    valor_invoice_origem REAL,
    valor_desconto REAL NOT NULL,
    motivo TEXT,  -- observações
    registrado_por INTEGER REFERENCES usuarios(id),
    registrado_em TEXT,
    -- Etapa 2: retenção (Financeiro)
    data_retencao TEXT,
    obs_retencao TEXT,
    retido_por INTEGER REFERENCES usuarios(id),
    retido_em TEXT,
    -- Cancelamento (Administrador)
    motivo_cancelamento TEXT,
    cancelado_por INTEGER REFERENCES usuarios(id),
    cancelado_em TEXT
);
-- Etapas 3 a 5: cada desconto pode ser abatido em um ou mais invoices
CREATE TABLE IF NOT EXISTS deducoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    desconto_id INTEGER NOT NULL REFERENCES descontos(id),
    status TEXT NOT NULL,
    -- Etapa 3: dedução em novo invoice (Compras)
    invoice TEXT NOT NULL,
    data_invoice TEXT,
    valor_invoice REAL,
    valor_deduzido REAL NOT NULL,
    obs_deducao TEXT,
    deduzido_por INTEGER REFERENCES usuarios(id),
    deduzido_em TEXT,
    -- Etapa 4: chegada do invoice deduzido (Recebimento)
    data_chegada TEXT,
    obs_chegada TEXT,
    recebido_por INTEGER REFERENCES usuarios(id),
    recebido_em TEXT,
    -- Etapa 5: pagamento da parte correspondente do valor retido (Financeiro)
    data_pagamento TEXT,
    ref_pagamento TEXT,
    obs_pagamento TEXT,
    pago_por INTEGER REFERENCES usuarios(id),
    pago_em TEXT
);
CREATE TABLE IF NOT EXISTS historico (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    desconto_id INTEGER NOT NULL REFERENCES descontos(id),
    usuario_id INTEGER REFERENCES usuarios(id),
    acao TEXT NOT NULL,
    status_anterior TEXT,
    status_novo TEXT,
    observacao TEXT,
    criado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_descontos_status ON descontos(status);
CREATE INDEX IF NOT EXISTS idx_descontos_fornecedor ON descontos(fornecedor_id);
CREATE INDEX IF NOT EXISTS idx_historico_desconto ON historico(desconto_id);
CREATE INDEX IF NOT EXISTS idx_deducoes_desconto ON deducoes(desconto_id);
CREATE INDEX IF NOT EXISTS idx_deducoes_status ON deducoes(status);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(SCHEMA)
        # bancos criados na versão 1.0/1.1 não têm a data de envio do relatório
        colunas = {r[1] for r in db.execute("PRAGMA table_info(descontos)")}
        if "data_envio_relatorio" not in colunas:
            db.execute("ALTER TABLE descontos ADD COLUMN data_envio_relatorio TEXT")


def agora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def registrar_historico(desconto_id, acao, status_anterior=None, status_novo=None, obs=None):
    get_db().execute(
        "INSERT INTO historico (desconto_id, usuario_id, acao, status_anterior, status_novo, observacao, criado_em)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (desconto_id, g.usuario["id"], acao, status_anterior, status_novo, obs, agora()),
    )


# ---------------------------------------------------------------------------
# Autenticação, permissões e CSRF
# ---------------------------------------------------------------------------
def total_usuarios_ativos():
    return get_db().execute("SELECT COUNT(*) FROM usuarios WHERE ativo = 1").fetchone()[0]


@app.before_request
def carregar_usuario():
    g.usuario = None
    uid = session.get("usuario_id")
    if uid:
        g.usuario = get_db().execute(
            "SELECT * FROM usuarios WHERE id = ? AND ativo = 1", (uid,)
        ).fetchone()
        if g.usuario is None:
            session.clear()


@app.before_request
def verificar_csrf():
    if request.method == "POST":
        token = session.get("csrf_token")
        if not token or token != request.form.get("csrf_token"):
            abort(400, "Token de segurança inválido. Recarregue a página e tente novamente.")


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return session["csrf_token"]


def login_obrigatorio(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.usuario is None:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def somente_admin(view):
    @wraps(view)
    @login_obrigatorio
    def wrapper(*args, **kwargs):
        if g.usuario["perfil"] != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def pode(perfil_necessario):
    """Administrador pode executar qualquer etapa."""
    return g.usuario is not None and g.usuario["perfil"] in ("admin", perfil_necessario)


# ---------------------------------------------------------------------------
# Helpers de template
# ---------------------------------------------------------------------------
def fmt_num(valor):
    if not isinstance(valor, (int, float)):
        return ""
    return f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_moeda(valor):
    if not isinstance(valor, (int, float)):
        return "—"
    return f"US$ {fmt_num(valor)}"


def fmt_data(valor):
    if not valor:
        return "—"
    try:
        if len(valor) > 10:
            return datetime.strptime(valor, "%Y-%m-%d %H:%M:%S").strftime("%d/%m/%Y %H:%M")
        return datetime.strptime(valor, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return valor


def codigo_desconto(d):
    return f"DSC-{d['id']:05d}"


app.jinja_env.globals.update(
    csrf_token=csrf_token, PERFIS=PERFIS, STATUS=STATUS, ETAPAS=ETAPAS,
    STATUS_DEDUCAO=STATUS_DEDUCAO, pode=pode,
    codigo_desconto=codigo_desconto, MAX_USUARIOS=MAX_USUARIOS,
)
app.jinja_env.filters["moeda"] = fmt_moeda
app.jinja_env.filters["num"] = fmt_num
app.jinja_env.filters["data"] = fmt_data


def parse_num(texto):
    """Aceita 1.234,56 / 1234,56 / 1,234.56 / 1234.56 / 10.000 / US$ 10.000,00."""
    if texto is None:
        return None
    s = str(texto).strip().replace(" ", "").replace("US$", "").replace("$", "")
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".") if s.count(",") == 1 else s.replace(",", "")
    elif s.count(".") > 1 or re.fullmatch(r"\d{1,3}\.\d{3}", s):
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Configuração inicial e login
# ---------------------------------------------------------------------------
@app.route("/configuracao-inicial", methods=["GET", "POST"])
def setup():
    if get_db().execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] > 0:
        return redirect(url_for("login"))
    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        email = request.form.get("email", "").strip().lower()
        senha = request.form.get("senha", "")
        if not nome or not email or len(senha) < 8:
            flash("Preencha nome, e-mail e uma senha com no mínimo 8 caracteres.", "erro")
        elif senha != request.form.get("senha2"):
            flash("As senhas não conferem.", "erro")
        else:
            db = get_db()
            db.execute(
                "INSERT INTO usuarios (nome, email, senha_hash, perfil, criado_em) VALUES (?, ?, ?, 'admin', ?)",
                (nome, email, generate_password_hash(senha), agora()),
            )
            db.commit()
            flash("Administrador criado. Faça login para continuar.", "ok")
            return redirect(url_for("login"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if get_db().execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] == 0:
        return redirect(url_for("setup"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        senha = request.form.get("senha", "")
        u = get_db().execute("SELECT * FROM usuarios WHERE email = ?", (email,)).fetchone()
        if u and u["ativo"] and check_password_hash(u["senha_hash"], senha):
            session.clear()
            session["usuario_id"] = u["id"]
            destino = request.args.get("next", "")
            if not destino.startswith("/") or destino.startswith("//"):
                destino = url_for("painel")
            return redirect(destino)
        flash("E-mail ou senha inválidos.", "erro")
    return render_template("login.html")


@app.route("/sair")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/minha-senha", methods=["GET", "POST"])
@login_obrigatorio
def minha_senha():
    if request.method == "POST":
        atual = request.form.get("atual", "")
        nova = request.form.get("nova", "")
        if not check_password_hash(g.usuario["senha_hash"], atual):
            flash("Senha atual incorreta.", "erro")
        elif len(nova) < 8:
            flash("A nova senha deve ter no mínimo 8 caracteres.", "erro")
        elif nova != request.form.get("nova2"):
            flash("As senhas não conferem.", "erro")
        else:
            db = get_db()
            db.execute("UPDATE usuarios SET senha_hash = ? WHERE id = ?",
                       (generate_password_hash(nova), g.usuario["id"]))
            db.commit()
            flash("Senha alterada com sucesso.", "ok")
            return redirect(url_for("painel"))
    return render_template("minha_senha.html")


# ---------------------------------------------------------------------------
# Painel / lista de descontos
# ---------------------------------------------------------------------------
SQL_LISTA = """
SELECT d.*, f.nome AS fornecedor,
    COALESCE((SELECT SUM(x.valor_deduzido) FROM deducoes x WHERE x.desconto_id = d.id), 0) AS total_deduzido,
    COALESCE((SELECT SUM(x.valor_deduzido) FROM deducoes x
              WHERE x.desconto_id = d.id AND x.status = 'PAGO'), 0) AS total_pago,
    (SELECT GROUP_CONCAT(x.invoice, ', ') FROM deducoes x WHERE x.desconto_id = d.id) AS invoices_deducao
FROM descontos d JOIN fornecedores f ON f.id = d.fornecedor_id
"""

# Filtros do painel. "Aguardando chegada/pagamento" considera as deduções do desconto.
FILTROS_STATUS = {
    "ABERTOS": "d.status NOT IN ('CONCLUIDO', 'CANCELADO')",
    "AGUARDANDO_RETENCAO": "d.status = 'AGUARDANDO_RETENCAO'",
    "AGUARDANDO_DEDUCAO": "d.status = 'AGUARDANDO_DEDUCAO'",
    "AGUARDANDO_CHEGADA": "d.status <> 'CANCELADO' AND EXISTS (SELECT 1 FROM deducoes x"
                          " WHERE x.desconto_id = d.id AND x.status = 'AGUARDANDO_CHEGADA')",
    "AGUARDANDO_PAGAMENTO": "d.status <> 'CANCELADO' AND EXISTS (SELECT 1 FROM deducoes x"
                            " WHERE x.desconto_id = d.id AND x.status = 'AGUARDANDO_PAGAMENTO')",
    "CONCLUIDO": "d.status = 'CONCLUIDO'",
    "CANCELADO": "d.status = 'CANCELADO'",
}


def saldo_a_deduzir(d):
    return round(d["valor_desconto"] - d["total_deduzido"], 2)


def atualizar_status(desconto_id):
    """Recalcula o status do desconto a partir da retenção e das deduções."""
    db = get_db()
    d = db.execute(SQL_LISTA + " WHERE d.id = ?", (desconto_id,)).fetchone()
    if d["status"] == "CANCELADO":
        return d["status"]
    pendentes = {r["status"] for r in db.execute(
        "SELECT DISTINCT status FROM deducoes WHERE desconto_id = ? AND status <> 'PAGO'", (desconto_id,))}
    if not d["retido_em"]:
        novo = "AGUARDANDO_RETENCAO"
    elif saldo_a_deduzir(d) > TOLERANCIA:
        novo = "AGUARDANDO_DEDUCAO"
    elif "AGUARDANDO_CHEGADA" in pendentes:
        novo = "AGUARDANDO_CHEGADA"
    elif "AGUARDANDO_PAGAMENTO" in pendentes:
        novo = "AGUARDANDO_PAGAMENTO"
    else:
        novo = "CONCLUIDO"
    if novo != d["status"]:
        db.execute("UPDATE descontos SET status = ? WHERE id = ?", (novo, desconto_id))
    return novo


def registrar_etapa(desconto_id, acao, status_anterior, obs=None):
    """Recalcula o status do desconto e registra a ação no histórico."""
    novo = atualizar_status(desconto_id)
    registrar_historico(desconto_id, acao, status_anterior, novo, obs)
    if novo == "CONCLUIDO" and status_anterior != "CONCLUIDO":
        registrar_historico(desconto_id, "Processo concluído", obs="Todo o valor retido foi deduzido e pago.")
    return novo


def listar_pendencias():
    """Ações pendentes para o perfil do usuário logado."""
    db = get_db()
    pend = []
    ativos = "d.status NOT IN ('CANCELADO', 'CONCLUIDO')"
    if pode("financeiro"):
        for d in db.execute(SQL_LISTA + " WHERE d.status = 'AGUARDANDO_RETENCAO' ORDER BY d.id"):
            pend.append({"d": d, "acao": ETAPAS["reter"]["titulo"], "invoice": None, "valor": d["valor_desconto"]})
    if pode("compras"):
        for d in db.execute(SQL_LISTA + " WHERE d.status = 'AGUARDANDO_DEDUCAO' ORDER BY d.id"):
            pend.append({"d": d, "acao": "Deduzir saldo em invoice", "invoice": None, "valor": saldo_a_deduzir(d)})
    for etapa, status in (("receber", "AGUARDANDO_CHEGADA"), ("pagar", "AGUARDANDO_PAGAMENTO")):
        if not pode(ETAPAS[etapa]["perfil"]):
            continue
        linhas = db.execute(
            "SELECT x.id AS deducao_id, x.invoice, x.valor_deduzido, d.id FROM deducoes x"
            f" JOIN descontos d ON d.id = x.desconto_id WHERE x.status = ? AND {ativos} ORDER BY x.id",
            (status,)).fetchall()
        for x in linhas:
            d = db.execute(SQL_LISTA + " WHERE d.id = ?", (x["id"],)).fetchone()
            pend.append({"d": d, "acao": ETAPAS[etapa]["titulo"], "invoice": x["invoice"], "valor": x["valor_deduzido"]})
    return pend


@app.route("/")
@login_obrigatorio
def painel():
    db = get_db()
    filtro_status = request.args.get("status", "")
    filtro_fornecedor = request.args.get("fornecedor", "")
    busca = request.args.get("q", "").strip()

    where, params = [], []
    if filtro_status in FILTROS_STATUS:
        where.append(FILTROS_STATUS[filtro_status])
    if filtro_fornecedor.isdigit():
        where.append("d.fornecedor_id = ?")
        params.append(int(filtro_fornecedor))
    if busca:
        where.append("(d.invoice_origem LIKE ? OR f.nome LIKE ? OR EXISTS (SELECT 1 FROM deducoes x"
                     " WHERE x.desconto_id = d.id AND x.invoice LIKE ?))")
        params += [f"%{busca}%"] * 3
    sql = SQL_LISTA + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY d.id DESC"
    descontos = db.execute(sql, params).fetchall()

    contagem = {}
    for chave in ("AGUARDANDO_RETENCAO", "AGUARDANDO_DEDUCAO", "CONCLUIDO"):
        contagem[chave] = db.execute(
            f"SELECT COUNT(*) FROM descontos d WHERE {FILTROS_STATUS[chave]}").fetchone()[0]
    for chave in ("AGUARDANDO_CHEGADA", "AGUARDANDO_PAGAMENTO"):
        contagem[chave] = db.execute(
            "SELECT COUNT(*) FROM deducoes x JOIN descontos d ON d.id = x.desconto_id"
            " WHERE x.status = ? AND d.status <> 'CANCELADO'", (chave,)).fetchone()[0]

    fornecedores = db.execute("SELECT id, nome FROM fornecedores ORDER BY nome").fetchall()
    return render_template(
        "painel.html", descontos=descontos, pendencias=listar_pendencias(), contagem=contagem,
        fornecedores=fornecedores, filtro_status=filtro_status,
        filtro_fornecedor=filtro_fornecedor, busca=busca,
    )


# ---------------------------------------------------------------------------
# Registro / edição do desconto (Etapa 1 - Qualidade)
# ---------------------------------------------------------------------------
def obter_fornecedor(nome):
    """Retorna o id do fornecedor pelo nome; cadastra automaticamente se for novo."""
    db = get_db()
    f = db.execute("SELECT id FROM fornecedores WHERE lower(nome) = lower(?)", (nome,)).fetchone()
    if f:
        return f["id"]
    return db.execute("INSERT INTO fornecedores (nome, criado_em) VALUES (?, ?)", (nome, agora())).lastrowid


def validar_form_desconto():
    f = request.form
    valor = parse_num(f.get("valor_desconto"))
    dados = {
        "fornecedor": " ".join(f.get("fornecedor", "").split()),
        "invoice_origem": f.get("invoice_origem", "").strip(),
        "data_envio_relatorio": f.get("data_envio_relatorio") or None,
        "valor_invoice_origem": parse_num(f.get("valor_invoice_origem")),
        "valor_desconto": round(valor, 2) if valor is not None else None,
        "motivo": f.get("motivo", "").strip(),
    }
    erros = []
    if not dados["fornecedor"]:
        erros.append("Informe o nome do fornecedor.")
    if not dados["invoice_origem"]:
        erros.append("Informe o número do invoice inspecionado.")
    if not dados["data_envio_relatorio"]:
        erros.append("Informe a data de envio do relatório.")
    if not dados["valor_invoice_origem"] or dados["valor_invoice_origem"] <= 0:
        erros.append("Informe o valor total do invoice.")
    if not dados["valor_desconto"] or dados["valor_desconto"] <= 0:
        erros.append("Informe o valor do desconto.")
    if (dados["valor_invoice_origem"] and dados["valor_desconto"]
            and dados["valor_desconto"] > dados["valor_invoice_origem"]):
        erros.append("O desconto não pode ser maior que o valor total do invoice.")
    return dados, erros


@app.route("/descontos/novo", methods=["GET", "POST"])
@login_obrigatorio
def desconto_novo():
    if not pode("qualidade"):
        abort(403)
    db = get_db()
    fornecedores = db.execute("SELECT * FROM fornecedores WHERE ativo = 1 ORDER BY nome").fetchall()
    if request.method == "POST":
        dados, erros = validar_form_desconto()
        if not erros:
            cur = db.execute(
                "INSERT INTO descontos (fornecedor_id, status, invoice_origem, data_envio_relatorio,"
                " valor_invoice_origem, valor_desconto, motivo, registrado_por, registrado_em)"
                " VALUES (?, 'AGUARDANDO_RETENCAO', ?, ?, ?, ?, ?, ?, ?)",
                (obter_fornecedor(dados["fornecedor"]), dados["invoice_origem"], dados["data_envio_relatorio"],
                 dados["valor_invoice_origem"], dados["valor_desconto"], dados["motivo"],
                 g.usuario["id"], agora()),
            )
            registrar_historico(cur.lastrowid, "Desconto registrado", None, "AGUARDANDO_RETENCAO",
                                fmt_moeda(dados["valor_desconto"]))
            db.commit()
            flash("Desconto registrado. Aguardando retenção pelo Financeiro.", "ok")
            return redirect(url_for("desconto_detalhe", id=cur.lastrowid))
        for e in erros:
            flash(e, "erro")
        return render_template("desconto_form.html", fornecedores=fornecedores, d=dados, editando=False)
    return render_template("desconto_form.html", fornecedores=fornecedores, d={}, editando=False)


@app.route("/descontos/<int:id>/editar", methods=["GET", "POST"])
@login_obrigatorio
def desconto_editar(id):
    db = get_db()
    d = carregar_desconto(id)
    # Valores só podem ser alterados antes da retenção
    if d["status"] != "AGUARDANDO_RETENCAO" or not pode("qualidade"):
        abort(403)
    fornecedores = db.execute("SELECT * FROM fornecedores WHERE ativo = 1 ORDER BY nome").fetchall()
    if request.method == "POST":
        dados, erros = validar_form_desconto()
        if not erros:
            db.execute(
                "UPDATE descontos SET fornecedor_id=?, invoice_origem=?, data_envio_relatorio=?,"
                " valor_invoice_origem=?, valor_desconto=?, motivo=? WHERE id=?",
                (obter_fornecedor(dados["fornecedor"]), dados["invoice_origem"], dados["data_envio_relatorio"],
                 dados["valor_invoice_origem"], dados["valor_desconto"], dados["motivo"], id),
            )
            obs = None
            if dados["valor_desconto"] != d["valor_desconto"]:
                obs = f"Valor alterado de {fmt_moeda(d['valor_desconto'])} para {fmt_moeda(dados['valor_desconto'])}"
            registrar_historico(id, "Registro editado", obs=obs)
            db.commit()
            flash("Desconto atualizado.", "ok")
            return redirect(url_for("desconto_detalhe", id=id))
        for e in erros:
            flash(e, "erro")
        return render_template("desconto_form.html", fornecedores=fornecedores, d=dados, editando=True, id=id)
    return render_template("desconto_form.html", fornecedores=fornecedores, d=dict(d), editando=True, id=id)


def carregar_desconto(id):
    d = get_db().execute(SQL_LISTA + " WHERE d.id = ?", (id,)).fetchone()
    if d is None:
        abort(404)
    return d


@app.route("/descontos/<int:id>")
@login_obrigatorio
def desconto_detalhe(id):
    db = get_db()
    d = carregar_desconto(id)
    deducoes = db.execute("SELECT * FROM deducoes WHERE desconto_id = ? ORDER BY id", (id,)).fetchall()
    historico = db.execute(
        "SELECT h.*, u.nome AS usuario FROM historico h LEFT JOIN usuarios u ON u.id = h.usuario_id"
        " WHERE h.desconto_id = ? ORDER BY h.id", (id,)
    ).fetchall()
    nomes = {u["id"]: u["nome"] for u in db.execute("SELECT id, nome FROM usuarios")}
    resumo = {
        "deduzido": d["total_deduzido"],
        "saldo": saldo_a_deduzir(d),
        "pago": d["total_pago"],
        "retido_aberto": round(d["valor_desconto"] - d["total_pago"], 2) if d["retido_em"] else 0,
        "n": len(deducoes),
        "chegaram": sum(1 for x in deducoes if x["status"] != "AGUARDANDO_CHEGADA"),
        "pagas": sum(1 for x in deducoes if x["status"] == "PAGO"),
    }
    return render_template("desconto_detalhe.html", d=d, deducoes=deducoes,
                           historico=historico, nomes=nomes, resumo=resumo,
                           hoje=datetime.now().strftime("%Y-%m-%d"))


# ---------------------------------------------------------------------------
# Etapa 2 - Retenção (Financeiro)
# ---------------------------------------------------------------------------
@app.route("/descontos/<int:id>/reter", methods=["POST"])
@login_obrigatorio
def desconto_reter(id):
    if not pode(ETAPAS["reter"]["perfil"]):
        abort(403)
    d = carregar_desconto(id)
    if d["status"] != "AGUARDANDO_RETENCAO":
        flash("A retenção deste desconto já foi registrada.", "erro")
        return redirect(url_for("desconto_detalhe", id=id))
    data = request.form.get("data_retencao")
    if not data:
        flash("Informe a data da retenção.", "erro")
        return redirect(url_for("desconto_detalhe", id=id))
    obs = request.form.get("obs", "").strip() or None
    db = get_db()
    db.execute("UPDATE descontos SET data_retencao=?, obs_retencao=?, retido_por=?, retido_em=? WHERE id=?",
               (data, obs, g.usuario["id"], agora(), id))
    registrar_etapa(id, f"Retenção confirmada · {fmt_moeda(d['valor_desconto'])} no invoice {d['invoice_origem']}",
                    d["status"], obs)
    db.commit()
    flash("Retenção confirmada. Aguardando dedução por Compras.", "ok")
    return redirect(url_for("desconto_detalhe", id=id))


# ---------------------------------------------------------------------------
# Etapa 3 - Dedução em um ou mais invoices (Compras)
# ---------------------------------------------------------------------------
@app.route("/descontos/<int:id>/deducoes", methods=["POST"])
@login_obrigatorio
def deducao_nova(id):
    if not pode(ETAPAS["deduzir"]["perfil"]):
        abort(403)
    d = carregar_desconto(id)
    volta = redirect(url_for("desconto_detalhe", id=id))
    if d["status"] != "AGUARDANDO_DEDUCAO":
        flash("Este desconto não possui saldo disponível para dedução.", "erro")
        return volta
    f = request.form
    invoice = f.get("invoice", "").strip()
    valor = parse_num(f.get("valor_deduzido"))
    valor = round(valor, 2) if valor is not None else None
    valor_invoice = parse_num(f.get("valor_invoice"))
    saldo = saldo_a_deduzir(d)
    erro = None
    if not invoice:
        erro = "Informe o número do invoice em que o valor foi deduzido."
    elif invoice.lower() == d["invoice_origem"].lower():
        erro = "O invoice de dedução deve ser diferente do invoice de origem."
    elif not valor or valor <= 0:
        erro = "Informe o valor deduzido neste invoice."
    elif valor > saldo + TOLERANCIA:
        erro = f"O valor deduzido ({fmt_moeda(valor)}) é maior que o saldo a deduzir ({fmt_moeda(saldo)})."
    elif valor_invoice is not None and valor > valor_invoice + TOLERANCIA:
        erro = "O valor deduzido não pode ser maior que o valor do invoice."
    elif get_db().execute("SELECT 1 FROM deducoes WHERE desconto_id = ? AND lower(invoice) = lower(?)",
                          (id, invoice)).fetchone():
        erro = f"O invoice {invoice} já tem uma dedução registrada para este desconto."
    if erro:
        flash(erro, "erro")
        return volta
    obs = f.get("obs", "").strip() or None
    db = get_db()
    db.execute(
        "INSERT INTO deducoes (desconto_id, status, invoice, data_invoice, valor_invoice, valor_deduzido,"
        " obs_deducao, deduzido_por, deduzido_em) VALUES (?, 'AGUARDANDO_CHEGADA', ?, ?, ?, ?, ?, ?, ?)",
        (id, invoice, f.get("data_invoice") or None, valor_invoice, valor, obs, g.usuario["id"], agora()),
    )
    restante = round(saldo - valor, 2)
    registrar_etapa(id, f"Dedução registrada · {fmt_moeda(valor)} no invoice {invoice}", d["status"],
                    obs or (f"Saldo restante a deduzir: {fmt_moeda(restante)}" if restante > TOLERANCIA else None))
    db.commit()
    if restante > TOLERANCIA:
        flash(f"Dedução registrada. Ainda restam {fmt_moeda(restante)} a deduzir em outro(s) invoice(s).", "ok")
    else:
        flash("Dedução registrada. Todo o valor do desconto já foi deduzido.", "ok")
    return volta


def carregar_deducao(pid):
    x = get_db().execute("SELECT * FROM deducoes WHERE id = ?", (pid,)).fetchone()
    if x is None:
        abort(404)
    return x, carregar_desconto(x["desconto_id"])


# ---------------------------------------------------------------------------
# Etapa 4 - Chegada do invoice deduzido (Recebimento)
# Etapa 5 - Pagamento da parte do valor retido (Financeiro)
# ---------------------------------------------------------------------------
@app.route("/deducoes/<int:pid>/<etapa>", methods=["POST"])
@login_obrigatorio
def deducao_avancar(pid, etapa):
    if etapa not in ("receber", "pagar"):
        abort(404)
    if not pode(ETAPAS[etapa]["perfil"]):
        abort(403)
    x, d = carregar_deducao(pid)
    volta = redirect(url_for("desconto_detalhe", id=d["id"]))
    esperado = "AGUARDANDO_CHEGADA" if etapa == "receber" else "AGUARDANDO_PAGAMENTO"
    if x["status"] != esperado or d["status"] == "CANCELADO":
        flash("Esta dedução não está mais nesta etapa. A página foi atualizada.", "erro")
        return volta
    f, uid, ts = request.form, g.usuario["id"], agora()
    obs = f.get("obs", "").strip() or None
    db = get_db()
    if etapa == "receber":
        data = f.get("data_chegada")
        if not data:
            flash("Informe a data de chegada na fábrica.", "erro")
            return volta
        db.execute("UPDATE deducoes SET status='AGUARDANDO_PAGAMENTO', data_chegada=?, obs_chegada=?,"
                   " recebido_por=?, recebido_em=? WHERE id=?", (data, obs, uid, ts, pid))
        acao = f"Chegada confirmada · invoice {x['invoice']}"
    else:
        data = f.get("data_pagamento")
        if not data:
            flash("Informe a data do pagamento.", "erro")
            return volta
        db.execute("UPDATE deducoes SET status='PAGO', data_pagamento=?, ref_pagamento=?, obs_pagamento=?,"
                   " pago_por=?, pago_em=? WHERE id=?",
                   (data, f.get("ref_pagamento", "").strip() or None, obs, uid, ts, pid))
        acao = (f"Pagamento do valor retido · {fmt_moeda(x['valor_deduzido'])} do invoice {d['invoice_origem']}"
                f" (referente à dedução no invoice {x['invoice']})")
    registrar_etapa(d["id"], acao, d["status"], obs)
    db.commit()
    flash(f"{ETAPAS[etapa]['titulo']}: concluído.", "ok")
    return volta


# ---------------------------------------------------------------------------
# Ações do administrador
# ---------------------------------------------------------------------------
@app.route("/deducoes/<int:pid>/desfazer", methods=["POST"])
@somente_admin
def deducao_desfazer(pid):
    """Desfaz a última etapa da dedução (pagamento -> chegada -> exclusão da dedução)."""
    x, d = carregar_deducao(pid)
    volta = redirect(url_for("desconto_detalhe", id=d["id"]))
    motivo = request.form.get("motivo", "").strip()
    if not motivo:
        flash("Informe o motivo para desfazer.", "erro")
        return volta
    db = get_db()
    if x["status"] == "PAGO":
        db.execute("UPDATE deducoes SET status='AGUARDANDO_PAGAMENTO', data_pagamento=NULL, ref_pagamento=NULL,"
                   " obs_pagamento=NULL, pago_por=NULL, pago_em=NULL WHERE id=?", (pid,))
        acao = f"Pagamento desfeito · invoice {x['invoice']}"
    elif x["status"] == "AGUARDANDO_PAGAMENTO":
        db.execute("UPDATE deducoes SET status='AGUARDANDO_CHEGADA', data_chegada=NULL, obs_chegada=NULL,"
                   " recebido_por=NULL, recebido_em=NULL WHERE id=?", (pid,))
        acao = f"Chegada desfeita · invoice {x['invoice']}"
    else:
        db.execute("DELETE FROM deducoes WHERE id=?", (pid,))
        acao = f"Dedução excluída · {fmt_moeda(x['valor_deduzido'])} no invoice {x['invoice']}"
    registrar_etapa(d["id"], acao, d["status"], motivo)
    db.commit()
    flash("Etapa desfeita.", "ok")
    return volta


@app.route("/descontos/<int:id>/desfazer-retencao", methods=["POST"])
@somente_admin
def desconto_desfazer_retencao(id):
    d = carregar_desconto(id)
    volta = redirect(url_for("desconto_detalhe", id=id))
    motivo = request.form.get("motivo", "").strip()
    if not d["retido_em"] or d["status"] == "CANCELADO":
        flash("Não há retenção a desfazer.", "erro")
    elif d["invoices_deducao"]:
        flash("Desfaça/exclua as deduções antes de desfazer a retenção.", "erro")
    elif not motivo:
        flash("Informe o motivo para desfazer.", "erro")
    else:
        db = get_db()
        db.execute("UPDATE descontos SET data_retencao=NULL, obs_retencao=NULL, retido_por=NULL, retido_em=NULL"
                   " WHERE id=?", (id,))
        registrar_etapa(id, "Retenção desfeita", d["status"], motivo)
        db.commit()
        flash("Retenção desfeita.", "ok")
    return volta


@app.route("/descontos/<int:id>/cancelar", methods=["POST"])
@somente_admin
def desconto_cancelar(id):
    d = carregar_desconto(id)
    motivo = request.form.get("motivo_cancelamento", "").strip()
    if d["status"] in ("CONCLUIDO", "CANCELADO"):
        flash("Este desconto já está encerrado.", "erro")
    elif d["invoices_deducao"]:
        flash("Este desconto já possui deduções. Exclua as deduções antes de cancelar.", "erro")
    elif not motivo:
        flash("Informe o motivo do cancelamento.", "erro")
    else:
        db = get_db()
        db.execute("UPDATE descontos SET status='CANCELADO', motivo_cancelamento=?, cancelado_por=?,"
                   " cancelado_em=? WHERE id=?", (motivo, g.usuario["id"], agora(), id))
        registrar_historico(id, "Desconto cancelado", d["status"], "CANCELADO", motivo)
        db.commit()
        flash("Desconto cancelado.", "ok")
    return redirect(url_for("desconto_detalhe", id=id))


# ---------------------------------------------------------------------------
# Fornecedores
# ---------------------------------------------------------------------------
@app.route("/fornecedores", methods=["GET", "POST"])
@login_obrigatorio
def fornecedores():
    db = get_db()
    pode_editar = pode("qualidade")
    if request.method == "POST":
        if not pode_editar:
            abort(403)
        fid = request.form.get("id", "")
        nome = request.form.get("nome", "").strip()
        cidade = request.form.get("cidade", "").strip()
        contato = request.form.get("contato", "").strip()
        ativo = 1 if request.form.get("ativo", "1") == "1" else 0
        if not nome:
            flash("Informe o nome do fornecedor.", "erro")
        else:
            try:
                if fid.isdigit():
                    db.execute("UPDATE fornecedores SET nome=?, cidade=?, contato=?, ativo=? WHERE id=?",
                               (nome, cidade, contato, ativo, int(fid)))
                    flash("Fornecedor atualizado.", "ok")
                else:
                    db.execute("INSERT INTO fornecedores (nome, cidade, contato, criado_em) VALUES (?, ?, ?, ?)",
                               (nome, cidade, contato, agora()))
                    flash("Fornecedor cadastrado.", "ok")
                db.commit()
            except sqlite3.IntegrityError:
                flash("Já existe um fornecedor com este nome.", "erro")
        return redirect(url_for("fornecedores"))
    lista = db.execute(
        "SELECT f.*, (SELECT COUNT(*) FROM descontos d WHERE d.fornecedor_id = f.id) AS qtd"
        " FROM fornecedores f ORDER BY f.ativo DESC, f.nome"
    ).fetchall()
    return render_template("fornecedores.html", fornecedores=lista, pode_editar=pode_editar)


# ---------------------------------------------------------------------------
# Usuários (somente administrador)
# ---------------------------------------------------------------------------
@app.route("/usuarios")
@somente_admin
def usuarios():
    lista = get_db().execute("SELECT * FROM usuarios ORDER BY ativo DESC, nome").fetchall()
    return render_template("usuarios.html", usuarios=lista, ativos=total_usuarios_ativos())


@app.route("/usuarios/novo", methods=["GET", "POST"])
@app.route("/usuarios/<int:id>/editar", methods=["GET", "POST"])
@somente_admin
def usuario_form(id=None):
    db = get_db()
    u = None
    if id is not None:
        u = db.execute("SELECT * FROM usuarios WHERE id = ?", (id,)).fetchone()
        if u is None:
            abort(404)
    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        email = request.form.get("email", "").strip().lower()
        perfil = request.form.get("perfil", "")
        ativo = 1 if request.form.get("ativo") == "1" else 0
        senha = request.form.get("senha", "")
        erros = []
        if not nome or not email:
            erros.append("Informe nome e e-mail.")
        if perfil not in PERFIS:
            erros.append("Perfil inválido.")
        if u is None and len(senha) < 8:
            erros.append("Defina uma senha inicial com no mínimo 8 caracteres.")
        if u is not None and senha and len(senha) < 8:
            erros.append("A nova senha deve ter no mínimo 8 caracteres.")
        # limite de usuários ativos
        ativos = total_usuarios_ativos()
        passara_a_ativo = ativo and (u is None or not u["ativo"])
        if passara_a_ativo and ativos >= MAX_USUARIOS:
            erros.append(f"Limite de {MAX_USUARIOS} usuários ativos atingido. Desative um usuário antes.")
        # o administrador logado não pode se rebaixar nem se desativar
        if u is not None and u["id"] == g.usuario["id"] and (perfil != "admin" or not ativo):
            erros.append("Você não pode remover seu próprio acesso de administrador.")
        if erros:
            for e in erros:
                flash(e, "erro")
            return render_template("usuario_form.html", u=request.form, id=id)
        try:
            if u is None:
                db.execute("INSERT INTO usuarios (nome, email, senha_hash, perfil, ativo, criado_em)"
                           " VALUES (?, ?, ?, ?, ?, ?)",
                           (nome, email, generate_password_hash(senha), perfil, ativo, agora()))
                flash("Usuário criado.", "ok")
            else:
                db.execute("UPDATE usuarios SET nome=?, email=?, perfil=?, ativo=? WHERE id=?",
                           (nome, email, perfil, ativo, id))
                if senha:
                    db.execute("UPDATE usuarios SET senha_hash=? WHERE id=?", (generate_password_hash(senha), id))
                flash("Usuário atualizado.", "ok")
            db.commit()
        except sqlite3.IntegrityError:
            flash("Já existe um usuário com este e-mail.", "erro")
            return render_template("usuario_form.html", u=request.form, id=id)
        return redirect(url_for("usuarios"))
    dados = dict(u) if u else {"ativo": 1, "perfil": "qualidade"}
    return render_template("usuario_form.html", u=dados, id=id)


# ---------------------------------------------------------------------------
# Relatórios e indicadores (planejado - próxima versão)
# ---------------------------------------------------------------------------
@app.route("/relatorios")
@login_obrigatorio
def relatorios():
    return render_template("relatorios.html")


@app.errorhandler(403)
def proibido(_e):
    return render_template("erro.html", titulo="Acesso negado",
                           msg="Seu perfil não tem permissão para esta ação."), 403


@app.errorhandler(404)
def nao_encontrado(_e):
    return render_template("erro.html", titulo="Não encontrado",
                           msg="O registro solicitado não existe."), 404


def ip_local():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("8.8.8.8", 80))
            return sk.getsockname()[0]
    except OSError:
        return "127.0.0.1"


init_db()

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    porta = int(os.environ.get("PORT", 5000))
    print("=" * 64)
    print(" Controle de Descontos - Fornecedores do Paquistão")
    print(f"  Neste computador:   http://localhost:{porta}")
    if host == "0.0.0.0":
        print(f"  Outros na rede:     http://{ip_local()}:{porta}")
    print("  Para encerrar, feche esta janela (ou Ctrl+C).")
    print("=" * 64)
    try:
        from waitress import serve
        serve(app, host=host, port=porta, threads=8)
    except ImportError:
        app.run(host=host, port=porta, debug=os.environ.get("DEBUG") == "1")
