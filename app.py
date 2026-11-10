"""
Controle de Descontos - Fornecedores do Paquistão

Fluxo:
  1. Administrador -> cadastra o desconto após inspeção do invoice (invoice de origem);
                      a partir do cadastro o valor já é considerado segurado
  2. Importação    -> registra o(s) novo(s) invoice(s) firmado(s) com a dedução do valor.
                      O desconto pode ser dividido em várias deduções (ex.: 10.000 = 4.000 + 6.000)
  3. Importação    -> confirma a chegada de cada invoice deduzido na fábrica
  4. Financeiro    -> não usa o sistema: recebe e-mails automáticos a cada evento que exige
                      ação (segurar ou liberar valores) e confirma a ciência pelo link do e-mail

Todos os valores são em dólar (USD).
"""
import glob
import json
import os
import re
import secrets
import socket
import sqlite3
import subprocess
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, abort, flash, g, has_request_context, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import email_envio

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
DB_PATH = os.environ.get("DESCONTOS_DB", os.path.join(INSTANCE_DIR, "descontos.db"))
MAX_USUARIOS = 5
VERSAO = "2.0"

# ---------------------------------------------------------------------------
# Perfis e fluxo
# ---------------------------------------------------------------------------
PERFIS = {
    "admin": "Administrador",
    "importacao": "Importação",
    "consulta": "Consulta (somente leitura)",
}
# perfis de versões anteriores -> perfis atuais (aplicado automaticamente ao iniciar).
# Desde a versão 2.0 o Financeiro não usa o sistema: é avisado por e-mail.
PERFIS_ANTIGOS = {"qualidade": "consulta", "compras": "importacao", "recebimento": "importacao",
                  "financeiro": "consulta"}

STATUS = {
    "AGUARDANDO_DEDUCAO": "Aguardando dedução",
    "AGUARDANDO_CHEGADA": "Aguardando chegada",
    "AGUARDANDO_CIENCIA": "Aguardando ciência do Financeiro",
    "CONCLUIDO": "Concluído",
    "CANCELADO": "Cancelado",
}

# Status de cada dedução (parcela do desconto abatida em um invoice)
STATUS_DEDUCAO = {
    "AGUARDANDO_CHEGADA": "Aguardando chegada",
    "CHEGOU": "Chegou na fábrica",
}

# Etapas e perfil responsável
ETAPAS = {
    "cadastrar": {"titulo": "Cadastrar desconto", "perfil": "admin"},
    "deduzir": {"titulo": "Registrar dedução em invoice", "perfil": "importacao"},
    "receber": {"titulo": "Confirmar chegada na fábrica", "perfil": "importacao"},
}

# E-mails ao Financeiro: um por evento. Todos pedem ciência, exceto a anulação (correção).
NOTIFICACOES = {
    "SEGURAR": "Segurar valor do desconto",
    "SEGURAR_ADICIONAL": "Segurar valor adicional",
    "LIBERAR_CHEGADA": "Liberar valor segurado (invoice deduzido chegou)",
    "LIBERAR_DIFERENCA": "Liberar diferença ao fornecedor",
    "LIBERAR_CANCELAMENTO": "Liberar valor segurado (desconto cancelado)",
    "ANULACAO": "Correção: desconsiderar e-mail anterior",
}
ENVIO_STATUS = {
    "AGUARDANDO": "Aguardando envio",
    "ENVIADO": "Enviado",
    "FALHOU": "Falhou",
    "TESTE": "Modo de teste (não enviado)",
    "LEGADO": "Versão anterior",
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
    -- Pagamento registrado pelo Financeiro (somente versões anteriores à 2.0)
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
-- Pagamentos lançados pelo Financeiro nas versões anteriores à 2.0 (mantidos como histórico)
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
-- Pagamentos de diferença lançados nas versões anteriores à 2.0 (mantidos como histórico)
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
-- Configurações gerais (envio de e-mails etc.)
CREATE TABLE IF NOT EXISTS configuracoes (
    chave TEXT PRIMARY KEY,
    valor TEXT
);
-- Quem recebe os e-mails do Financeiro: principal (Para, confirma a ciência) ou copia (Cc)
CREATE TABLE IF NOT EXISTS destinatarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    tipo TEXT NOT NULL,
    ativo INTEGER NOT NULL DEFAULT 1,
    criado_em TEXT NOT NULL
);
-- Um registro por evento comunicado ao Financeiro (e-mail + ciência)
CREATE TABLE IF NOT EXISTS notificacoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    desconto_id INTEGER NOT NULL REFERENCES descontos(id),
    deducao_id INTEGER REFERENCES deducoes(id),
    tipo TEXT NOT NULL,
    valor REAL,
    detalhe TEXT,
    exige_ciencia INTEGER NOT NULL DEFAULT 1,
    referencia_id INTEGER REFERENCES notificacoes(id),
    token TEXT NOT NULL UNIQUE,
    criado_em TEXT NOT NULL,
    criado_por INTEGER REFERENCES usuarios(id),
    envio_status TEXT NOT NULL,
    tentativas INTEGER NOT NULL DEFAULT 0,
    ultimo_erro TEXT,
    enviado_em TEXT,
    enviado_para TEXT,
    enviado_cc TEXT,
    ciencia_em TEXT,
    ciencia_por TEXT,
    anulada_em TEXT,
    anulada_motivo TEXT
);
CREATE INDEX IF NOT EXISTS idx_notificacoes_desconto ON notificacoes(desconto_id);
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
        migrar_v2(db)


