"""
Controle de Descontos - Fornecedores do Paquistão

Fluxo:
  1. Administrador -> cadastra o desconto após inspeção do invoice (invoice de origem);
                      a partir do cadastro o valor já é considerado segurado
  2. Importação    -> registra o(s) novo(s) invoice(s) firmado(s) com a dedução do valor.
                      O desconto pode ser dividido em várias deduções (ex.: 10.000 = 4.000 + 6.000)
  3. Importação    -> confirma a chegada de cada invoice deduzido na fábrica
  4. Financeiro    -> paga (total ou parcialmente) a parte correspondente do valor segurado

Todos os valores são em dólar (USD).
"""
import glob
import os
import re
import socket
import subprocess
import secrets
import sqlite3
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
DB_PATH = os.environ.get("DESCONTOS_DB", os.path.join(INSTANCE_DIR, "descontos.db"))
MAX_USUARIOS = 5
VERSAO = "1.9"

# ---------------------------------------------------------------------------
# Perfis e fluxo
# ---------------------------------------------------------------------------
PERFIS = {
    "admin": "Administrador",
    "importacao": "Importação",
    "financeiro": "Financeiro",
    "consulta": "Consulta (somente leitura)",
}
# perfis da versão anterior -> perfis atuais (aplicado automaticamente ao iniciar)
PERFIS_ANTIGOS = {"qualidade": "consulta", "compras": "importacao", "recebimento": "importacao"}