def migrar_v2(db):
    """Versão 2.0: o Financeiro deixa de lançar pagamentos e passa a receber e-mails com ciência.

    - Deduções que já chegaram (aguardando pagamento ou pagas) passam a "Chegou na fábrica".
    - Valores pagos na versão anterior viram notificações já confirmadas (registro histórico).
    - Valores que ainda aguardavam pagamento viram notificações "aguardando envio".
    """
    if db.execute("SELECT 1 FROM configuracoes WHERE chave = 'migracao_v2'").fetchone():
        return
    ts = agora()

    def notificacao(desconto_id, deducao_id, tipo, valor, legado_em=None):
        db.execute(
            "INSERT INTO notificacoes (desconto_id, deducao_id, tipo, valor, token, criado_em, envio_status,"
            " ciencia_em, ciencia_por) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (desconto_id, deducao_id, tipo, round(valor, 2), secrets.token_urlsafe(24), legado_em or ts,
             "LEGADO" if legado_em else "AGUARDANDO", legado_em,
             "Pagamento registrado no sistema (versão anterior)" if legado_em else None))

    deducoes = db.execute(
        "SELECT x.id, x.desconto_id, x.valor_deduzido, x.status, d.status AS status_desconto,"
        " COALESCE((SELECT SUM(p.valor) FROM pagamentos_deducao p WHERE p.deducao_id = x.id), 0),"
        " (SELECT MAX(p.pago_em) FROM pagamentos_deducao p WHERE p.deducao_id = x.id)"
        " FROM deducoes x JOIN descontos d ON d.id = x.desconto_id"
        " WHERE x.status IN ('AGUARDANDO_PAGAMENTO', 'PAGO')").fetchall()
    for pid, did, valor, _st, st_desc, pago, pago_em in deducoes:
        if pago > TOLERANCIA:
            notificacao(did, pid, "LIBERAR_CHEGADA", pago, legado_em=pago_em or ts)
        if valor - pago > TOLERANCIA and st_desc != "CANCELADO":
            notificacao(did, pid, "LIBERAR_CHEGADA", valor - pago)
        db.execute("UPDATE deducoes SET status = 'CHEGOU' WHERE id = ?", (pid,))
    for did, segurado, final, st_desc, pago, pago_em in db.execute(
            "SELECT d.id, d.valor_segurado, d.valor_desconto, d.status,"
            " COALESCE((SELECT SUM(p.valor) FROM pagamentos_diferenca p WHERE p.desconto_id = d.id), 0),"
            " (SELECT MAX(p.pago_em) FROM pagamentos_diferenca p WHERE p.desconto_id = d.id)"
            " FROM descontos d WHERE d.valor_segurado IS NOT NULL").fetchall():
        if pago > TOLERANCIA:
            notificacao(did, None, "LIBERAR_DIFERENCA", pago, legado_em=pago_em or ts)
        if segurado - final - pago > TOLERANCIA and st_desc != "CANCELADO":
            notificacao(did, None, "LIBERAR_DIFERENCA", segurado - final - pago)
    db.execute("UPDATE descontos SET status = 'AGUARDANDO_CIENCIA' WHERE status = 'AGUARDANDO_PAGAMENTO'")
    db.execute("INSERT INTO configuracoes (chave, valor) VALUES ('migracao_v2', ?)", (ts,))
    db.execute("INSERT OR IGNORE INTO configuracoes (chave, valor) VALUES ('email_modo', 'teste')")
    db.commit()
    with app.app_context():
        for (did,) in db.execute("SELECT id FROM descontos WHERE status NOT IN ('CANCELADO')").fetchall():
            atualizar_status(did)
        get_db().commit()


def agora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def registrar_historico(desconto_id, acao, status_anterior=None, status_novo=None, obs=None):
    get_db().execute(
        "INSERT INTO historico (desconto_id, usuario_id, acao, status_anterior, status_novo, observacao, criado_em)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (desconto_id, g.usuario["id"] if g.get("usuario") else None, acao, status_anterior, status_novo, obs,
         agora()),
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
    STATUS_DEDUCAO=STATUS_DEDUCAO, NOTIFICACOES=NOTIFICACOES, ENVIO_STATUS=ENVIO_STATUS, pode=pode,
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
    COALESCE((SELECT SUM(x.valor_deduzido) FROM deducoes x
              WHERE x.desconto_id = d.id AND x.status = 'CHEGOU'), 0) AS total_chegado,
    (SELECT GROUP_CONCAT(x.invoice, ', ') FROM deducoes x WHERE x.desconto_id = d.id) AS invoices_deducao,
    COALESCE((SELECT SUM(n.valor) FROM notificacoes n WHERE n.desconto_id = d.id
              AND n.tipo = 'LIBERAR_DIFERENCA' AND n.anulada_em IS NULL), 0) AS diferenca_solicitada,
    COALESCE((SELECT SUM(n.valor) FROM notificacoes n WHERE n.desconto_id = d.id
              AND n.tipo = 'LIBERAR_CANCELAMENTO' AND n.anulada_em IS NULL), 0) AS cancelamento_solicitado,
    (SELECT COUNT(*) FROM notificacoes n WHERE n.desconto_id = d.id AND n.exige_ciencia = 1
     AND n.ciencia_em IS NULL AND n.anulada_em IS NULL) AS ciencias_pendentes