STATUS = {
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
    "cadastrar": {"titulo": "Cadastrar desconto", "perfil": "admin"},
    "deduzir": {"titulo": "Registrar dedução em invoice", "perfil": "importacao"},
    "receber": {"titulo": "Confirmar chegada na fábrica", "perfil": "importacao"},
    "pagar": {"titulo": "Pagar valor segurado", "perfil": "financeiro"},
    "pagar_diferenca": {"titulo": "Pagar diferença ao fornecedor", "perfil": "financeiro"},
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
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Em servidor com HTTPS (ex.: PythonAnywhere), o cookie de sessão só trafega criptografado
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("HTTPS") == "1"

MAX_TENTATIVAS_LOGIN = 5
BLOQUEIO_MINUTOS = 15

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
    -- Etapa 1: cadastro (Administrador)
    invoice_origem TEXT NOT NULL,
    data_envio_relatorio TEXT,
    valor_invoice_origem REAL,
    valor_desconto REAL NOT NULL,
    motivo TEXT,  -- observações
    registrado_por INTEGER REFERENCES usuarios(id),
    registrado_em TEXT,
    -- Valor segurado no cadastro. valor_retido = valor inicial segurado (nunca muda);
    -- valor_segurado = total segurado (aumenta se o valor for revalidado para cima antes de qualquer pagamento);
    -- valor_desconto = valor final (pode ser revalidado), usado nas etapas seguintes
    valor_retido REAL,
    valor_segurado REAL,
    data_retencao TEXT,
    obs_retencao TEXT,
    retido_por INTEGER REFERENCES usuarios(id),
    retido_em TEXT,
    -- Cancelamento (Administrador)
    motivo_cancelamento TEXT,
    cancelado_por INTEGER REFERENCES usuarios(id),
    cancelado_em TEXT
);
-- Etapas 2 a 4: cada desconto pode ser abatido em um ou mais invoices
CREATE TABLE IF NOT EXISTS deducoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    desconto_id INTEGER NOT NULL REFERENCES descontos(id),
    status TEXT NOT NULL,
    -- Etapa 2: dedução em novo invoice (Importação)
    invoice TEXT NOT NULL,
    data_invoice TEXT,
    valor_invoice REAL,
    valor_deduzido REAL NOT NULL,
    obs_deducao TEXT,
    deduzido_por INTEGER REFERENCES usuarios(id),
    deduzido_em TEXT,
    -- Etapa 3: chegada do invoice deduzido (Importação)
    data_chegada TEXT,
    obs_chegada TEXT,
    recebido_por INTEGER REFERENCES usuarios(id),
    recebido_em TEXT,
    -- Etapa 4: pagamento da parte correspondente do valor segurado (Financeiro)
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
-- Revalidações do valor do desconto (somente administrador)
CREATE TABLE IF NOT EXISTS revisoes_valor (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    desconto_id INTEGER NOT NULL REFERENCES descontos(id),
    valor_anterior REAL NOT NULL,
    valor_novo REAL NOT NULL,
    motivo TEXT NOT NULL,
    usuario_id INTEGER REFERENCES usuarios(id),
    criado_em TEXT NOT NULL
);
-- Etapa 4: pagamentos (totais ou parciais) do valor segurado referente a cada dedução (Financeiro)
CREATE TABLE IF NOT EXISTS pagamentos_deducao (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deducao_id INTEGER NOT NULL REFERENCES deducoes(id),
    valor REAL NOT NULL,
    data_pagamento TEXT NOT NULL,
    ref_pagamento TEXT NOT NULL,
    obs TEXT,
    pago_por INTEGER REFERENCES usuarios(id),
    pago_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pagamentos_deducao ON pagamentos_deducao(deducao_id);
-- Pagamento ao fornecedor da diferença quando o valor final fica menor que o valor segurado
CREATE TABLE IF NOT EXISTS pagamentos_diferenca (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    desconto_id INTEGER NOT NULL REFERENCES descontos(id),
    valor REAL NOT NULL,
    data_pagamento TEXT NOT NULL,
    ref_pagamento TEXT,
    obs TEXT,
    pago_por INTEGER REFERENCES usuarios(id),
    pago_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tentativas_login (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
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
        # versão 1.5: valor inicial segurado separado do valor final
        if "valor_retido" not in colunas:
            db.execute("ALTER TABLE descontos ADD COLUMN valor_retido REAL")
        # versão 1.9: não existe mais a etapa de retenção; o valor é segurado no cadastro
        db.execute("UPDATE descontos SET retido_em = registrado_em, retido_por = registrado_por,"
                   " data_retencao = substr(registrado_em, 1, 10), status = 'AGUARDANDO_DEDUCAO'"
                   " WHERE status = 'AGUARDANDO_RETENCAO'")
        db.execute("UPDATE descontos SET valor_retido = valor_desconto"
                   " WHERE retido_em IS NOT NULL AND valor_retido IS NULL")
        # versão 1.6: total segurado separado do valor inicial segurado
        if "valor_segurado" not in colunas:
            db.execute("ALTER TABLE descontos ADD COLUMN valor_segurado REAL")
        db.execute("UPDATE descontos SET valor_segurado = valor_retido"
                   " WHERE valor_retido IS NOT NULL AND valor_segurado IS NULL")
        # versão 1.8: pagamentos parciais. Deduções pagas antes viram um pagamento integral.
        db.execute(
            "INSERT INTO pagamentos_deducao (deducao_id, valor, data_pagamento, ref_pagamento, obs, pago_por, pago_em)"
            " SELECT x.id, x.valor_deduzido, COALESCE(x.data_pagamento, substr(x.pago_em, 1, 10)),"
            " COALESCE(x.ref_pagamento, '—'), x.obs_pagamento, x.pago_por, COALESCE(x.pago_em, datetime('now'))"
            " FROM deducoes x WHERE x.status = 'PAGO'"
            " AND NOT EXISTS (SELECT 1 FROM pagamentos_deducao p WHERE p.deducao_id = x.id)")
        for antigo, novo in PERFIS_ANTIGOS.items():
            db.execute("UPDATE usuarios SET perfil = ? WHERE perfil = ?", (novo, antigo))


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
    codigo_desconto=codigo_desconto, MAX_USUARIOS=MAX_USUARIOS, VERSAO=VERSAO,
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
ARQ_CODIGO_INSTALACAO = os.path.join(INSTANCE_DIR, "codigo_instalacao.txt")


def codigo_instalacao():
    """Código exigido para criar o primeiro administrador (impede que outra pessoa o crie)."""
    os.makedirs(INSTANCE_DIR, exist_ok=True)
    if not os.path.exists(ARQ_CODIGO_INSTALACAO):
        with open(ARQ_CODIGO_INSTALACAO, "w") as f:
            f.write(f"{secrets.randbelow(10**6):06d}")
    with open(ARQ_CODIGO_INSTALACAO) as f:
        return f.read().strip()


def sem_usuarios():
    return get_db().execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] == 0


@app.route("/configuracao-inicial", methods=["GET", "POST"])
def setup():
    if not sem_usuarios():
        return redirect(url_for("login"))
    codigo_instalacao()  # garante que o código exista
    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        email = request.form.get("email", "").strip().lower()
        senha = request.form.get("senha", "")
        if not secrets.compare_digest(request.form.get("codigo", "").strip(), codigo_instalacao()):
            flash("Código de instalação incorreto.", "erro")
        elif not nome or not email or len(senha) < 8:
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
            os.remove(ARQ_CODIGO_INSTALACAO)
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
        db = get_db()
        limite = (datetime.now() - timedelta(minutes=BLOQUEIO_MINUTOS)).strftime("%Y-%m-%d %H:%M:%S")
        falhas = db.execute("SELECT COUNT(*) FROM tentativas_login WHERE email = ? AND criado_em > ?",
                            (email, limite)).fetchone()[0]
        if falhas >= MAX_TENTATIVAS_LOGIN:
            flash(f"Muitas tentativas incorretas. Aguarde {BLOQUEIO_MINUTOS} minutos ou peça ao administrador"
                  " para redefinir sua senha.", "erro")
            return render_template("login.html")
        u = db.execute("SELECT * FROM usuarios WHERE email = ?", (email,)).fetchone()
        if u and u["ativo"] and check_password_hash(u["senha_hash"], senha):
            db.execute("DELETE FROM tentativas_login WHERE email = ?", (email,))
            db.commit()
            session.clear()
            session["usuario_id"] = u["id"]
            destino = request.args.get("next", "")
            if not destino.startswith("/") or destino.startswith("//"):
                destino = url_for("painel")
            return redirect(destino)
        db.execute("INSERT INTO tentativas_login (email, criado_em) VALUES (?, ?)", (email, agora()))
        db.execute("DELETE FROM tentativas_login WHERE criado_em < ?", (limite,))
        db.commit()
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
    COALESCE((SELECT SUM(p.valor) FROM pagamentos_deducao p JOIN deducoes x ON x.id = p.deducao_id
              WHERE x.desconto_id = d.id), 0) AS total_pago,
    (SELECT GROUP_CONCAT(x.invoice, ', ') FROM deducoes x WHERE x.desconto_id = d.id) AS invoices_deducao,
    COALESCE((SELECT SUM(p.valor) FROM pagamentos_diferenca p WHERE p.desconto_id = d.id), 0) AS diferenca_paga
FROM descontos d JOIN fornecedores f ON f.id = d.fornecedor_id
"""

# Diferença segurada a mais (valor inicial segurado - valor final) que ainda não foi paga ao fornecedor
SQL_DIFERENCA_PENDENTE = (
    "(d.valor_segurado IS NOT NULL AND d.valor_segurado - d.valor_desconto"
    " - COALESCE((SELECT SUM(p.valor) FROM pagamentos_diferenca p WHERE p.desconto_id = d.id), 0) > 0.005)"
)

# Filtros do painel. "Aguardando chegada/pagamento" considera as deduções do desconto.
FILTROS_STATUS = {
    "ABERTOS": "d.status NOT IN ('CONCLUIDO', 'CANCELADO')",
    "AGUARDANDO_DEDUCAO": "d.status = 'AGUARDANDO_DEDUCAO'",
    "AGUARDANDO_CHEGADA": "d.status <> 'CANCELADO' AND EXISTS (SELECT 1 FROM deducoes x"
                          " WHERE x.desconto_id = d.id AND x.status = 'AGUARDANDO_CHEGADA')",
    "AGUARDANDO_PAGAMENTO": "d.status <> 'CANCELADO' AND (EXISTS (SELECT 1 FROM deducoes x"
                            " WHERE x.desconto_id = d.id AND x.status = 'AGUARDANDO_PAGAMENTO')"
                            f" OR {SQL_DIFERENCA_PENDENTE})",
    "CONCLUIDO": "d.status = 'CONCLUIDO'",
    "CANCELADO": "d.status = 'CANCELADO'",
}


def saldo_a_deduzir(d):
    return round(d["valor_desconto"] - d["total_deduzido"], 2)


def diferenca_pendente(d):
    """Parte do valor segurado que ficou acima do valor final e ainda não foi paga ao fornecedor."""
    if d["valor_segurado"] is None:
        return 0
    return max(round(d["valor_segurado"] - d["valor_desconto"] - d["diferenca_paga"], 2), 0)


def tem_pagamento(d):
    """Algum pagamento do valor segurado já foi registrado (parcelas ou diferença)?"""
    return d["total_pago"] > TOLERANCIA or d["diferenca_paga"] > TOLERANCIA


def atualizar_status(desconto_id):
    """Recalcula o status do desconto a partir das deduções e dos pagamentos."""
    db = get_db()
    d = db.execute(SQL_LISTA + " WHERE d.id = ?", (desconto_id,)).fetchone()
    if d["status"] == "CANCELADO":
        return d["status"]
    pendentes = {r["status"] for r in db.execute(
        "SELECT DISTINCT status FROM deducoes WHERE desconto_id = ? AND status <> 'PAGO'", (desconto_id,))}
    if saldo_a_deduzir(d) > TOLERANCIA:
        novo = "AGUARDANDO_DEDUCAO"
    elif "AGUARDANDO_CHEGADA" in pendentes:
        novo = "AGUARDANDO_CHEGADA"
    elif "AGUARDANDO_PAGAMENTO" in pendentes or diferenca_pendente(d) > TOLERANCIA:
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
        registrar_historico(desconto_id, "Processo concluído", obs="Todo o valor segurado foi deduzido e pago.")
    return novo


def listar_pendencias():
    """Ações pendentes para o perfil do usuário logado."""
    db = get_db()
    pend = []
    ativos = "d.status NOT IN ('CANCELADO', 'CONCLUIDO')"
    if pode(ETAPAS["deduzir"]["perfil"]):
        for d in db.execute(SQL_LISTA + " WHERE d.status = 'AGUARDANDO_DEDUCAO' ORDER BY d.id"):
            pend.append({"d": d, "acao": "Deduzir saldo em invoice", "invoice": None, "valor": saldo_a_deduzir(d)})
    for etapa, status in (("receber", "AGUARDANDO_CHEGADA"), ("pagar", "AGUARDANDO_PAGAMENTO")):
        if not pode(ETAPAS[etapa]["perfil"]):
            continue
        linhas = db.execute(
            "SELECT x.id AS deducao_id, x.invoice, x.valor_deduzido, d.id,"
            " COALESCE((SELECT SUM(p.valor) FROM pagamentos_deducao p WHERE p.deducao_id = x.id), 0) AS pago"
            f" FROM deducoes x JOIN descontos d ON d.id = x.desconto_id WHERE x.status = ? AND {ativos} ORDER BY x.id",
            (status,)).fetchall()
        for x in linhas:
            d = db.execute(SQL_LISTA + " WHERE d.id = ?", (x["id"],)).fetchone()
            acao = ETAPAS[etapa]["titulo"] + (" (restante)" if x["pago"] > TOLERANCIA else "")
            pend.append({"d": d, "acao": acao, "invoice": x["invoice"],
                         "valor": round(x["valor_deduzido"] - x["pago"], 2)})
    if pode(ETAPAS["pagar_diferenca"]["perfil"]):
        for d in db.execute(SQL_LISTA + f" WHERE {ativos} AND {SQL_DIFERENCA_PENDENTE} ORDER BY d.id"):
            pend.append({"d": d, "acao": ETAPAS["pagar_diferenca"]["titulo"], "invoice": None,
                         "valor": diferenca_pendente(d)})
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
    for chave in ("AGUARDANDO_DEDUCAO", "CONCLUIDO"):
        contagem[chave] = db.execute(
            f"SELECT COUNT(*) FROM descontos d WHERE {FILTROS_STATUS[chave]}").fetchone()[0]
    for chave in ("AGUARDANDO_CHEGADA", "AGUARDANDO_PAGAMENTO"):
        contagem[chave] = db.execute(
            "SELECT COUNT(*) FROM deducoes x JOIN descontos d ON d.id = x.desconto_id"
            " WHERE x.status = ? AND d.status <> 'CANCELADO'", (chave,)).fetchone()[0]
    contagem["AGUARDANDO_PAGAMENTO"] += db.execute(
        f"SELECT COUNT(*) FROM descontos d WHERE d.status <> 'CANCELADO' AND {SQL_DIFERENCA_PENDENTE}").fetchone()[0]

    fornecedores = db.execute("SELECT id, nome FROM fornecedores ORDER BY nome").fetchall()
    return render_template(
        "painel.html", descontos=descontos, pendencias=listar_pendencias(), contagem=contagem,
        fornecedores=fornecedores, filtro_status=filtro_status,
        filtro_fornecedor=filtro_fornecedor, busca=busca,
    )


# ---------------------------------------------------------------------------
# Cadastro / edição do desconto (Etapa 1 - Administrador)
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
    if not pode(ETAPAS["cadastrar"]["perfil"]):
        abort(403)
    db = get_db()
    fornecedores = db.execute("SELECT * FROM fornecedores WHERE ativo = 1 ORDER BY nome").fetchall()
    if request.method == "POST":
        dados, erros = validar_form_desconto()
        if not erros:
            # o valor é considerado segurado a partir do cadastro
            ts = agora()
            cur = db.execute(
                "INSERT INTO descontos (fornecedor_id, status, invoice_origem, data_envio_relatorio,"
                " valor_invoice_origem, valor_desconto, valor_retido, valor_segurado, motivo, registrado_por,"
                " registrado_em, data_retencao, retido_por, retido_em)"
                " VALUES (?, 'AGUARDANDO_DEDUCAO', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (obter_fornecedor(dados["fornecedor"]), dados["invoice_origem"], dados["data_envio_relatorio"],
                 dados["valor_invoice_origem"], dados["valor_desconto"], dados["valor_desconto"],
                 dados["valor_desconto"], dados["motivo"], g.usuario["id"], ts, ts[:10], g.usuario["id"], ts),
            )
            registrar_historico(cur.lastrowid, f"Desconto cadastrado · {fmt_moeda(dados['valor_desconto'])} segurado"
                                f" no invoice {dados['invoice_origem']}", None, "AGUARDANDO_DEDUCAO")
            db.commit()
            flash("Desconto cadastrado e valor segurado. Aguardando dedução pela Importação.", "ok")
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
    if d["status"] in ("CONCLUIDO", "CANCELADO") or not pode(ETAPAS["cadastrar"]["perfil"]):
        abort(403)
    # O valor só pode ser corrigido aqui enquanto nada aconteceu com ele (sem revalidação, dedução ou
    # pagamento); depois disso, qualquer mudança de valor é feita pela revalidação.
    valor_editavel = (not d["invoices_deducao"] and not d["diferenca_paga"] and not db.execute(
        "SELECT 1 FROM revisoes_valor WHERE desconto_id = ?", (id,)).fetchone())
    fornecedores = db.execute("SELECT * FROM fornecedores WHERE ativo = 1 ORDER BY nome").fetchall()
    if request.method == "POST":
        dados, erros = validar_form_desconto()
        if not valor_editavel:
            dados["valor_desconto"] = d["valor_desconto"]
            erros = [e for e in erros if "desconto" not in e.lower()]
        if not erros:
            db.execute(
                "UPDATE descontos SET fornecedor_id=?, invoice_origem=?, data_envio_relatorio=?,"
                " valor_invoice_origem=?, valor_desconto=?, valor_retido=?, valor_segurado=?, motivo=? WHERE id=?",
                (obter_fornecedor(dados["fornecedor"]), dados["invoice_origem"], dados["data_envio_relatorio"],
                 dados["valor_invoice_origem"], dados["valor_desconto"],
                 dados["valor_desconto"] if valor_editavel else d["valor_retido"],
                 dados["valor_desconto"] if valor_editavel else d["valor_segurado"], dados["motivo"], id),
            )
            obs = None
            if dados["valor_desconto"] != d["valor_desconto"]:
                obs = (f"Valor corrigido de {fmt_moeda(d['valor_desconto'])} para "
                       f"{fmt_moeda(dados['valor_desconto'])} (valor segurado atualizado)")
            registrar_historico(id, "Registro editado", obs=obs)
            db.commit()
            flash("Desconto atualizado.", "ok")
            return redirect(url_for("desconto_detalhe", id=id))
        for e in erros:
            flash(e, "erro")
        return render_template("desconto_form.html", fornecedores=fornecedores, d=dados, editando=True, id=id,
                               valor_editavel=valor_editavel)
    return render_template("desconto_form.html", fornecedores=fornecedores, d=dict(d), editando=True, id=id,
                           valor_editavel=valor_editavel)


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
    deducoes = db.execute(
        "SELECT x.*, COALESCE((SELECT SUM(p.valor) FROM pagamentos_deducao p WHERE p.deducao_id = x.id), 0) AS pago"
        " FROM deducoes x WHERE x.desconto_id = ? ORDER BY x.id", (id,)).fetchall()
    pagamentos = {}
    for p in db.execute(
            "SELECT p.*, u.nome AS usuario FROM pagamentos_deducao p JOIN deducoes x ON x.id = p.deducao_id"
            " LEFT JOIN usuarios u ON u.id = p.pago_por WHERE x.desconto_id = ? ORDER BY p.id", (id,)):
        pagamentos.setdefault(p["deducao_id"], []).append(p)
    historico = db.execute(
        "SELECT h.*, u.nome AS usuario FROM historico h LEFT JOIN usuarios u ON u.id = h.usuario_id"
        " WHERE h.desconto_id = ? ORDER BY h.id", (id,)
    ).fetchall()
    nomes = {u["id"]: u["nome"] for u in db.execute("SELECT id, nome FROM usuarios")}
    revisoes = db.execute(
        "SELECT r.*, u.nome AS usuario FROM revisoes_valor r LEFT JOIN usuarios u ON u.id = r.usuario_id"
        " WHERE r.desconto_id = ? ORDER BY r.id", (id,)).fetchall()
    pagamentos_dif = db.execute(
        "SELECT p.*, u.nome AS usuario FROM pagamentos_diferenca p LEFT JOIN usuarios u ON u.id = p.pago_por"
        " WHERE p.desconto_id = ? ORDER BY p.id", (id,)).fetchall()
    pago_total = round(d["total_pago"] + d["diferenca_paga"], 2)
    resumo = {
        "deduzido": d["total_deduzido"],
        "saldo": saldo_a_deduzir(d),
        "pago": pago_total,
        "retido_aberto": round(d["valor_segurado"] - pago_total, 2) if d["valor_segurado"] is not None else 0,
        "diferenca": round(d["valor_segurado"] - d["valor_desconto"], 2) if d["valor_segurado"] is not None else 0,
        "diferenca_pendente": diferenca_pendente(d),
        "minimo_revalidacao": d["total_deduzido"],
        "tem_pagamento": tem_pagamento(d),
        "maximo_revalidacao": round(d["valor_segurado"] - d["diferenca_paga"], 2) if d["valor_segurado"] is not None else None,
        "n": len(deducoes),
        "chegaram": sum(1 for x in deducoes if x["status"] != "AGUARDANDO_CHEGADA"),
        "pagas": sum(1 for x in deducoes if x["status"] == "PAGO"),
    }
    return render_template("desconto_detalhe.html", d=d, deducoes=deducoes, pagamentos=pagamentos, revisoes=revisoes,
                           pagamentos_dif=pagamentos_dif, historico=historico, nomes=nomes, resumo=resumo,
                           hoje=datetime.now().strftime("%Y-%m-%d"))


# ---------------------------------------------------------------------------
# Revalidação do valor (somente administrador)
# O valor inicial segurado (valor_retido) é preservado; o valor final (valor_desconto)
# passa a valer para as etapas seguintes. O valor final pode ficar acima do segurado
# somente enquanto nenhum pagamento foi registrado: nesse caso o valor adicional também
# é segurado (valor_segurado acompanha o valor final).
# ---------------------------------------------------------------------------
@app.route("/descontos/<int:id>/revalidar", methods=["POST"])
@somente_admin
def desconto_revalidar(id):
    d = carregar_desconto(id)
    volta = redirect(url_for("desconto_detalhe", id=id))
    if d["valor_retido"] is None or d["status"] in ("CONCLUIDO", "CANCELADO"):
        flash("O valor não pode mais ser revalidado: o desconto já foi concluído ou cancelado.", "erro")
        return volta
    novo = parse_num(request.form.get("valor_final"))
    novo = round(novo, 2) if novo is not None else None
    motivo = request.form.get("motivo", "").strip()
    maximo = round(d["valor_segurado"] - d["diferenca_paga"], 2)
    aumento = novo is not None and novo > d["valor_segurado"] + TOLERANCIA
    erro = None
    if novo is None or novo < 0:
        erro = "Informe o valor final do desconto (use 0 para zerar o desconto)."
    elif not motivo:
        erro = "Informe o motivo da revalidação."
    elif abs(novo - d["valor_desconto"]) <= TOLERANCIA:
        erro = "O valor final informado é igual ao valor atual."
    elif novo < d["total_deduzido"] - TOLERANCIA:
        erro = (f"O valor final não pode ser menor que o total já deduzido em invoices "
                f"({fmt_moeda(d['total_deduzido'])}). Exclua deduções antes, se necessário.")
    elif aumento and tem_pagamento(d):
        erro = (f"Já existe pagamento registrado para este desconto, então o Financeiro não tem como segurar valor"
                f" adicional. O valor final pode ser no máximo {fmt_moeda(maximo)}.")
    elif aumento and request.form.get("confirmo_segurar") != "1":
        erro = (f"O novo valor é maior que o segurado ({fmt_moeda(d['valor_segurado'])}). Marque a confirmação de que"
                f" o invoice {d['invoice_origem']} ainda não foi pago e o valor adicional também será segurado.")
    elif not aumento and novo > maximo + TOLERANCIA:
        erro = f"O valor final não pode ser maior que {fmt_moeda(maximo)} (valor segurado menos a diferença já paga)."
    if erro:
        flash(erro, "erro")
        return volta
    db = get_db()
    db.execute("INSERT INTO revisoes_valor (desconto_id, valor_anterior, valor_novo, motivo, usuario_id, criado_em)"
               " VALUES (?, ?, ?, ?, ?, ?)", (id, d["valor_desconto"], novo, motivo, g.usuario["id"], agora()))
    db.execute("UPDATE descontos SET valor_desconto = ? WHERE id = ?", (novo, id))
    acao = f"Valor revalidado · de {fmt_moeda(d['valor_desconto'])} para {fmt_moeda(novo)}"
    if novo == 0:
        acao += " · desconto zerado"
    if aumento:
        db.execute("UPDATE descontos SET valor_segurado = ? WHERE id = ?", (novo, id))
        acao += f" · total segurado aumentado de {fmt_moeda(d['valor_segurado'])} para {fmt_moeda(novo)}"
    registrar_etapa(id, acao, d["status"], motivo)
    db.commit()
    dif = round(max(d["valor_segurado"], novo) - novo - d["diferenca_paga"], 2)
    if aumento:
        flash(f"Valor revalidado. O valor adicional de {fmt_moeda(novo - d['valor_segurado'])} também deve ser"
              f" segurado no invoice {d['invoice_origem']}.", "ok")
    elif novo == 0 and dif > TOLERANCIA:
        flash(f"Desconto zerado. O Financeiro deve devolver ao fornecedor o valor segurado de {fmt_moeda(dif)};"
              " o processo será concluído após esse pagamento.", "ok")
    elif novo == 0:
        flash("Desconto zerado. Processo concluído.", "ok")
    elif dif > TOLERANCIA:
        flash(f"Valor revalidado. A diferença de {fmt_moeda(dif)} ficou pendente de pagamento pelo Financeiro.", "ok")
    else:
        flash("Valor revalidado.", "ok")
    return volta


@app.route("/descontos/<int:id>/pagar-diferenca", methods=["POST"])
@login_obrigatorio
def desconto_pagar_diferenca(id):
    if not pode(ETAPAS["pagar_diferenca"]["perfil"]):
        abort(403)
    d = carregar_desconto(id)
    volta = redirect(url_for("desconto_detalhe", id=id))
    valor = diferenca_pendente(d)
    if d["status"] == "CANCELADO" or valor <= TOLERANCIA:
        flash("Não há diferença pendente de pagamento.", "erro")
        return volta
    dados, erro = ler_pagamento(valor)
    if erro:
        flash(erro, "erro")
        return volta
    obs = request.form.get("obs", "").strip() or None
    db = get_db()
    db.execute("INSERT INTO pagamentos_diferenca (desconto_id, valor, data_pagamento, ref_pagamento, obs, pago_por,"
               " pago_em) VALUES (?, ?, ?, ?, ?, ?, ?)",
               (id, dados["valor"], dados["data"], dados["ref"], obs, g.usuario["id"], agora()))
    restante = round(valor - dados["valor"], 2)
    acao = (f"Diferença paga ao fornecedor · {fmt_moeda(dados['valor'])} do invoice {d['invoice_origem']}"
            f" · Ref. {dados['ref']}")
    if restante > TOLERANCIA:
        acao += f" · parcial, restam {fmt_moeda(restante)}"
    registrar_etapa(id, acao, d["status"], obs)
    db.commit()
    if restante > TOLERANCIA:
        flash(f"Pagamento parcial de {fmt_moeda(dados['valor'])} registrado. Restam {fmt_moeda(restante)} da diferença.", "ok")
    else:
        flash(f"Pagamento da diferença de {fmt_moeda(dados['valor'])} registrado.", "ok")
    return volta


@app.route("/pagamentos-diferenca/<int:pid>/desfazer", methods=["POST"])
@somente_admin
def pagamento_diferenca_desfazer(pid):
    p = get_db().execute("SELECT * FROM pagamentos_diferenca WHERE id = ?", (pid,)).fetchone()
    if p is None:
        abort(404)
    d = carregar_desconto(p["desconto_id"])
    motivo = request.form.get("motivo", "").strip()
    if not motivo:
        flash("Informe o motivo para desfazer.", "erro")
    elif d["status"] == "CANCELADO":
        flash("Este desconto está cancelado.", "erro")
    else:
        db = get_db()
        db.execute("DELETE FROM pagamentos_diferenca WHERE id = ?", (pid,))
        registrar_etapa(d["id"], f"Pagamento da diferença desfeito · {fmt_moeda(p['valor'])}", d["status"], motivo)
        db.commit()
        flash("Pagamento da diferença desfeito.", "ok")
    return redirect(url_for("desconto_detalhe", id=d["id"]))


# ---------------------------------------------------------------------------
# Etapa 2 - Dedução em um ou mais invoices (Importação)
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
# Etapa 3 - Chegada do invoice deduzido (Importação)
# Etapa 4 - Pagamento da parte do valor segurado (Financeiro)
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
        ja_pago = db.execute("SELECT COALESCE(SUM(valor), 0) FROM pagamentos_deducao WHERE deducao_id = ?",
                             (pid,)).fetchone()[0]
        pendente = round(x["valor_deduzido"] - ja_pago, 2)
        dados, erro = ler_pagamento(pendente)
        if erro:
            flash(erro, "erro")
            return volta
        db.execute("INSERT INTO pagamentos_deducao (deducao_id, valor, data_pagamento, ref_pagamento, obs, pago_por,"
                   " pago_em) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (pid, dados["valor"], dados["data"], dados["ref"], obs, uid, ts))
        restante = round(pendente - dados["valor"], 2)
        if restante <= TOLERANCIA:
            db.execute("UPDATE deducoes SET status='PAGO' WHERE id=?", (pid,))
        acao = (f"Pagamento do valor segurado · {fmt_moeda(dados['valor'])} do invoice {d['invoice_origem']}"
                f" (referente à dedução no invoice {x['invoice']}) · Ref. {dados['ref']}")
        if restante > TOLERANCIA:
            acao += f" · parcial, restam {fmt_moeda(restante)}"
    registrar_etapa(d["id"], acao, d["status"], obs)
    db.commit()
    if etapa == "pagar" and restante > TOLERANCIA:
        flash(f"Pagamento parcial registrado. Restam {fmt_moeda(restante)} a pagar referente ao invoice {x['invoice']}.", "ok")
    else:
        flash(f"{ETAPAS[etapa]['titulo']}: concluído.", "ok")
    return volta


def ler_pagamento(pendente):
    """Valida data, valor (total ou parcial) e referência de um pagamento do Financeiro."""
    f = request.form
    valor = parse_num(f.get("valor_pago"))
    dados = {"data": f.get("data_pagamento"), "ref": f.get("ref_pagamento", "").strip(),
             "valor": round(valor, 2) if valor is not None else None}
    if not dados["data"]:
        return dados, "Informe a data do pagamento."
    if dados["valor"] is None or dados["valor"] <= 0:
        return dados, "Informe o valor pago."
    if dados["valor"] > pendente + TOLERANCIA:
        return dados, f"O valor pago ({fmt_moeda(dados['valor'])}) é maior que o valor pendente ({fmt_moeda(pendente)})."
    if not dados["ref"]:
        return dados, "Informe a referência do pagamento."
    return dados, None


# ---------------------------------------------------------------------------
# Ações do administrador
# ---------------------------------------------------------------------------
@app.route("/deducoes/<int:pid>/desfazer", methods=["POST"])
@somente_admin
def deducao_desfazer(pid):
    """Desfaz a última etapa da dedução (último pagamento -> chegada -> exclusão da dedução)."""
    x, d = carregar_deducao(pid)
    volta = redirect(url_for("desconto_detalhe", id=d["id"]))
    motivo = request.form.get("motivo", "").strip()
    if not motivo:
        flash("Informe o motivo para desfazer.", "erro")
        return volta
    db = get_db()
    ultimo = db.execute("SELECT * FROM pagamentos_deducao WHERE deducao_id = ? ORDER BY id DESC LIMIT 1",
                        (pid,)).fetchone()
    if ultimo:
        db.execute("DELETE FROM pagamentos_deducao WHERE id = ?", (ultimo["id"],))
        db.execute("UPDATE deducoes SET status='AGUARDANDO_PAGAMENTO' WHERE id=?", (pid,))
        acao = f"Pagamento desfeito · {fmt_moeda(ultimo['valor'])} (Ref. {ultimo['ref_pagamento']}) · invoice {x['invoice']}"
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


@app.route("/descontos/<int:id>/cancelar", methods=["POST"])
@somente_admin
def desconto_cancelar(id):
    d = carregar_desconto(id)
    motivo = request.form.get("motivo_cancelamento", "").strip()
    if d["status"] in ("CONCLUIDO", "CANCELADO"):
        flash("Este desconto já está encerrado.", "erro")
    elif d["invoices_deducao"]:
        flash("Este desconto já possui deduções. Exclua as deduções antes de cancelar.", "erro")
    elif d["diferenca_paga"]:
        flash("Este desconto já teve diferença paga ao fornecedor. Desfaça o pagamento antes de cancelar.", "erro")
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
    pode_editar = pode(ETAPAS["cadastrar"]["perfil"])
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
                    db.execute("DELETE FROM tentativas_login WHERE email = ?", (email,))
                flash("Usuário atualizado.", "ok")
            db.commit()
        except sqlite3.IntegrityError:
            flash("Já existe um usuário com este e-mail.", "erro")
            return render_template("usuario_form.html", u=request.form, id=id)
        return redirect(url_for("usuarios"))
    dados = dict(u) if u else {"ativo": 1, "perfil": "importacao"}
    return render_template("usuario_form.html", u=dados, id=id)


# ---------------------------------------------------------------------------
# Relatórios e indicadores (planejado - próxima versão)
# ---------------------------------------------------------------------------
@app.route("/relatorios")
@login_obrigatorio
def relatorios():
    return render_template("relatorios.html")


# ---------------------------------------------------------------------------
# Sistema: versão e atualização pelo navegador (somente administrador)
# ---------------------------------------------------------------------------
def git(*args):
    r = subprocess.run(["git", "-C", BASE_DIR, *args], capture_output=True, text=True, timeout=120)
    return r.returncode, (r.stdout + r.stderr).strip()


def arquivo_wsgi():
    """No PythonAnywhere, alterar o arquivo WSGI reinicia o sistema com o código novo."""
    if os.environ.get("WSGI_FILE"):
        return os.environ["WSGI_FILE"]
    encontrados = glob.glob("/var/www/*_wsgi.py")
    return encontrados[0] if encontrados else None


def backup_banco(sufixo=""):
    if not os.path.exists(DB_PATH):
        return None
    pasta = os.path.join(INSTANCE_DIR, "backups")
    os.makedirs(pasta, exist_ok=True)
    destino = os.path.join(pasta, f"descontos-{datetime.now():%Y%m%d-%H%M%S}{sufixo}.db")
    with sqlite3.connect(DB_PATH) as origem, sqlite3.connect(destino) as copia:
        origem.backup(copia)
    for antigo in sorted(f for f in os.listdir(pasta) if f.endswith(".db"))[:-30]:
        os.remove(os.path.join(pasta, antigo))
    return destino


@app.route("/sistema", methods=["GET", "POST"])
@somente_admin
def sistema():
    usa_git = os.path.isdir(os.path.join(BASE_DIR, ".git"))
    if request.method == "POST" and usa_git:
        antes = git("rev-parse", "HEAD")[1]
        cod, saida = git("fetch", "origin", "main")
        if cod != 0:
            flash(f"Não foi possível consultar o GitHub: {saida}", "erro")
            return redirect(url_for("sistema"))
        if git("rev-parse", "origin/main")[1] == antes:
            flash("O sistema já está na versão mais recente.", "ok")
            return redirect(url_for("sistema"))
        backup_banco("-antes-atualizacao")
        # o servidor não deve ter alterações locais no código: fica idêntico ao GitHub
        cod, saida = git("reset", "--hard", "origin/main")
        if cod != 0:
            flash(f"Falha ao atualizar: {saida}", "erro")
            return redirect(url_for("sistema"))
        wsgi = arquivo_wsgi()
        if wsgi:
            os.utime(wsgi, None)
            flash("Atualização instalada. O sistema está reiniciando: aguarde alguns segundos e recarregue a página.", "ok")
        else:
            flash("Atualização baixada. Feche e abra o sistema novamente para aplicá-la.", "ok")
        return redirect(url_for("sistema"))
    commit = git("log", "-1", "--format=%h · %cd · %s", "--date=format:%d/%m/%Y %H:%M")[1] if usa_git else None
    backups = sorted(os.listdir(os.path.join(INSTANCE_DIR, "backups")), reverse=True)[:10] \
        if os.path.isdir(os.path.join(INSTANCE_DIR, "backups")) else []
    return render_template("sistema.html", usa_git=usa_git, commit=commit, backups=backups)


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
    print(f" Controle de Descontos - Fornecedores do Paquistão · versão {VERSAO}")
    print(f"  Neste computador:   http://localhost:{porta}")
    if host == "0.0.0.0":
        print(f"  Outros na rede:     http://{ip_local()}:{porta}")
    print("  Para encerrar, feche esta janela (ou Ctrl+C).")
    with sqlite3.connect(DB_PATH) as _db:
        if _db.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] == 0:
            with app.app_context():
                print(f"  Código de instalação (primeiro acesso): {codigo_instalacao()}")
    print("=" * 64)
    try:
        from waitress import serve
        serve(app, host=host, port=porta, threads=8)
    except ImportError:
        app.run(host=host, port=porta, debug=os.environ.get("DEBUG") == "1")