FROM descontos d JOIN fornecedores f ON f.id = d.fornecedor_id
"""

SQL_CIENCIA_PENDENTE = ("n.exige_ciencia = 1 AND n.ciencia_em IS NULL AND n.anulada_em IS NULL")

# Filtros do painel. "Aguardando chegada/ciência" consideram as deduções e os e-mails do desconto.
FILTROS_STATUS = {
    "ABERTOS": "d.status NOT IN ('CONCLUIDO', 'CANCELADO')",
    "AGUARDANDO_DEDUCAO": "d.status = 'AGUARDANDO_DEDUCAO'",
    "AGUARDANDO_CHEGADA": "d.status <> 'CANCELADO' AND EXISTS (SELECT 1 FROM deducoes x"
                          " WHERE x.desconto_id = d.id AND x.status = 'AGUARDANDO_CHEGADA')",
    "AGUARDANDO_CIENCIA": f"EXISTS (SELECT 1 FROM notificacoes n WHERE n.desconto_id = d.id AND {SQL_CIENCIA_PENDENTE})",
    "CONCLUIDO": "d.status = 'CONCLUIDO'",
    "CANCELADO": "d.status = 'CANCELADO'",
}


def saldo_a_deduzir(d):
    return round(d["valor_desconto"] - d["total_deduzido"], 2)


def liberacao_solicitada(d):
    """Quanto do valor segurado já foi pedido ao Financeiro para liberar ao fornecedor."""
    return round(d["total_chegado"] + d["diferenca_solicitada"] + d["cancelamento_solicitado"], 2)


def tem_liberacao(d):
    """Já foi pedido ao Financeiro para liberar alguma parte do valor segurado?"""
    return liberacao_solicitada(d) > TOLERANCIA


def atualizar_status(desconto_id):
    """Recalcula o status do desconto a partir das deduções e da ciência do Financeiro."""
    db = get_db()
    d = db.execute(SQL_LISTA + " WHERE d.id = ?", (desconto_id,)).fetchone()
    if d["status"] == "CANCELADO":
        return d["status"]
    aguardando_chegada = db.execute(
        "SELECT 1 FROM deducoes WHERE desconto_id = ? AND status = 'AGUARDANDO_CHEGADA'", (desconto_id,)).fetchone()
    if saldo_a_deduzir(d) > TOLERANCIA:
        novo = "AGUARDANDO_DEDUCAO"
    elif aguardando_chegada:
        novo = "AGUARDANDO_CHEGADA"
    elif d["ciencias_pendentes"]:
        novo = "AGUARDANDO_CIENCIA"
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
        registrar_historico(desconto_id, "Processo concluído",
                            obs="Valor deduzido e o Financeiro deu ciência de todas as notificações.")
    return novo


def listar_pendencias():
    """Ações pendentes para o perfil do usuário logado."""
    db = get_db()
    pend = []
    if pode(ETAPAS["deduzir"]["perfil"]):
        for d in db.execute(SQL_LISTA + " WHERE d.status = 'AGUARDANDO_DEDUCAO' ORDER BY d.id"):
            pend.append({"d": d, "acao": "Deduzir saldo em invoice", "invoice": None, "valor": saldo_a_deduzir(d)})
    if pode(ETAPAS["receber"]["perfil"]):
        for x in db.execute(
                "SELECT x.invoice, x.valor_deduzido, x.desconto_id FROM deducoes x JOIN descontos d"
                " ON d.id = x.desconto_id WHERE x.status = 'AGUARDANDO_CHEGADA' AND d.status <> 'CANCELADO'"
                " ORDER BY x.id").fetchall():
            d = db.execute(SQL_LISTA + " WHERE d.id = ?", (x["desconto_id"],)).fetchone()
            pend.append({"d": d, "acao": ETAPAS["receber"]["titulo"], "invoice": x["invoice"],
                         "valor": x["valor_deduzido"]})
    if g.usuario["perfil"] == "admin":
        for n in db.execute(
                "SELECT n.*, x.invoice FROM notificacoes n LEFT JOIN deducoes x ON x.id = n.deducao_id"
                f" WHERE ({SQL_CIENCIA_PENDENTE}) OR n.envio_status IN ('FALHOU', 'AGUARDANDO') ORDER BY n.id"):
            d = db.execute(SQL_LISTA + " WHERE d.id = ?", (n["desconto_id"],)).fetchone()
            if n["envio_status"] in ("FALHOU", "AGUARDANDO"):
                acao = f"E-mail não enviado: {NOTIFICACOES[n['tipo']]}"
            else:
                acao = f"Aguardando ciência do Financeiro: {NOTIFICACOES[n['tipo']]}"
            pend.append({"d": d, "acao": acao, "invoice": n["invoice"], "valor": n["valor"]})
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
    contagem["AGUARDANDO_CHEGADA"] = db.execute(
        "SELECT COUNT(*) FROM deducoes x JOIN descontos d ON d.id = x.desconto_id"
        " WHERE x.status = 'AGUARDANDO_CHEGADA' AND d.status <> 'CANCELADO'").fetchone()[0]
    contagem["AGUARDANDO_CIENCIA"] = db.execute(
        f"SELECT COUNT(*) FROM notificacoes n WHERE {SQL_CIENCIA_PENDENTE}").fetchone()[0]

    fornecedores = db.execute("SELECT id, nome FROM fornecedores ORDER BY nome").fetchall()
    return render_template(
        "painel.html", descontos=descontos, pendencias=listar_pendencias(), contagem=contagem,
        fornecedores=fornecedores, filtro_status=filtro_status,
        filtro_fornecedor=filtro_fornecedor, busca=busca, alerta_email=alerta_email(),
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
            criar_notificacao(cur.lastrowid, "SEGURAR", dados["valor_desconto"])
            db.commit()
            flash("Desconto cadastrado e valor segurado. Aguardando dedução pela Importação.", "ok")
            enviar_novas()
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
    # Fornecedor, invoice e valor (o que foi informado ao Financeiro para segurar) só podem ser
    # corrigidos aqui enquanto nada aconteceu com o desconto (sem revalidação, dedução ou liberação).
    # Depois disso, mudanças de valor são feitas pela revalidação.
    valor_editavel = (not d["invoices_deducao"] and not tem_liberacao(d) and not db.execute(
        "SELECT 1 FROM revisoes_valor WHERE desconto_id = ?", (id,)).fetchone())
    fornecedores = db.execute("SELECT * FROM fornecedores WHERE ativo = 1 ORDER BY nome").fetchall()
    if request.method == "POST":
        dados, erros = validar_form_desconto()
        if not valor_editavel:
            dados.update(fornecedor=d["fornecedor"], invoice_origem=d["invoice_origem"],
                         valor_desconto=d["valor_desconto"])
            erros = [e for e in erros if "desconto" not in e.lower() and "fornecedor" not in e.lower()
                     and "invoice inspecionado" not in e.lower()]
        if not erros:
            fornecedor_id = obter_fornecedor(dados["fornecedor"])
            db.execute(
                "UPDATE descontos SET fornecedor_id=?, invoice_origem=?, data_envio_relatorio=?,"
                " valor_invoice_origem=?, valor_desconto=?, valor_retido=?, valor_segurado=?, motivo=? WHERE id=?",
                (fornecedor_id, dados["invoice_origem"], dados["data_envio_relatorio"],
                 dados["valor_invoice_origem"], dados["valor_desconto"],
                 dados["valor_desconto"] if valor_editavel else d["valor_retido"],
                 dados["valor_desconto"] if valor_editavel else d["valor_segurado"], dados["motivo"], id),
            )
            mudou = []
            if dados["valor_desconto"] != d["valor_desconto"]:
                mudou.append(f"valor de {fmt_moeda(d['valor_desconto'])} para {fmt_moeda(dados['valor_desconto'])}")
            if dados["invoice_origem"] != d["invoice_origem"]:
                mudou.append(f"invoice de {d['invoice_origem']} para {dados['invoice_origem']}")
            if fornecedor_id != d["fornecedor_id"]:
                mudou.append(f"fornecedor de {d['fornecedor']} para {dados['fornecedor']}")
            obs = ("Corrigido: " + "; ".join(mudou)) if mudou else None
            registrar_historico(id, "Cadastro editado", obs=obs)
            if mudou:
                # o Financeiro recebeu a instrução de segurar com os dados antigos: corrige e reenvia
                anular_notificacoes(id, "SEGURAR", f"Cadastro corrigido: {'; '.join(mudou)}.")
                criar_notificacao(id, "SEGURAR", dados["valor_desconto"])
            db.commit()
            flash("Desconto atualizado." + (" O Financeiro foi avisado da correção." if mudou else ""), "ok")
            enviar_novas()
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
    deducoes = db.execute("SELECT * FROM deducoes WHERE desconto_id = ? ORDER BY id", (id,)).fetchall()
    notificacoes = db.execute(
        "SELECT n.*, x.invoice FROM notificacoes n LEFT JOIN deducoes x ON x.id = n.deducao_id"
        " WHERE n.desconto_id = ? ORDER BY n.id", (id,)).fetchall()
    # notificação de liberação vigente de cada dedução que chegou
    liberacao_por_deducao = {n["deducao_id"]: n for n in notificacoes
                             if n["tipo"] == "LIBERAR_CHEGADA" and not n["anulada_em"]}
    historico = db.execute(
        "SELECT h.*, u.nome AS usuario FROM historico h LEFT JOIN usuarios u ON u.id = h.usuario_id"
        " WHERE h.desconto_id = ? ORDER BY h.id", (id,)
    ).fetchall()
    nomes = {u["id"]: u["nome"] for u in db.execute("SELECT id, nome FROM usuarios")}
    revisoes = db.execute(
        "SELECT r.*, u.nome AS usuario FROM revisoes_valor r LEFT JOIN usuarios u ON u.id = r.usuario_id"
        " WHERE r.desconto_id = ? ORDER BY r.id", (id,)).fetchall()
    liberado = liberacao_solicitada(d)
    com_ciencia = [n for n in notificacoes if n["exige_ciencia"] and not n["anulada_em"]]
    resumo = {
        "deduzido": d["total_deduzido"],
        "chegado": d["total_chegado"],
        "saldo": saldo_a_deduzir(d),
        "liberado": liberado,
        "a_liberar": round(d["valor_segurado"] - liberado, 2) if d["valor_segurado"] is not None else 0,
        "diferenca": round(d["valor_segurado"] - d["valor_desconto"], 2) if d["valor_segurado"] is not None else 0,
        "minimo_revalidacao": d["total_deduzido"],
        "tem_liberacao": tem_liberacao(d),
        "maximo_revalidacao": round(d["valor_segurado"] - d["diferenca_solicitada"], 2),
        "n": len(deducoes),
        "chegaram": sum(1 for x in deducoes if x["status"] == "CHEGOU"),
        "ciencias": len([n for n in com_ciencia if n["ciencia_em"]]),
        "ciencias_total": len(com_ciencia),
    }
    return render_template("desconto_detalhe.html", d=d, deducoes=deducoes, notificacoes=notificacoes,
                           liberacao_por_deducao=liberacao_por_deducao, revisoes=revisoes, historico=historico,
                           nomes=nomes, resumo=resumo, hoje=datetime.now().strftime("%Y-%m-%d"))


# ---------------------------------------------------------------------------
# Revalidação do valor (somente administrador)
# O valor inicial segurado (valor_retido) é preservado; o valor final (valor_desconto)
# passa a valer para as etapas seguintes. O valor final pode ficar acima do segurado
# somente enquanto nenhuma liberação foi pedida ao Financeiro: nesse caso o valor adicional
# também é segurado (valor_segurado acompanha o valor final) e o Financeiro é avisado.
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
    maximo = round(d["valor_segurado"] - d["diferenca_solicitada"], 2)
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
    elif aumento and tem_liberacao(d):
        erro = (f"O Financeiro já foi avisado para liberar parte do valor segurado, então não é possível segurar"
                f" valor adicional. O valor final pode ser no máximo {fmt_moeda(maximo)}.")
    elif aumento and request.form.get("confirmo_segurar") != "1":
        erro = (f"O novo valor é maior que o segurado ({fmt_moeda(d['valor_segurado'])}). Marque a confirmação de que"
                f" o invoice {d['invoice_origem']} ainda não foi pago e o valor adicional também será segurado.")
    elif not aumento and novo > maximo + TOLERANCIA:
        erro = (f"O valor final não pode ser maior que {fmt_moeda(maximo)} "
                "(valor segurado menos a diferença já informada ao Financeiro para liberar).")
    if erro:
        flash(erro, "erro")
        return volta
    db = get_db()
    db.execute("INSERT INTO revisoes_valor (desconto_id, valor_anterior, valor_novo, motivo, usuario_id, criado_em)"
               " VALUES (?, ?, ?, ?, ?, ?)", (id, d["valor_desconto"], novo, motivo, g.usuario["id"], agora()))
    db.execute("UPDATE descontos SET valor_desconto = ? WHERE id = ?", (novo, id))
    contexto = f"Valor do desconto revalidado de {fmt_moeda(d['valor_desconto'])} para {fmt_moeda(novo)}. Motivo: {motivo}"
    acao = f"Valor revalidado · de {fmt_moeda(d['valor_desconto'])} para {fmt_moeda(novo)}"
    if novo == 0:
        acao += " · desconto zerado"
    dif = 0
    if aumento:
        db.execute("UPDATE descontos SET valor_segurado = ? WHERE id = ?", (novo, id))
        acao += f" · total segurado aumentado de {fmt_moeda(d['valor_segurado'])} para {fmt_moeda(novo)}"
        criar_notificacao(id, "SEGURAR_ADICIONAL", novo - d["valor_segurado"],
                          detalhe=f"{contexto} O total segurado passa a ser {fmt_moeda(novo)}.")
    else:
        dif = round(d["valor_segurado"] - novo - d["diferenca_solicitada"], 2)
        if dif > TOLERANCIA:
            criar_notificacao(id, "LIBERAR_DIFERENCA", dif, detalhe=(
                f"{contexto} O desconto foi zerado: todo o valor segurado deve ser liberado." if novo == 0
                else contexto))
    registrar_etapa(id, acao, d["status"], motivo)
    db.commit()
    if aumento:
        flash(f"Valor revalidado. O Financeiro foi avisado para segurar o valor adicional de "
              f"{fmt_moeda(novo - d['valor_segurado'])} no invoice {d['invoice_origem']}.", "ok")
    elif dif > TOLERANCIA:
        flash(f"Valor revalidado. O Financeiro foi avisado para liberar {fmt_moeda(dif)} ao fornecedor"
              f"{' (desconto zerado)' if novo == 0 else ''}.", "ok")
    else:
        flash("Valor revalidado.", "ok")
    enviar_novas()
    return volta


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
    elif get_db().execute("SELECT 1 FROM deducoes WHERE desconto_id = ? AND lower(invoice) = lower(?)",
                          (id, invoice)).fetchone():
        erro = f"O invoice {invoice} já tem uma dedução registrada para este desconto."
    if erro:
        flash(erro, "erro")
        return volta
    obs = f.get("obs", "").strip() or None
    db = get_db()
    db.execute(
        "INSERT INTO deducoes (desconto_id, status, invoice, valor_deduzido, obs_deducao, deduzido_por, deduzido_em)"
        " VALUES (?, 'AGUARDANDO_CHEGADA', ?, ?, ?, ?, ?)",
        (id, invoice, valor, obs, g.usuario["id"], agora()),
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
# Etapa 4 - O Financeiro é avisado por e-mail para liberar a parte correspondente do valor segurado
# ---------------------------------------------------------------------------
@app.route("/deducoes/<int:pid>/receber", methods=["POST"])
@login_obrigatorio
def deducao_receber(pid):
    if not pode(ETAPAS["receber"]["perfil"]):
        abort(403)
    x, d = carregar_deducao(pid)
    volta = redirect(url_for("desconto_detalhe", id=d["id"]))
    if x["status"] != "AGUARDANDO_CHEGADA" or d["status"] == "CANCELADO":
        flash("Esta dedução não está mais aguardando chegada. A página foi atualizada.", "erro")
        return volta
    data = request.form.get("data_chegada")
    if not data:
        flash("Informe a data de chegada na fábrica.", "erro")
        return volta
    obs = request.form.get("obs", "").strip() or None
    db = get_db()
    db.execute("UPDATE deducoes SET status='CHEGOU', data_chegada=?, obs_chegada=?, recebido_por=?, recebido_em=?"
               " WHERE id=?", (data, obs, g.usuario["id"], agora(), pid))
    criar_notificacao(d["id"], "LIBERAR_CHEGADA", x["valor_deduzido"], deducao_id=pid, detalhe=(
        f"O invoice {x['invoice']}, com dedução de {fmt_moeda(x['valor_deduzido'])}, chegou à fábrica em "
        f"{fmt_data(data)}." + (f" Observação: {obs}" if obs else "")))
    registrar_etapa(d["id"], f"Chegada confirmada · invoice {x['invoice']}", d["status"], obs)
    db.commit()
    flash(f"Chegada confirmada. O Financeiro foi avisado para liberar {fmt_moeda(x['valor_deduzido'])}.", "ok")
    enviar_novas()
    return volta


# ---------------------------------------------------------------------------
# Ações do administrador
# ---------------------------------------------------------------------------
@app.route("/deducoes/<int:pid>/desfazer", methods=["POST"])
@somente_admin
def deducao_desfazer(pid):
    """Desfaz a última etapa da dedução (chegada -> exclusão da dedução)."""
    x, d = carregar_deducao(pid)
    volta = redirect(url_for("desconto_detalhe", id=d["id"]))
    motivo = request.form.get("motivo", "").strip()
    if not motivo:
        flash("Informe o motivo para desfazer.", "erro")
        return volta
    db = get_db()
    if x["status"] == "CHEGOU":
        db.execute("UPDATE deducoes SET status='AGUARDANDO_CHEGADA', data_chegada=NULL, obs_chegada=NULL,"
                   " recebido_por=NULL, recebido_em=NULL WHERE id=?", (pid,))
        anular_notificacoes(d["id"], "LIBERAR_CHEGADA", f"Chegada do invoice {x['invoice']} desfeita: {motivo}",
                            deducao_id=pid)
        acao = f"Chegada desfeita · invoice {x['invoice']}"
    else:
        db.execute("UPDATE notificacoes SET deducao_id = NULL WHERE deducao_id = ?", (pid,))
        db.execute("DELETE FROM deducoes WHERE id=?", (pid,))
        acao = f"Dedução excluída · {fmt_moeda(x['valor_deduzido'])} no invoice {x['invoice']}"
    registrar_etapa(d["id"], acao, d["status"], motivo)
    db.commit()
    flash("Etapa desfeita.", "ok")
    enviar_novas()
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
        a_liberar = round(d["valor_segurado"] - liberacao_solicitada(d), 2)
        if a_liberar > TOLERANCIA:
            criar_notificacao(id, "LIBERAR_CANCELAMENTO", a_liberar,
                              detalhe=f"O desconto foi cancelado. Motivo: {motivo}")
        db.commit()
        flash("Desconto cancelado." + (f" O Financeiro foi avisado para liberar {fmt_moeda(a_liberar)}."
                                       if a_liberar > TOLERANCIA else ""), "ok")
        enviar_novas()
    return redirect(url_for("desconto_detalhe", id=id))


# ---------------------------------------------------------------------------
# Notificações por e-mail ao Financeiro
# Cada evento que exige ação do Financeiro gera um registro em `notificacoes` e um e-mail
# informativo (sem acesso ao sistema). O e-mail traz apenas um link para a página de ciência,
# onde um destinatário principal confirma com um botão que leu e está de acordo.
# ---------------------------------------------------------------------------
ARQ_SENHA_EMAIL = os.path.join(INSTANCE_DIR, "gmail_senha_app.txt")


def config(chave, padrao=None):
    r = get_db().execute("SELECT valor FROM configuracoes WHERE chave = ?", (chave,)).fetchone()
    return r["valor"] if r and r["valor"] is not None else padrao


def salvar_config(chave, valor):
    get_db().execute("INSERT INTO configuracoes (chave, valor) VALUES (?, ?)"
                     " ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor", (chave, valor))


def senha_email():
    try:
        with open(ARQ_SENHA_EMAIL) as f:
            return f.read().strip()
    except OSError:
        return ""


def salvar_senha_email(senha):
    os.makedirs(INSTANCE_DIR, exist_ok=True)
    with open(ARQ_SENHA_EMAIL, "w") as f:
        f.write(senha.replace(" ", ""))
    os.chmod(ARQ_SENHA_EMAIL, 0o600)


def lembrar_endereco():
    """Guarda o endereço do sistema para montar o link de ciência (também usado pela tarefa diária)."""
    if has_request_context():
        base = request.url_root.rstrip("/")
        if os.environ.get("HTTPS") == "1" and base.startswith("http://"):
            base = "https://" + base[len("http://"):]
        if config("url_base") != base:
            salvar_config("url_base", base)


def criar_notificacao(desconto_id, tipo, valor=None, deducao_id=None, detalhe=None, referencia_id=None):
    """Registra o evento; o e-mail é enviado depois do commit, por enviar_novas()."""
    lembrar_endereco()
    cur = get_db().execute(
        "INSERT INTO notificacoes (desconto_id, deducao_id, tipo, valor, detalhe, exige_ciencia, referencia_id,"
        " token, criado_em, criado_por, envio_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'AGUARDANDO')",
        (desconto_id, deducao_id, tipo, round(valor, 2) if valor is not None else None, detalhe,
         0 if tipo == "ANULACAO" else 1, referencia_id, secrets.token_urlsafe(24), agora(),
         g.usuario["id"] if g.get("usuario") else None))
    g.setdefault("notificacoes_novas", []).append(cur.lastrowid)
    return cur.lastrowid


def anular_notificacoes(desconto_id, tipo, motivo, deducao_id=None):
    """Anula notificações vigentes de um tipo. As que já chegaram ao Financeiro geram e-mail de correção."""
    db = get_db()
    sql = "SELECT * FROM notificacoes WHERE desconto_id = ? AND tipo = ? AND anulada_em IS NULL"
    params = [desconto_id, tipo]
    if deducao_id is not None:
        sql += " AND deducao_id = ?"
        params.append(deducao_id)
    for n in db.execute(sql, params).fetchall():
        db.execute("UPDATE notificacoes SET anulada_em = ?, anulada_motivo = ? WHERE id = ?", (agora(), motivo, n["id"]))
        if n["envio_status"] in ("ENVIADO", "TESTE"):
            criar_notificacao(desconto_id, "ANULACAO", n["valor"], deducao_id=n["deducao_id"], detalhe=motivo,
                              referencia_id=n["id"])


def destinatarios_ativos():
    linhas = get_db().execute("SELECT nome, email, tipo FROM destinatarios WHERE ativo = 1 ORDER BY nome").fetchall()
    return ([(r["nome"], r["email"]) for r in linhas if r["tipo"] == "principal"],
            [(r["nome"], r["email"]) for r in linhas if r["tipo"] == "copia"])


def conteudo_notificacao(n):
    """Monta assunto e textos do e-mail (sem links para o sistema, apenas o link de ciência)."""
    db = get_db()
    d = db.execute(SQL_LISTA + " WHERE d.id = ?", (n["desconto_id"],)).fetchone()
    x = db.execute("SELECT * FROM deducoes WHERE id = ?", (n["deducao_id"],)).fetchone() if n["deducao_id"] else None
    valor = fmt_moeda(n["valor"]) if n["valor"] is not None else ""
    origem, forn = d["invoice_origem"], d["fornecedor"]
    acoes = {
        "SEGURAR": f"Segurar {valor} no pagamento do invoice {origem} do fornecedor {forn}.",
        "SEGURAR_ADICIONAL": f"Segurar o valor adicional de {valor} no invoice {origem} do fornecedor {forn}.",
        "LIBERAR_CHEGADA": f"Liberar ao fornecedor {forn} {valor} do valor segurado no invoice {origem}.",
        "LIBERAR_DIFERENCA": f"Liberar ao fornecedor {forn} {valor} do valor segurado no invoice {origem}.",
        "LIBERAR_CANCELAMENTO": f"Liberar ao fornecedor {forn} {valor} segurado no invoice {origem}.",
    }
    if n["tipo"] == "ANULACAO":
        ref = db.execute("SELECT * FROM notificacoes WHERE id = ?", (n["referencia_id"],)).fetchone()
        acao = (f"Desconsiderar o e-mail de {fmt_data(ref['criado_em'])}: \"{NOTIFICACOES[ref['tipo']]}\""
                f"{' de ' + fmt_moeda(ref['valor']) if ref['valor'] is not None else ''}.")
    else:
        acao = acoes[n["tipo"]]
    linhas = [("Fornecedor", forn), ("Invoice inspecionado", origem)]
    if d["valor_invoice_origem"]:
        linhas.append(("Valor total do invoice", fmt_moeda(d["valor_invoice_origem"])))
    if x:
        linhas.append(("Invoice com dedução", x["invoice"]))
    if n["valor"] is not None:
        linhas.append(("Valor", valor))
    linhas.append(("Referência", f"{codigo_desconto(d)} · {fmt_data(n['criado_em'])}"))
    base = config("url_base", "")
    return {
        "assunto": f"[Descontos] {NOTIFICACOES[n['tipo']]} · {valor + ' · ' if valor else ''}{forn} · {origem}",
        "titulo": NOTIFICACOES[n["tipo"]],
        "acao": acao,
        "detalhe": n["detalhe"],
        "linhas": linhas,
        "link_ciencia": f"{base}/ciencia/{n['token']}" if n["exige_ciencia"] else None,
    }


def enviar_notificacao(nid, forcar=False):
    """Envia (ou registra, no modo de teste) o e-mail de uma notificação. Retorna (ok, mensagem)."""
    db = get_db()
    n = db.execute("SELECT * FROM notificacoes WHERE id = ?", (nid,)).fetchone()
    para, copia = destinatarios_ativos()
    ts = agora()
    if config("email_modo", "teste") != "ativo" and not forcar:
        db.execute("UPDATE notificacoes SET envio_status = 'TESTE', enviado_em = ?, enviado_para = ?, enviado_cc = ?"
                   " WHERE id = ?", (ts, json.dumps(para), json.dumps(copia), nid))
        db.commit()
        return True, None
    erro = None
    if not para:
        erro = "Nenhum destinatário principal ativo cadastrado."
    elif not config("email_remetente") or not senha_email():
        erro = "Configure o e-mail remetente (Gmail) e a senha de app."
    if not erro:
        c = conteudo_notificacao(n)
        html = render_template("email/notificacao.html", c=c, copia=copia)
        texto = render_template("email/notificacao.txt", c=c, copia=copia)
        for tentativa in (1, 2):
            try:
                email_envio.enviar(config("email_remetente"), config("email_nome", "Controle de Descontos"),
                                   senha_email(), para, copia, c["assunto"], texto, html)
                erro = None
                break
            except email_envio.FalhaEnvio as e:
                erro = str(e)
    if erro:
        db.execute("UPDATE notificacoes SET envio_status = 'FALHOU', tentativas = tentativas + 1, ultimo_erro = ?"
                   " WHERE id = ?", (erro, nid))
    else:
        db.execute("UPDATE notificacoes SET envio_status = 'ENVIADO', tentativas = tentativas + 1, ultimo_erro = NULL,"
                   " enviado_em = ?, enviado_para = ?, enviado_cc = ? WHERE id = ?",
                   (ts, json.dumps(para), json.dumps(copia), nid))
    db.commit()
    return erro is None, erro


def enviar_novas():
    """Envia os e-mails das notificações criadas nesta requisição (chamar após o commit)."""
    for nid in g.pop("notificacoes_novas", []):
        ok, erro = enviar_notificacao(nid)
        if not ok:
            flash(f"Atenção: o e-mail ao Financeiro não foi enviado ({erro}). Ele pode ser reenviado em "
                  "Notificações e será tentado novamente automaticamente.", "erro")


def alerta_email():
    """Resumo para alertas do administrador no painel."""
    if not g.get("usuario") or g.usuario["perfil"] != "admin":
        return None
    db = get_db()
    return {
        "modo_teste": config("email_modo", "teste") != "ativo",
        "falhas": db.execute("SELECT COUNT(*) FROM notificacoes WHERE envio_status IN ('FALHOU', 'AGUARDANDO')"
                             ).fetchone()[0],
        "sem_principal": not destinatarios_ativos()[0],
    }


def tarefa_envios():
    """Usada pela tarefa diária: reenvia e-mails que falharam ou ficaram aguardando."""
    with app.app_context():
        if config("email_modo", "teste") != "ativo":
            print("Envio de e-mails em modo de teste: nada a reenviar.")
            return
        ids = [r[0] for r in get_db().execute(
            "SELECT id FROM notificacoes WHERE envio_status IN ('FALHOU', 'AGUARDANDO') ORDER BY id")]
        for nid in ids:
            ok, erro = enviar_notificacao(nid)
            print(f"Notificação {nid}: {'enviada' if ok else 'falhou - ' + erro}")
        if not ids:
            print("Nenhum e-mail pendente de envio.")


# --- Página de ciência (acesso pelo link do e-mail, sem login) --------------------------------
def principais_da_notificacao(n):
    if n["enviado_para"]:
        return [tuple(p) for p in json.loads(n["enviado_para"])]
    return destinatarios_ativos()[0]


@app.route("/ciencia/<token>", methods=["GET", "POST"])
def ciencia(token):
    db = get_db()
    n = db.execute("SELECT * FROM notificacoes WHERE token = ?", (token,)).fetchone()
    if n is None or not n["exige_ciencia"]:
        abort(404)
    principais = principais_da_notificacao(n)
    if request.method == "POST" and not n["ciencia_em"] and not n["anulada_em"]:
        escolhido = request.form.get("quem", "")
        pessoa = next((p for p in principais if p[1] == escolhido), None)
        if pessoa:
            d = db.execute("SELECT status FROM descontos WHERE id = ?", (n["desconto_id"],)).fetchone()
            quem = f"{pessoa[0]} <{pessoa[1]}>"
            db.execute("UPDATE notificacoes SET ciencia_em = ?, ciencia_por = ? WHERE id = ?", (agora(), quem, n["id"]))
            registrar_etapa(n["desconto_id"], f"Ciência do Financeiro · {NOTIFICACOES[n['tipo']]}"
                            f"{' · ' + fmt_moeda(n['valor']) if n['valor'] is not None else ''}", d["status"],
                            f"Confirmado por {quem}")
            db.commit()
            n = db.execute("SELECT * FROM notificacoes WHERE id = ?", (n["id"],)).fetchone()
    return render_template("ciencia.html", n=n, c=conteudo_notificacao(n), principais=principais)


# --- Tela de gestão (somente administrador) ----------------------------------------------------
@app.route("/notificacoes")
@somente_admin
def notificacoes():
    db = get_db()
    filtro = request.args.get("f", "pendentes")
    where = {
        "pendentes": f"({SQL_CIENCIA_PENDENTE}) OR n.envio_status IN ('FALHOU', 'AGUARDANDO')",
        "falhas": "n.envio_status IN ('FALHOU', 'AGUARDANDO')",
    }.get(filtro, "1 = 1")
    lista = db.execute(
        "SELECT n.*, f.nome AS fornecedor, d.invoice_origem, x.invoice FROM notificacoes n"
        " JOIN descontos d ON d.id = n.desconto_id JOIN fornecedores f ON f.id = d.fornecedor_id"
        f" LEFT JOIN deducoes x ON x.id = n.deducao_id WHERE {where} ORDER BY n.id DESC LIMIT 300").fetchall()
    destinatarios = db.execute("SELECT * FROM destinatarios ORDER BY ativo DESC, tipo DESC, nome").fetchall()
    teste_pendentes = db.execute(
        f"SELECT COUNT(*) FROM notificacoes n WHERE n.envio_status = 'TESTE' AND {SQL_CIENCIA_PENDENTE}").fetchone()[0]
    return render_template(
        "notificacoes.html", lista=lista, filtro=filtro, destinatarios=destinatarios,
        modo=config("email_modo", "teste"), remetente=config("email_remetente", ""),
        nome_remetente=config("email_nome", "Controle de Descontos"), tem_senha=bool(senha_email()),
        teste_pendentes=teste_pendentes, alerta_email=alerta_email(),
        json_loads=json.loads)


@app.route("/notificacoes/configuracao", methods=["POST"])
@somente_admin
def notificacoes_configuracao():
    f = request.form
    remetente = f.get("remetente", "").strip().lower()
    modo = "ativo" if f.get("modo") == "ativo" else "teste"
    if remetente and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", remetente):
        flash("E-mail remetente inválido.", "erro")
        return redirect(url_for("notificacoes"))
    if f.get("senha", "").strip():
        salvar_senha_email(f["senha"].strip())
    if modo == "ativo" and (not remetente or not senha_email()):
        flash("Para ativar o envio, informe o e-mail remetente e a senha de app.", "erro")
        modo = "teste"
    salvar_config("email_remetente", remetente)
    salvar_config("email_nome", f.get("nome", "").strip() or "Controle de Descontos")
    salvar_config("email_modo", modo)
    get_db().commit()
    flash("Configuração de envio salva." + (" Envio ATIVO." if modo == "ativo" else " Modo de teste."), "ok")
    return redirect(url_for("notificacoes"))


@app.route("/notificacoes/teste", methods=["POST"])
@somente_admin
def notificacoes_teste():
    destino = request.form.get("email_teste", "").strip()
    if not config("email_remetente") or not senha_email():
        flash("Configure o e-mail remetente e a senha de app antes do teste.", "erro")
    elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", destino):
        flash("Informe um e-mail válido para o teste.", "erro")
    else:
        try:
            email_envio.enviar(config("email_remetente"), config("email_nome", "Controle de Descontos"), senha_email(),
                               [("", destino)], [], "[Descontos] E-mail de teste",
                               "Este é um e-mail de teste do Controle de Descontos. O envio está funcionando.",
                               "<p>Este é um e-mail de teste do <strong>Controle de Descontos</strong>. "
                               "O envio está funcionando.</p>")
            flash(f"E-mail de teste enviado para {destino}. Confira a caixa de entrada (e o lixo eletrônico).", "ok")
        except email_envio.FalhaEnvio as e:
            flash(f"O teste falhou: {e}", "erro")
    return redirect(url_for("notificacoes"))


@app.route("/notificacoes/destinatarios", methods=["POST"])
@somente_admin
def notificacoes_destinatarios():
    f = request.form
    db = get_db()
    nome, email = f.get("nome", "").strip(), f.get("email", "").strip().lower()
    tipo = "copia" if f.get("tipo") == "copia" else "principal"
    ativo = 0 if f.get("ativo") == "0" else 1
    if not nome or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        flash("Informe nome e um e-mail válido.", "erro")
        return redirect(url_for("notificacoes") + "#destinatarios")
    try:
        if f.get("id", "").isdigit():
            db.execute("UPDATE destinatarios SET nome=?, email=?, tipo=?, ativo=? WHERE id=?",
                       (nome, email, tipo, ativo, int(f["id"])))
            flash("Destinatário atualizado.", "ok")
        else:
            db.execute("INSERT INTO destinatarios (nome, email, tipo, ativo, criado_em) VALUES (?, ?, ?, 1, ?)",
                       (nome, email, tipo, agora()))
            flash("Destinatário cadastrado.", "ok")
        db.commit()
    except sqlite3.IntegrityError:
        flash("Este e-mail já está cadastrado.", "erro")
    return redirect(url_for("notificacoes") + "#destinatarios")


@app.route("/notificacoes/<int:nid>/enviar", methods=["POST"])
@somente_admin
def notificacao_enviar(nid):
    if get_db().execute("SELECT 1 FROM notificacoes WHERE id = ?", (nid,)).fetchone() is None:
        abort(404)
    lembrar_endereco()
    ok, erro = enviar_notificacao(nid)
    if ok:
        flash("E-mail enviado." if config("email_modo", "teste") == "ativo" else
              "Registrado em modo de teste (nenhum e-mail saiu).", "ok")
    else:
        flash(f"O envio falhou: {erro}", "erro")
    return redirect(request.form.get("voltar") or url_for("notificacoes"))


@app.route("/notificacoes/enviar-pendentes", methods=["POST"])
@somente_admin
def notificacoes_enviar_pendentes():
    """Envia as que falharam/aguardam e, se pedido, as geradas em modo de teste ainda sem ciência."""
    if config("email_modo", "teste") != "ativo":
        flash("Ative o envio antes de enviar as notificações pendentes.", "erro")
        return redirect(url_for("notificacoes"))
    lembrar_endereco()
    status = ["'FALHOU'", "'AGUARDANDO'"]
    sql = f"SELECT n.id FROM notificacoes n WHERE n.envio_status IN ({', '.join(status)})"
    if request.form.get("incluir_teste") == "1":
        sql += f" OR (n.envio_status = 'TESTE' AND {SQL_CIENCIA_PENDENTE})"
    ids = [r[0] for r in get_db().execute(sql + " ORDER BY n.id")]
    falhas = [erro for ok, erro in (enviar_notificacao(i) for i in ids) if not ok]
    if falhas:
        flash(f"{len(ids) - len(falhas)} enviado(s), {len(falhas)} com falha: {falhas[0]}", "erro")
    else:
        flash(f"{len(ids)} e-mail(s) enviado(s).", "ok")
    return redirect(url_for("notificacoes"))


@app.route("/notificacoes/<int:nid>/previa")
@somente_admin
def notificacao_previa(nid):
    n = get_db().execute("SELECT * FROM notificacoes WHERE id = ?", (nid,)).fetchone()
    if n is None:
        abort(404)
    lembrar_endereco()
    para, copia = destinatarios_ativos()
    return render_template("email/notificacao.html", c=conteudo_notificacao(n), copia=copia, previa=True)


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
