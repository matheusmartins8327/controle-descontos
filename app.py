"""
Controle de Invoices e Descontos - Fornecedores

Controla o ciclo completo de cada invoice, da criação até o pagamento completo:
  Importação         -> cria o invoice (itens, moeda, condição de pagamento, deduções de descontos
                        aprovados) e acompanha coleta, embarque, desembaraço, canal e chegada
  Engenharia (admin) -> registra a inspeção, o desconto por má qualidade e acompanha a aprovação
                        do fornecedor; revalida o valor quando necessário
  Financeiro         -> registra pagamentos (antecipado, integral ou parcial com valor segurado),
                        liberações do valor segurado e Swifts; acompanha o fluxo de pagamentos

Documentos circulam por e-mail/OneDrive, fora do sistema (só o link do Swift é guardado).
"""
import csv
import glob
import io
import unicodedata
import os
import re
import secrets
import json
import smtplib
import socket
import sqlite3
import subprocess
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from functools import wraps

from markupsafe import escape

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
DB_PATH = os.environ.get("DESCONTOS_DB", os.path.join(INSTANCE_DIR, "descontos.db"))
MAX_USUARIOS = 10
VERSAO = "4.0"

# ---------------------------------------------------------------------------
# Perfis, moedas e etapas
# ---------------------------------------------------------------------------
PERFIS = {
    "admin": "Administrador",
    "importacao": "Importação",
    "financeiro": "Financeiro",
    "consulta": "Consulta (somente leitura)",
}
# setor responsável por cada pendência (o Administrador é a Engenharia de Processos)
SETORES = {"importacao": "Importação", "admin": "Engenharia de Processos", "financeiro": "Financeiro"}
# perfis de versões anteriores -> perfis atuais (aplicado automaticamente ao iniciar)
PERFIS_ANTIGOS = {"qualidade": "consulta", "compras": "importacao", "recebimento": "importacao"}

MOEDAS = {"USD": "US$", "EUR": "€", "BRL": "R$"}
# Condição de pagamento: uma ou mais parcelas (ex.: 25% antecipado + 75% 60 dias após a HAWB)
CONDICOES = {"ANTECIPADO": "Antecipado", "PRAZO": "Após a HAWB"}
PRAZO_ANTECIPADO_DIAS = 5          # pagamento antecipado: data de assinatura + 5 dias úteis
NACIONALIZACAO_PLANEJADA = (-1, 3)  # impostos de nacionalização: ETA planejado -1 a +3 dias úteis
NACIONALIZACAO_REAL = (0, 2)        # com o ETA real: ETA real a +2 dias úteis
PRAZO_LIBERACAO_DIAS_UTEIS = 5     # liberação do segurado: chegada real do invoice deduzido + 5 dias úteis
CUSTO_IMPORTACAO_ESTIMADO = 0.40   # visibilidade do Financeiro a partir do embarque
# canal de parametrização: opção -> (cor de fundo pastel, cor do texto)
CORES_CANAL = {"Verde": ("#d4f0dc", "#1d5a31"), "Amarelo": ("#fff3c4", "#6b5200")}
STATUS_DESCONTO = {"ENVIADO": "Enviado ao fornecedor", "APROVADO": "Aprovado pelo fornecedor"}
TIPOS_PAGAMENTO = {"PRINCIPAL": "Pagamento do invoice", "LIBERACAO": "Liberação do valor segurado"}

# Etapas logísticas (Importação): campos de cada etapa -> (campo, rótulo, tipo, obrigatório p/ concluir)
CAMPOS_ETAPA = {
    "COLETA": [("data_hawb", "Data da HAWB", "data", True),
               ("data_coleta", "Data real de coleta", "data", True)],
    "EMBARQUE": [("etd_planejado", "ETD planejado", "data", True),
                 ("etd_real", "ETD real", "data", True),
                 ("eta_planejado", "ETA planejado", "data", True),
                 ("eta_real", "ETA real", "data", True)],
    "DESEMBARACO": [("data_registro_br", "Data de emissão da DUIMP", "data", True),
                    ("data_parametrizacao", "Data de parametrização", "data", True),
                    ("cor_canal", "Canal", "canal", True),
                    ("exigencias_anvisa", "Exigências da ANVISA", "texto", False),
                    ("data_desembaraco", "Data de desembaraço", "data", True)],
    "CHEGADA": [("data_prog_chegada", "Data programada de chegada", "data", True),
                ("data_chegada", "Data real de chegada na fábrica", "data", True)],
}
TITULO_ETAPA = {"COLETA": "Coleta", "EMBARQUE": "Embarque", "DESEMBARACO": "Desembaraço", "CHEGADA": "Transporte"}
# Situação atual do invoice (calculada) e quem age nela
SITUACOES = {
    "CRIACAO": ("Cadastro incompleto", "importacao"),
    "COLETA": ("Aguardando coleta", "importacao"),
    "EMBARQUE": ("Em embarque", "importacao"),
    "DESEMBARACO": ("Em desembaraço", "importacao"),
    "CHEGADA": ("Em transporte para a fábrica", "importacao"),
    "INSPECAO": ("Aguardando inspeção", "admin"),
    "PAGAMENTO": ("Aguardando pagamento", "financeiro"),
    "DEDUCAO": ("Desconto aguardando dedução", "importacao"),
    "LIBERACAO": ("Aguardando liberação do segurado", "financeiro"),
    "SWIFT": ("Swift pendente", "financeiro"),
    "CONCLUIDO": ("Concluído", None),
    "CANCELADO": ("Cancelado", None),
}
ORDEM_LOGISTICA = ["COLETA", "EMBARQUE", "DESEMBARACO", "CHEGADA"]

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
CREATE TABLE IF NOT EXISTS configuracoes (
    chave TEXT PRIMARY KEY,
    valor TEXT
);
CREATE TABLE IF NOT EXISTS tentativas_login (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    criado_em TEXT NOT NULL
);
-- Invoice: entidade central do processo
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fornecedor_id INTEGER NOT NULL REFERENCES fornecedores(id),
    numero TEXT NOT NULL,
    moeda TEXT NOT NULL,
    -- Criação (Importação)
    data_assinatura TEXT NOT NULL,
    condicao_tipo TEXT NOT NULL,          -- ANTECIPADO | PRAZO | MISTA (detalhe em condicao_parcelas)
    condicao_dias INTEGER,                -- dias após a HAWB (condição de parcela única a prazo)
    agente_carga TEXT,
    despachante TEXT,
    grupo_consolidacao TEXT,
    data_prog_coleta TEXT,
    obs TEXT,
    -- Coleta / embarque / desembaraço / canal / chegada (Importação)
    data_hawb TEXT,
    data_coleta TEXT,
    etd_planejado TEXT,
    etd_real TEXT,
    eta_planejado TEXT,
    eta_real TEXT,
    banco_debito TEXT,                    -- informado pelo Financeiro
    data_registro_br TEXT,
    data_parametrizacao TEXT,
    cor_canal TEXT,
    exigencias_anvisa TEXT,
    data_prog_chegada TEXT,
    data_desembaraco TEXT,
    data_chegada TEXT,
    -- Inspeção (Engenharia de Processos)
    data_fim_inspecao TEXT,
    tem_desconto INTEGER,                 -- NULL = não inspecionado; 0 = não; 1 = sim
    criado_por INTEGER REFERENCES usuarios(id),
    criado_em TEXT NOT NULL,
    cancelado_em TEXT,
    cancelado_por INTEGER REFERENCES usuarios(id),
    motivo_cancelamento TEXT,
    UNIQUE (fornecedor_id, numero)
);
CREATE TABLE IF NOT EXISTS invoice_itens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    codigo TEXT,                          -- código do item
    item TEXT NOT NULL,                   -- descrição do item
    quantidade REAL,                      -- pode ficar vazio enquanto o cadastro está incompleto
    valor_unit_original REAL,             -- (versões anteriores) preço original separado
    valor_unit REAL                       -- preço original do item: total = quantidade x preço
);
-- Parcelas da condição de pagamento (percentuais somam 100%)
CREATE TABLE IF NOT EXISTS condicao_parcelas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    ordem INTEGER NOT NULL,
    percentual REAL NOT NULL,
    tipo TEXT NOT NULL,                   -- ANTECIPADO (assinatura + 5 dias úteis) | PRAZO (HAWB + dias)
    dias INTEGER
);
-- Desconto por má qualidade de um invoice (no máximo um por invoice)
CREATE TABLE IF NOT EXISTS descontos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL UNIQUE REFERENCES invoices(id),
    valor_inicial REAL NOT NULL,
    valor_final REAL NOT NULL,            -- pode ser revalidado; é o valor deduzido nos próximos invoices
    valor_segurado REAL NOT NULL,         -- parte não paga do invoice que fica retida até a dedução
    data_relatorio TEXT NOT NULL,
    status TEXT NOT NULL,                 -- ENVIADO | APROVADO
    aprovado_em TEXT,
    aprovado_por INTEGER REFERENCES usuarios(id),
    obs TEXT,
    criado_por INTEGER REFERENCES usuarios(id),
    criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revisoes_desconto (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    desconto_id INTEGER NOT NULL REFERENCES descontos(id),
    valor_anterior REAL NOT NULL,
    valor_novo REAL NOT NULL,
    motivo TEXT NOT NULL,
    usuario_id INTEGER REFERENCES usuarios(id),
    criado_em TEXT NOT NULL
);
-- Parte de um desconto deduzida em outro invoice do mesmo fornecedor
CREATE TABLE IF NOT EXISTS deducoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    desconto_id INTEGER NOT NULL REFERENCES descontos(id),
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    valor REAL NOT NULL,
    criado_por INTEGER REFERENCES usuarios(id),
    criado_em TEXT NOT NULL,
    UNIQUE (desconto_id, invoice_id)
);
-- Pagamentos registrados pelo Financeiro
CREATE TABLE IF NOT EXISTS pagamentos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    tipo TEXT NOT NULL,                   -- PRINCIPAL | LIBERACAO
    valor REAL NOT NULL,
    data_pagamento TEXT NOT NULL,
    banco TEXT,
    swift_url TEXT,                       -- link do Swift no OneDrive (pendente enquanto vazio)
    obs TEXT,
    registrado_por INTEGER REFERENCES usuarios(id),
    registrado_em TEXT NOT NULL
);
-- Documentos: links do OneDrive (no futuro, pode receber integração automática)
CREATE TABLE IF NOT EXISTS anexos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    tipo TEXT NOT NULL,
    descricao TEXT,
    url TEXT NOT NULL,
    origem TEXT NOT NULL DEFAULT 'LINK',  -- LINK (colado manualmente) | futuramente ONEDRIVE_API
    criado_por INTEGER REFERENCES usuarios(id),
    criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eventos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    usuario_id INTEGER REFERENCES usuarios(id),
    acao TEXT NOT NULL,
    observacao TEXT,
    criado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx3_invoices_fornecedor ON invoices(fornecedor_id);
CREATE INDEX IF NOT EXISTS idx3_itens_invoice ON invoice_itens(invoice_id);
CREATE INDEX IF NOT EXISTS idx3_deducoes_desconto ON deducoes(desconto_id);
CREATE INDEX IF NOT EXISTS idx3_deducoes_invoice ON deducoes(invoice_id);
CREATE INDEX IF NOT EXISTS idx3_pagamentos_invoice ON pagamentos(invoice_id);
CREATE INDEX IF NOT EXISTS idx3_anexos_invoice ON anexos(invoice_id);
CREATE INDEX IF NOT EXISTS idx3_eventos_invoice ON eventos(invoice_id);
-- versão 4.0: e-mails automáticos
CREATE TABLE IF NOT EXISTS notificacoes_estado (
    invoice_id INTEGER PRIMARY KEY,
    estado TEXT NOT NULL,                 -- última situação de previsibilidade notificada (JSON)
    atualizado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fluxo TEXT NOT NULL,
    invoice_id INTEGER,
    assunto TEXT NOT NULL,
    corpo TEXT NOT NULL,
    para TEXT,
    cc TEXT,
    status TEXT NOT NULL,                 -- PENDENTE | ENVIADO | ERRO | NAO_ENVIADO
    erro TEXT,
    tentativas INTEGER NOT NULL DEFAULT 0,
    criado_em TEXT NOT NULL,
    enviado_em TEXT
);
"""

# Tabelas da versão 2 (controle só de descontos). Na versão 3 elas são preservadas com o
# prefixo legacy_v2_ (consulta histórica, se necessário) e o sistema recomeça do zero.
TABELAS_V2 = ["descontos", "deducoes", "historico", "revisoes_valor", "pagamentos_deducao",
              "pagamentos_diferenca", "notificacoes", "destinatarios"]


def get_db():
    if "db" not in g:
        # timeout: com vários usuários simultâneos, uma gravação espera a outra terminar (até 15 s)
        g.db = sqlite3.connect(DB_PATH, timeout=15)
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
        colunas = {r[1] for r in db.execute("PRAGMA table_info(descontos)")}
        if "invoice_origem" in colunas:  # banco da versão 2
            existentes = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            for t in TABELAS_V2:
                if t in existentes and f"legacy_v2_{t}" not in existentes:
                    db.execute(f"ALTER TABLE {t} RENAME TO legacy_v2_{t}")
        db.executescript(SCHEMA)
        # versão 3.1: código do item
        if "codigo" not in {r[1] for r in db.execute("PRAGMA table_info(invoice_itens)")}:
            db.execute("ALTER TABLE invoice_itens ADD COLUMN codigo TEXT")
        if "data_parametrizacao" not in {r[1] for r in db.execute("PRAGMA table_info(invoices)")}:
            db.execute("ALTER TABLE invoices ADD COLUMN data_parametrizacao TEXT")
        # versão 3.6: itens podem ser salvos sem quantidade/preço (cadastro incompleto)
        if any(c[1] in ("quantidade", "valor_unit") and c[3] for c in db.execute("PRAGMA table_info(invoice_itens)")):
            db.executescript("""
                ALTER TABLE invoice_itens RENAME TO invoice_itens_v35;
                CREATE TABLE invoice_itens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
                    codigo TEXT, item TEXT NOT NULL, quantidade REAL, valor_unit_original REAL, valor_unit REAL);
                INSERT INTO invoice_itens (id, invoice_id, codigo, item, quantidade, valor_unit_original, valor_unit)
                    SELECT id, invoice_id, codigo, item, quantidade, valor_unit_original, valor_unit FROM invoice_itens_v35;
                DROP TABLE invoice_itens_v35;
                CREATE INDEX IF NOT EXISTS idx3_itens_invoice ON invoice_itens(invoice_id);
            """)
        # versão 3.2: condição de pagamento em parcelas (invoices antigos viram uma parcela de 100%)
        db.execute("INSERT INTO condicao_parcelas (invoice_id, ordem, percentual, tipo, dias)"
                   " SELECT id, 1, 100, condicao_tipo, condicao_dias FROM invoices i"
                   " WHERE condicao_tipo IN ('ANTECIPADO', 'PRAZO')"
                   " AND NOT EXISTS (SELECT 1 FROM condicao_parcelas c WHERE c.invoice_id = i.id)")
        # cadastros salvos sem condição de pagamento não recebem parcela automática
        db.execute("DELETE FROM condicao_parcelas WHERE tipo NOT IN ('ANTECIPADO', 'PRAZO')")
        # DUIMP e boleto do ICMS circulam por e-mail, fora do sistema: não são guardados
        db.execute("DELETE FROM anexos WHERE tipo IN ('DUIMP', 'BOLETO_ICMS')")
        for antigo, novo in PERFIS_ANTIGOS.items():
            db.execute("UPDATE usuarios SET perfil = ? WHERE perfil = ?", (novo, antigo))


def agora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def hoje():
    return date.today().isoformat()


def registrar_evento(invoice_id, acao, obs=None):
    get_db().execute(
        "INSERT INTO eventos (invoice_id, usuario_id, acao, observacao, criado_em) VALUES (?, ?, ?, ?, ?)",
        (invoice_id, g.usuario["id"] if g.get("usuario") else None, acao, obs, agora()))


def config(chave, padrao=None):
    r = get_db().execute("SELECT valor FROM configuracoes WHERE chave = ?", (chave,)).fetchone()
    return r["valor"] if r and r["valor"] is not None else padrao


def salvar_config(chave, valor):
    get_db().execute("INSERT INTO configuracoes (chave, valor) VALUES (?, ?)"
                     " ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor", (chave, valor))


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


def fmt_moeda(valor, moeda="USD"):
    if not isinstance(valor, (int, float)):
        return "—"
    return f"{MOEDAS.get(moeda, moeda)} {fmt_num(valor)}"


def fmt_qtd(valor):
    if not isinstance(valor, (int, float)):
        return ""
    return f"{valor:g}".replace(".", ",")


def fmt_data(valor):
    if not valor:
        return "—"
    try:
        if len(valor) > 10:
            return datetime.strptime(valor, "%Y-%m-%d %H:%M:%S").strftime("%d/%m/%Y %H:%M")
        return datetime.strptime(valor, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return valor


def somar_dias(data_iso, dias):
    if not data_iso:
        return None
    return (datetime.strptime(data_iso, "%Y-%m-%d") + timedelta(days=int(dias or 0))).strftime("%Y-%m-%d")


def texto_parcela(c, sozinha=False):
    pct = "" if sozinha else f"{fmt_qtd(c['percentual'])}% "
    if c["tipo"] == "ANTECIPADO":
        return f"{pct}antecipado".strip().capitalize() if sozinha else f"{pct}antecipado"
    return f"{pct}{c['dias'] if c['dias'] is not None else '?'} dias após a HAWB"


def condicao_texto(r):
    parcelas = r.get("condicao") or []
    if len(parcelas) == 1:
        return texto_parcela(parcelas[0], sozinha=True)
    return " + ".join(texto_parcela(c) for c in parcelas)


# ---------------------------------------------------------------------------
# Cálculos de cada invoice
# ---------------------------------------------------------------------------
SQL_INVOICE = """
SELECT i.*, f.nome AS fornecedor
FROM invoices i JOIN fornecedores f ON f.id = i.fornecedor_id
"""


def vencimento_parcela(inv, c):
    """Antecipado: data de assinatura + 5 dias úteis. A prazo: data da HAWB (registrada na Coleta) + dias da condição;
    antes da HAWB, a parcela a prazo fica sem vencimento (não há estimativa)."""
    if c["tipo"] == "ANTECIPADO":
        return somar_dias_uteis(inv["data_assinatura"], PRAZO_ANTECIPADO_DIAS)
    return somar_dias(inv["data_hawb"], c["dias"]) if inv["data_hawb"] else None


def calcular_parcelas(r):
    """Divide o valor a pagar entre as parcelas da condição, desconta o valor segurado das últimas
    parcelas e distribui os pagamentos já feitos na ordem de vencimento."""
    V = r["valor_invoice"]
    parcelas = []
    for idx, c in enumerate(r["condicao"]):
        parcelas.append({"idx": idx, "percentual": c["percentual"], "tipo": c["tipo"], "dias": c["dias"],
                         "texto": texto_parcela(c, sozinha=len(r["condicao"]) == 1),
                         "valor": round(V * c["percentual"] / 100, 2), "vencimento": vencimento_parcela(r, c)})
    if parcelas:  # ajuste de centavos na última parcela
        parcelas[-1]["valor"] = round(V - sum(p["valor"] for p in parcelas[:-1]), 2)
    ordem = sorted(range(len(parcelas)), key=lambda i: (parcelas[i]["vencimento"] is None,
                                                        parcelas[i]["vencimento"] or "", i))
    for p in parcelas:
        p["devido"] = p["valor"]
    resto = r["segurado"]
    for i in reversed(ordem):  # o valor segurado sai das parcelas que vencem por último
        tira = min(resto, parcelas[i]["devido"])
        parcelas[i]["devido"] = round(parcelas[i]["devido"] - tira, 2)
        resto -= tira
    pago = r["principal_pago"]
    for i in ordem:
        aloca = min(pago, parcelas[i]["devido"])
        parcelas[i]["pago"] = round(aloca, 2)
        parcelas[i]["pendente"] = round(parcelas[i]["devido"] - aloca, 2)
        pago -= aloca
    return [parcelas[i] for i in ordem]


def data_prevista_chegada(inv):
    return inv["data_chegada"] or inv["data_prog_chegada"] or inv["eta_real"] or inv["eta_planejado"]


def etapa_concluida(inv, etapa):
    if etapa == "CRIACAO":
        return not criacao_faltando(inv)
    return not etapa_faltando(inv, etapa)


def somar_dias_uteis(data_iso, dias):
    """Soma (ou subtrai, se negativo) dias úteis: segunda a sexta; feriados não são considerados.
    Com 0 dias, uma data em fim de semana vai para o próximo dia útil."""
    if not data_iso:
        return None
    data = datetime.strptime(data_iso[:10], "%Y-%m-%d")
    passo = 1 if dias >= 0 else -1
    restantes = abs(dias)
    if restantes == 0:
        while data.weekday() >= 5:
            data += timedelta(days=1)
    while restantes > 0:
        data += timedelta(days=passo)
        if data.weekday() < 5:
            restantes -= 1
    return data.strftime("%Y-%m-%d")


def janela_nacionalizacao(r):
    """Janela de pagamento dos impostos de nacionalização (até o desembaraço):
    ETA planejado -1 a +3 dias úteis; quando houver ETA real, ETA real a +2 dias úteis."""
    if r["cancelado_em"] or r["data_desembaraco"]:
        return None
    if r["eta_real"]:
        ini, fim = NACIONALIZACAO_REAL
        return {"ini": somar_dias_uteis(r["eta_real"], ini), "fim": somar_dias_uteis(r["eta_real"], fim),
                "base": "real", "eta": r["eta_real"]}
    if r["eta_planejado"]:
        ini, fim = NACIONALIZACAO_PLANEJADA
        return {"ini": somar_dias_uteis(r["eta_planejado"], ini), "fim": somar_dias_uteis(r["eta_planejado"], fim),
                "base": "planejado", "eta": r["eta_planejado"]}
    return None


def parcelas_liberacao(r, d):
    """Divide o valor segurado em parcelas de liberação.

    Cada parcela é liberada quando o invoice que recebeu a dedução chega à fábrica (data real de chegada)
    e vence nessa data + 5 dias úteis. Antes da chegada não há vencimento. Se o desconto foi revalidado
    para baixo do segurado, a diferença fica disponível a partir da revalidação.
    """
    S = r["segurado"]
    if not d or S <= TOLERANCIA:
        return []
    parcelas = []
    dif = round(max(0, S - d["valor_final"]), 2)
    if dif > TOLERANCIA:
        rev = get_db().execute("SELECT MAX(criado_em) FROM revisoes_desconto WHERE desconto_id = ?",
                               (d["id"],)).fetchone()[0]
        base = (rev or d["criado_em"])[:10]
        parcelas.append({"valor": dif, "invoice": None, "invoice_id": None, "chegada": base, "status": "DISPONIVEL",
                         "vencimento": somar_dias_uteis(base, PRAZO_LIBERACAO_DIAS_UTEIS),
                         "texto": "Diferença da revalidação do desconto"})
    deducoes = [x for x in r["deducoes_do_desconto"] if not x["cancelado_em"] or x["data_chegada"]]
    deducoes.sort(key=lambda x: (x["data_chegada"] is None, x["data_chegada"] or "", x["id"]))
    acumulado = 0
    for x in deducoes:
        antes = min(S - dif, acumulado)
        acumulado += x["valor"]
        valor = round(min(S - dif, acumulado) - antes, 2)
        if valor <= TOLERANCIA:
            continue
        chegou = bool(x["data_chegada"])
        parcelas.append({"valor": valor, "invoice": x["invoice_destino"], "invoice_id": x["invoice_id"],
                         "chegada": x["data_chegada"], "status": "DISPONIVEL" if chegou else "AGUARDANDO_CHEGADA",
                         "vencimento": somar_dias_uteis(x["data_chegada"], PRAZO_LIBERACAO_DIAS_UTEIS) if chegou else None,
                         "texto": f"Dedução no invoice {x['invoice_destino']}"})
    resto = round(S - dif - min(S - dif, acumulado), 2)
    if resto > TOLERANCIA:
        parcelas.append({"valor": resto, "invoice": None, "invoice_id": None, "chegada": None,
                         "status": "AGUARDANDO_DEDUCAO", "vencimento": None,
                         "texto": "Aguardando dedução em próximo invoice"})
    # o que já foi liberado abate as parcelas na ordem
    pago = r["liberado"]
    for p in parcelas:
        p["pago"] = round(min(p["valor"], pago), 2)
        pago = round(pago - p["pago"], 2)
        p["pendente"] = round(p["valor"] - p["pago"], 2)
    return parcelas


def calcular(inv):
    """Monta um dicionário com o invoice e todos os valores derivados (pagamentos, desconto, situação)."""
    db = get_db()
    r = dict(inv)
    iid = inv["id"]
    r["condicao"] = [dict(c) for c in db.execute(
        "SELECT * FROM condicao_parcelas WHERE invoice_id = ? ORDER BY ordem", (iid,)).fetchall()]
    r["itens"] = db.execute("SELECT * FROM invoice_itens WHERE invoice_id = ? ORDER BY id", (iid,)).fetchall()
    # valor total do invoice: quantidade x preço; o valor final desconta as deduções de descontos
    r["itens_total"] = round(sum((x["quantidade"] or 0) * (x["valor_unit"] or 0) for x in r["itens"]), 2)
    # deduções de descontos de invoices anteriores aplicadas neste invoice
    r["deducoes_recebidas"] = db.execute(
        "SELECT x.*, o.numero AS invoice_origem, o.id AS origem_id FROM deducoes x"
        " JOIN descontos d ON d.id = x.desconto_id JOIN invoices o ON o.id = d.invoice_id"
        " WHERE x.invoice_id = ? ORDER BY x.id", (iid,)).fetchall()
    r["deducoes_aplicadas"] = round(sum(x["valor"] for x in r["deducoes_recebidas"]), 2)
    r["valor_invoice"] = round(r["itens_total"] - r["deducoes_aplicadas"], 2)
    # com deduções, cada item recebe o desconto proporcional ao seu valor (apenas exibição)
    linhas, _t, _td = itens_com_desconto([dict(x) for x in r["itens"]], r["deducoes_aplicadas"])
    r["itens_com_desconto"] = [{"preco": x["preco_desc"], "total": x["total_desc"]} for x in linhas]

    # desconto deste invoice (por má qualidade)
    d = db.execute("SELECT * FROM descontos WHERE invoice_id = ?", (iid,)).fetchone()
    r["desconto"] = d
    r["segurado"] = d["valor_segurado"] if d else 0
    r["deducoes_do_desconto"] = db.execute(
        "SELECT x.*, i.numero AS invoice_destino, i.data_chegada, i.eta_real, i.eta_planejado,"
        " i.data_prog_chegada, i.cancelado_em FROM deducoes x JOIN invoices i ON i.id = x.invoice_id"
        " WHERE x.desconto_id = ? ORDER BY x.id", (d["id"],)).fetchall() if d else []
    r["deduzido"] = round(sum(x["valor"] for x in r["deducoes_do_desconto"]), 2)
    r["deduzido_chegado"] = round(sum(x["valor"] for x in r["deducoes_do_desconto"] if x["data_chegada"]), 2)
    r["saldo_a_deduzir"] = round(d["valor_final"] - r["deduzido"], 2) if d else 0

    # pagamentos
    r["pagamentos"] = db.execute(
        "SELECT p.*, u.nome AS usuario FROM pagamentos p LEFT JOIN usuarios u ON u.id = p.registrado_por"
        " WHERE p.invoice_id = ? ORDER BY p.data_pagamento, p.id", (iid,)).fetchall()
    r["principal_pago"] = round(sum(p["valor"] for p in r["pagamentos"] if p["tipo"] == "PRINCIPAL"), 2)
    r["liberado"] = round(sum(p["valor"] for p in r["pagamentos"] if p["tipo"] == "LIBERACAO"), 2)
    r["principal_devido"] = round(r["valor_invoice"] - r["segurado"], 2)
    r["principal_pendente"] = max(round(r["principal_devido"] - r["principal_pago"], 2), 0)
    # o segurado é liberado conforme os invoices com a dedução chegam; se o valor final do desconto
    # ficar abaixo do segurado, a diferença pode ser liberada na hora
    if d:
        liberavel = min(r["segurado"], r["deduzido_chegado"]) + max(0, r["segurado"] - d["valor_final"])
    else:
        liberavel = 0
    r["liberavel"] = round(liberavel, 2)
    r["liberacao_disponivel"] = max(round(liberavel - r["liberado"], 2), 0)
    r["parcelas_liberacao"] = parcelas_liberacao(r, d)
    r["nacionalizacao"] = janela_nacionalizacao(r)
    r["swifts_pendentes"] = sum(1 for p in r["pagamentos"] if not p["swift_url"])
    r["parcelas"] = calcular_parcelas(r)
    pendentes = [p for p in r["parcelas"] if p["pendente"] > TOLERANCIA]
    # próximo vencimento em aberto (sem data: parcela a prazo de invoice sem data da HAWB)
    r["vencimento"] = pendentes[0]["vencimento"] if pendentes else None
    r["vencimento_estimado"] = False
    r["anexos"] = db.execute("SELECT * FROM anexos WHERE invoice_id = ? ORDER BY id", (iid,)).fetchall()
    r["situacao"] = situacao(r)
    return r


def situacao(r):
    """Situação atual do invoice: primeira etapa ainda não concluída."""
    if r["cancelado_em"]:
        return "CANCELADO"
    if criacao_faltando(r):
        return "CRIACAO"
    for etapa in ORDEM_LOGISTICA:
        if not etapa_concluida(r, etapa):
            return etapa
    d = r["desconto"]
    if r["tem_desconto"] is None or not r["data_fim_inspecao"] or (r["tem_desconto"] and not d):
        return "INSPECAO"
    if r["principal_pendente"] > TOLERANCIA:
        return "PAGAMENTO"
    if d and (r["saldo_a_deduzir"] > TOLERANCIA or any(not x["data_chegada"] for x in r["deducoes_do_desconto"])):
        return "DEDUCAO"
    if r["segurado"] - r["liberado"] > TOLERANCIA:
        return "LIBERACAO"
    if r["swifts_pendentes"]:
        return "SWIFT"
    return "CONCLUIDO"


def carregar_invoice(iid):
    inv = get_db().execute(SQL_INVOICE + " WHERE i.id = ?", (iid,)).fetchone()
    if inv is None:
        abort(404)
    return calcular(inv)


def todos_invoices(incluir_cancelados=False):
    sql = SQL_INVOICE + ("" if incluir_cancelados else " WHERE i.cancelado_em IS NULL") + " ORDER BY i.id DESC"
    return [calcular(inv) for inv in get_db().execute(sql).fetchall()]


def codigo_invoice(r):
    return f"{r['fornecedor']} · {r['numero']}"


def descontos_disponiveis(fornecedor_id, moeda, excluir_invoice_id=None):
    """Descontos aprovados do fornecedor, na mesma moeda, com saldo a deduzir."""
    lista = []
    for d in get_db().execute(
            "SELECT d.*, i.numero, i.moeda, i.id AS origem_id FROM descontos d JOIN invoices i ON i.id = d.invoice_id"
            " WHERE i.fornecedor_id = ? AND i.moeda = ? AND d.status = 'APROVADO' AND i.cancelado_em IS NULL"
            " ORDER BY d.id", (fornecedor_id, moeda)).fetchall():
        if d["origem_id"] == excluir_invoice_id:
            continue
        deduzido = get_db().execute("SELECT COALESCE(SUM(valor), 0) FROM deducoes WHERE desconto_id = ?",
                                    (d["id"],)).fetchone()[0]
        saldo = round(d["valor_final"] - deduzido, 2)
        if saldo > TOLERANCIA:
            lista.append({"id": d["id"], "invoice": d["numero"], "valor_final": d["valor_final"], "saldo": saldo,
                          "data_relatorio": d["data_relatorio"]})
    return lista


app.jinja_env.globals.update(
    csrf_token=lambda: csrf_token(), PERFIS=PERFIS, SETORES=SETORES, MOEDAS=MOEDAS, CONDICOES=CONDICOES, SITUACOES=SITUACOES,
    CAMPOS_ETAPA=CAMPOS_ETAPA, TITULO_ETAPA=TITULO_ETAPA, ORDEM_LOGISTICA=ORDEM_LOGISTICA, CORES_CANAL=CORES_CANAL,
    STATUS_DESCONTO=STATUS_DESCONTO, TIPOS_PAGAMENTO=TIPOS_PAGAMENTO,
    pode=lambda perfil: pode(perfil), MAX_USUARIOS=MAX_USUARIOS, VERSAO=VERSAO, condicao_texto=condicao_texto,
    etapa_concluida=etapa_concluida, PRAZO_ANTECIPADO_DIAS=PRAZO_ANTECIPADO_DIAS, PRAZO_LIBERACAO_DIAS_UTEIS=PRAZO_LIBERACAO_DIAS_UTEIS, etapa_faltando=lambda r, e: etapa_faltando(r, e),
    criacao_faltando=lambda r: criacao_faltando(r), opcionais_faltando=lambda r: opcionais_faltando(r),
)
app.jinja_env.filters["moeda"] = fmt_moeda
app.jinja_env.filters["num"] = fmt_num
app.jinja_env.filters["num4"] = lambda v: (f"{v:,.4f}".replace(",", "X").replace(".", ",").replace("X", ".")
                                           if isinstance(v, (int, float)) else "")
app.jinja_env.filters["qtd"] = fmt_qtd
app.jinja_env.filters["data"] = fmt_data


def parse_num(texto):
    """Aceita 1.234,56 / 1234,56 / 1,234.56 / 1234.56 / 10.000 / US$ 10.000,00 / R$ 10,00 / € 10,00."""
    if texto is None:
        return None
    s = str(texto).strip().replace(" ", "")
    for simbolo in ("US$", "R$", "€", "$"):
        s = s.replace(simbolo, "")
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
# Painel e pendências
# ---------------------------------------------------------------------------
def listar_pendencias(invoices, perfil=None):
    """Pendências de todos os setores (todos veem tudo); as do setor do usuário vêm primeiro.
    Ver não dá permissão de preencher: cada tela continua restrita ao seu setor."""
    perfil = perfil or g.usuario["perfil"]
    adm = imp = fin = True
    pend = []

    def add(r, acao, quem, data=None, valor=None, alerta=False):
        pend.append({"r": r, "acao": acao, "quem": quem, "data": data, "valor": valor, "alerta": alerta})

    lim_7 = somar_dias(hoje(), 7)
    for r in invoices:
        sit = r["situacao"]
        if sit == "CANCELADO":
            continue
        if imp and sit == "CRIACAO":
            add(r, "Completar o cadastro: falta " + ", ".join(criacao_faltando(r)), "importacao")
        if imp and sit in ORDEM_LOGISTICA:
            # o que a Importação precisa preencher para concluir a etapa (o banco de débito é pendência do Financeiro)
            falta = [f for f in etapa_faltando(r, sit) if not f.startswith("Banco de débito")]
            if falta:
                add(r, f"{TITULO_ETAPA[sit]}: falta " + ", ".join(falta), "importacao",
                    r["data_prog_chegada"] if sit == "CHEGADA" else None)
        opcionais = opcionais_faltando(r)
        if imp and opcionais:
            add(r, "Informações não obrigatórias a preencher: " + ", ".join(opcionais), "importacao")
        if adm and sit == "INSPECAO":
            add(r, "Registrar a inspeção", "admin", r["data_chegada"])
        d = r["desconto"]
        if adm and d and d["status"] == "ENVIADO":
            add(r, "Acompanhar a aprovação do desconto pelo fornecedor", "admin", d["data_relatorio"], d["valor_final"])
        if imp and d and d["status"] == "APROVADO" and r["saldo_a_deduzir"] > TOLERANCIA:
            add(r, "Desconto aprovado aguardando dedução em um próximo invoice", "importacao", None, r["saldo_a_deduzir"])
        if fin:
            multiplas = len(r["parcelas"]) > 1
            for p in r["parcelas"]:
                if p["pendente"] <= TOLERANCIA:
                    continue
                rotulo = f" · parcela {p['texto']}" if multiplas else ""
                if not p["vencimento"]:
                    add(r, "Pagar invoice" + rotulo + " (vencimento depende da data da HAWB)", "financeiro",
                        None, p["pendente"])
                    continue
                vencido = p["vencimento"] < hoje()
                add(r, ("Pagamento vencido" if vencido else "Pagar invoice") + rotulo, "financeiro", p["vencimento"],
                    p["pendente"], alerta=vencido or p["vencimento"] <= lim_7)
        if fin:
            for p in r["parcelas_liberacao"]:
                if p["status"] != "DISPONIVEL" or p["pendente"] <= TOLERANCIA:
                    continue
                vencido = p["vencimento"] < hoje()
                add(r, ("Liberação do segurado vencida" if vencido else "Liberar valor segurado")
                    + (f" (invoice {p['invoice']} chegou)" if p["invoice"] else " (revalidação)"), "financeiro",
                    p["vencimento"], p["pendente"], alerta=vencido or p["vencimento"] <= lim_7)
        if fin and r["swifts_pendentes"]:
            add(r, f"Anexar Swift ({r['swifts_pendentes']} pagamento(s))", "financeiro")
        if fin and not r["banco_debito"] and (r["etd_planejado"] or r["etd_real"]):
            add(r, "Informar o banco de débito", "financeiro")
    for p in pend:
        p["minha"] = p["quem"] == perfil
    ordem_setor = list(SETORES)
    pend.sort(key=lambda p: (not p["minha"], ordem_setor.index(p["quem"]) if not p["minha"] else 0,
                             p["data"] is None, p["data"] or ""))
    return pend


@app.route("/")
@login_obrigatorio
def painel():
    invoices = todos_invoices(incluir_cancelados=True)
    filtro_sit = request.args.get("situacao", "")
    filtro_forn = request.args.get("fornecedor", "")
    filtro_moeda = request.args.get("moeda", "")
    busca = request.args.get("q", "").strip().lower()
    hj, lim_30 = hoje(), somar_dias(hoje(), 30)
    ativos = [r for r in invoices if r["situacao"] != "CANCELADO"]
    contagem = {
        "logistica": sum(1 for r in ativos if r["situacao"] in ["CRIACAO"] + ORDEM_LOGISTICA),
        "inspecao": sum(1 for r in ativos if r["situacao"] == "INSPECAO"),
        "vencidos": sum(1 for r in ativos for p in r["parcelas"] if p["pendente"] > TOLERANCIA
                        and p["vencimento"] and p["vencimento"] < hj),
        "vencer_30": sum(1 for r in ativos for p in r["parcelas"] if p["pendente"] > TOLERANCIA
                         and p["vencimento"] and hj <= p["vencimento"] <= lim_30),
        "liberacoes": sum(1 for r in ativos if r["liberacao_disponivel"] > TOLERANCIA),
        "concluidos": sum(1 for r in ativos if r["situacao"] == "CONCLUIDO"),
    }
    lista = invoices
    if filtro_sit == "ABERTOS":
        lista = [r for r in lista if r["situacao"] not in ("CONCLUIDO", "CANCELADO")]
    elif filtro_sit == "LOGISTICA":
        lista = [r for r in lista if r["situacao"] in ["CRIACAO"] + ORDEM_LOGISTICA]
    elif filtro_sit in SITUACOES:
        lista = [r for r in lista if r["situacao"] == filtro_sit]
    if filtro_forn.isdigit():
        lista = [r for r in lista if r["fornecedor_id"] == int(filtro_forn)]
    if filtro_moeda in MOEDAS:
        lista = [r for r in lista if r["moeda"] == filtro_moeda]
    if busca:
        lista = [r for r in lista if busca in r["numero"].lower() or busca in r["fornecedor"].lower()]
    fornecedores = get_db().execute("SELECT id, nome FROM fornecedores ORDER BY nome").fetchall()
    return render_template("painel.html", invoices=lista, pendencias=listar_pendencias(invoices),
                           contagem=contagem, fornecedores=fornecedores, filtro_sit=filtro_sit,
                           filtro_forn=filtro_forn, filtro_moeda=filtro_moeda, busca=busca, hoje=hj)


# ---------------------------------------------------------------------------
# Fluxo de pagamentos (previsibilidade para o Financeiro)
# ---------------------------------------------------------------------------
def itens_fluxo(invoices):
    """Pagamentos previstos: invoices a pagar, liberações do valor segurado e custo estimado de importação."""
    itens = []
    hj = hoje()
    for r in invoices:
        if r["situacao"] in ("CANCELADO", "CONCLUIDO"):
            continue
        base = {"invoice_id": r["id"], "fornecedor": r["fornecedor"], "numero": r["numero"], "moeda": r["moeda"]}
        if not r["parcelas"] and r["principal_pendente"] > TOLERANCIA:
            itens.append(dict(base, categoria="PAGAMENTO", tipo="Pagamento do invoice · condição de pagamento não "
                              "informada", data=None, estimado=False, valor=r["principal_pendente"]))
        for p in r["parcelas"]:
            if p["pendente"] <= TOLERANCIA:
                continue
            tipo = "Pagamento do invoice"
            if len(r["parcelas"]) > 1:
                tipo += f" · parcela {p['texto']}"
            if p["devido"] + TOLERANCIA < p["valor"]:
                tipo += f" (com {fmt_moeda(p['valor'] - p['devido'], r['moeda'])} segurado)"
            elif p["pago"] > TOLERANCIA:
                tipo += " (saldo)"
            if not p["vencimento"]:
                tipo += " · aguardando data da HAWB"
            itens.append(dict(base, categoria="PAGAMENTO", tipo=tipo, data=p["vencimento"], estimado=False,
                              valor=p["pendente"]))
        for p in r["parcelas_liberacao"]:
            if p["pendente"] <= TOLERANCIA:
                continue
            if p["status"] == "DISPONIVEL":
                tipo = ("Liberação do segurado · " + (f"invoice {p['invoice']} chegou em {fmt_data(p['chegada'])}"
                        if p["invoice"] else "diferença da revalidação") + f" (+{PRAZO_LIBERACAO_DIAS_UTEIS} dias úteis)")
            elif p["status"] == "AGUARDANDO_CHEGADA":
                tipo = f"Liberação do segurado · aguardando a chegada do invoice {p['invoice']} na Quinelato"
            else:
                tipo = "Liberação do segurado · aguardando dedução em próximo invoice"
            itens.append(dict(base, categoria="LIBERACAO", tipo=tipo, data=p["vencimento"],
                              estimado=p["status"] != "DISPONIVEL", valor=p["pendente"]))
        jan = r["nacionalizacao"]
        if (jan or r["etd_planejado"] or r["etd_real"]) and not r["data_desembaraco"]:
            tipo = "Impostos de nacionalização (estimativa: 40% do invoice)"
            tipo += (f" · pagar entre {fmt_data(jan['ini'])} e {fmt_data(jan['fim'])} (ETA {jan['base']})" if jan
                     else " · aguardando o ETA planejado")
            itens.append(dict(base, categoria="CUSTO", tipo=tipo, data=jan["ini"] if jan else None, estimado=True,
                              valor=round(r["itens_total"] * CUSTO_IMPORTACAO_ESTIMADO, 2)))
    for it in itens:
        if it["data"] and it["data"] < hj and it["categoria"] != "CUSTO":
            it["situacao"] = "Vencido" if it["categoria"] == "PAGAMENTO" or not it["estimado"] else "Disponível"
        elif it["categoria"] == "LIBERACAO" and not it["data"]:
            it["situacao"] = "Aguardando chegada" if "chegada" in it["tipo"] else "Aguardando dedução"
        else:
            it["situacao"] = "Estimado" if it["estimado"] else "Confirmado"
    itens.sort(key=lambda it: (it["data"] is None, it["data"] or ""))
    return itens


def filtrar_fluxo(itens, args):
    periodo = args.get("periodo", "90")
    if periodo in ("30", "60", "90", "180"):
        limite = somar_dias(hoje(), int(periodo))
        itens = [it for it in itens if it["data"] is None or it["data"] <= limite]
    if args.get("moeda") in MOEDAS:
        itens = [it for it in itens if it["moeda"] == args["moeda"]]
    if args.get("fornecedor", "").isdigit():
        nome = get_db().execute("SELECT nome FROM fornecedores WHERE id = ?", (int(args["fornecedor"]),)).fetchone()
        itens = [it for it in itens if nome and it["fornecedor"] == nome["nome"]]
    if args.get("custos", "1") != "1":
        itens = [it for it in itens if it["categoria"] != "CUSTO"]
    if args.get("estimados", "1") != "1":
        itens = [it for it in itens if not it["estimado"]]
    return itens


@app.route("/fluxo-pagamentos")
@login_obrigatorio
def fluxo_pagamentos():
    itens = filtrar_fluxo(itens_fluxo(todos_invoices()), request.args)
    grupos = {}
    for it in itens:
        chave = it["data"][:7] if it["data"] else "sem-data"
        grupo = grupos.setdefault(chave, {"itens": [], "totais": {}})
        grupo["itens"].append(it)
        grupo["totais"][it["moeda"]] = round(grupo["totais"].get(it["moeda"], 0) + it["valor"], 2)
    totais = {}
    for it in itens:
        totais[it["moeda"]] = round(totais.get(it["moeda"], 0) + it["valor"], 2)
    meses = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro",
             "outubro", "novembro", "dezembro"]
    titulos = {k: ("Sem data definida" if k == "sem-data" else f"{meses[int(k[5:7]) - 1].capitalize()} de {k[:4]}")
               for k in grupos}
    fornecedores = get_db().execute("SELECT id, nome FROM fornecedores ORDER BY nome").fetchall()
    return render_template("fluxo.html", grupos=grupos, titulos=titulos, totais=totais, args=request.args,
                           fornecedores=fornecedores, hoje=hoje())


@app.route("/fluxo-pagamentos.csv")
@login_obrigatorio
def fluxo_pagamentos_csv():
    itens = filtrar_fluxo(itens_fluxo(todos_invoices()), request.args)
    saida = io.StringIO()
    w = csv.writer(saida, delimiter=";")
    w.writerow(["Data", "Situação", "Tipo", "Fornecedor", "Invoice", "Moeda", "Valor"])
    for it in itens:
        w.writerow([fmt_data(it["data"]) if it["data"] else "", it["situacao"], it["tipo"], it["fornecedor"],
                    it["numero"], it["moeda"], fmt_num(it["valor"])])
    nome = f"fluxo-pagamentos-{hoje()}.csv"
    # BOM para o Excel reconhecer acentos
    return Response("﻿" + saida.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={nome}"})


# ---------------------------------------------------------------------------
# Criação e edição do invoice (Importação)
# ---------------------------------------------------------------------------
def obter_fornecedor(nome, criar=True):
    db = get_db()
    f = db.execute("SELECT id FROM fornecedores WHERE lower(nome) = lower(?)", (nome,)).fetchone()
    if f:
        return f["id"]
    if not criar:
        return None
    return db.execute("INSERT INTO fornecedores (nome, criado_em) VALUES (?, ?)", (nome, agora())).lastrowid


def data_valida(texto):
    if not texto:
        return None
    try:
        return datetime.strptime(texto, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def link_valido(url):
    return bool(re.fullmatch(r"https://\S+", url or ""))


def ler_form_invoice():
    """Lê o cadastro do invoice. Salvar sempre é possível: só fornecedor e nº do invoice são exigidos
    (identificam o processo). O que faltar fica pendente e impede o avanço da etapa, não o salvamento."""
    f = request.form
    dados = {
        "fornecedor": " ".join(f.get("fornecedor", "").split()),
        "numero": f.get("numero", "").strip(),
        "moeda": f.get("moeda", "") if f.get("moeda", "") in MOEDAS else "USD",
        "data_assinatura": data_valida(f.get("data_assinatura")) or "",
        "agente_carga": f.get("agente_carga", "").strip(),
        "despachante": f.get("despachante", "").strip(),
        "grupo_consolidacao": f.get("grupo_consolidacao", "").strip(),
        "data_prog_coleta": data_valida(f.get("data_prog_coleta")),
        "obs": f.get("obs", "").strip(),
    }
    erros = []
    if not dados["fornecedor"]:
        erros.append("Informe o fornecedor.")
    if not dados["numero"]:
        erros.append("Informe o número do invoice.")
    itens = []
    for cod, desc, qtd, vu in zip(f.getlist("item_codigo"), f.getlist("item_desc"), f.getlist("item_qtd"),
                                  f.getlist("item_vu")):
        if not any(x.strip() for x in (cod, desc, qtd, vu)):
            continue
        preco, quantidade = parse_num(vu), parse_num(qtd)
        if (quantidade is not None and quantidade < 0) or (preco is not None and preco < 0):
            erros.append(f"Item {cod.strip() or len(itens) + 1}: quantidade e preço não podem ser negativos.")
        itens.append({"codigo": cod.strip(), "item": desc.strip(), "quantidade": quantidade,
                      "valor_unit_original": preco, "valor_unit": preco})
    condicao = []
    for pct, tipo, dias in zip(f.getlist("cond_pct"), f.getlist("cond_tipo"), f.getlist("cond_dias")):
        percentual = parse_num(pct)
        if percentual is None:
            continue
        if percentual <= 0 or percentual > 100:
            erros.append("Condição de pagamento: cada percentual deve estar entre 0 e 100.")
        condicao.append({"percentual": percentual, "tipo": tipo if tipo in CONDICOES else "PRAZO",
                         "dias": int(dias) if tipo == "PRAZO" and dias.strip().isdigit() else None})
    dados["condicao"] = condicao
    unica = condicao[0] if len(condicao) == 1 else None
    dados["condicao_tipo"] = (unica["tipo"] if unica else "MISTA") if condicao else ""
    dados["condicao_dias"] = unica["dias"] if unica else None
    return dados, itens, erros


# Cadastro do invoice: obrigatórios para concluir a etapa de criação e opcionais que ficam pendentes no painel
OPCIONAIS_CADASTRO = [("agente_carga", "Agente de carga"), ("despachante", "Despachante"),
                      ("data_prog_coleta", "Data programada de coleta")]
# campos não obrigatórios que não geram pendência (grupo de consolidação também não, por não estar na lista acima)
SEM_PENDENCIA = {"exigencias_anvisa"}


def criacao_faltando(r):
    """Informações obrigatórias do cadastro que ainda faltam (impedem o avanço do processo)."""
    falta = []
    if not r["data_assinatura"]:
        falta.append("Data de assinatura")
    condicao = r.get("condicao") or []
    if not condicao:
        falta.append("Condição de pagamento")
    else:
        if abs(sum(c["percentual"] for c in condicao) - 100) > 0.01:
            falta.append("Condição de pagamento somando 100%")
        if any(c["tipo"] == "PRAZO" and c["dias"] is None for c in condicao):
            falta.append("Dias após a HAWB da condição")
    itens = r.get("itens") or []
    if not itens:
        falta.append("Itens")
    elif any(not x["codigo"] or not x["quantidade"] or x["valor_unit"] is None for x in itens):
        falta.append("Código, quantidade e preço de todos os itens")
    return falta


def opcionais_faltando(r):
    """Informações não obrigatórias ainda vazias (ficam como pendência no painel)."""
    falta = [rot for campo, rot in OPCIONAIS_CADASTRO if not r[campo]]
    for etapa in ORDEM_LOGISTICA:
        falta += [rot for campo, rot, _t, obrig in CAMPOS_ETAPA[etapa]
                  if not obrig and campo not in SEM_PENDENCIA and not r[campo]]
    return falta


def etapa_faltando(r, etapa):
    """Informações obrigatórias de uma etapa logística que ainda faltam."""
    falta = [rot for campo, rot, _t, obrig in CAMPOS_ETAPA[etapa] if obrig and not r[campo]]
    if etapa == "EMBARQUE" and not r["banco_debito"]:
        falta.append("Banco de débito (Financeiro)")
    return falta


def avisar_cadastro_pendente(iid):
    r = carregar_invoice(iid)
    falta = criacao_faltando(r)
    if falta:
        flash("Cadastro salvo, mas incompleto: o processo só avança com " + ", ".join(falta) + ".", "aviso-box")


def salvar_condicao(invoice_id, condicao):
    db = get_db()
    db.execute("DELETE FROM condicao_parcelas WHERE invoice_id = ?", (invoice_id,))
    for ordem, c in enumerate(condicao, 1):
        db.execute("INSERT INTO condicao_parcelas (invoice_id, ordem, percentual, tipo, dias) VALUES (?, ?, ?, ?, ?)",
                   (invoice_id, ordem, round(c["percentual"], 4), c["tipo"], c["dias"]))


def salvar_itens(invoice_id, itens):
    db = get_db()
    db.execute("DELETE FROM invoice_itens WHERE invoice_id = ?", (invoice_id,))
    for it in itens:
        db.execute("INSERT INTO invoice_itens (invoice_id, codigo, item, quantidade, valor_unit_original, valor_unit)"
                   " VALUES (?, ?, ?, ?, ?, ?)",
                   (invoice_id, it.get("codigo"), it["item"] or "", it["quantidade"], it["valor_unit_original"],
                    it["valor_unit"]))


# --- Carga de itens por planilha (Excel .xlsx ou CSV) ------------------------------------------
COLUNAS_PLANILHA = {  # cabeçalho normalizado -> campo
    "codigo": "codigo", "cod": "codigo", "codigo do item": "codigo",
    "descricao": "item", "descricao do item": "item", "item": "item",
    "quantidade": "quantidade", "qtd": "quantidade", "qtde": "quantidade",
    "preco original": "valor_unit", "valor original": "valor_unit", "preco": "valor_unit",
    "preco unitario": "valor_unit", "valor unitario": "valor_unit", "preco unitario original": "valor_unit",
    "valor unitario original": "valor_unit",
}
MAX_LINHAS_PLANILHA = 2000
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # planilhas de até 5 MB


def normalizar(texto):
    texto = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", texto.lower()).split())


def ler_planilha(arquivo):
    """Lê uma planilha de itens e devolve (itens, erros). Aceita .xlsx e .csv."""
    nome = (arquivo.filename or "").lower()
    if nome.endswith(".xlsx") or nome.endswith(".xlsm"):
        import openpyxl
        try:
            wb = openpyxl.load_workbook(arquivo, read_only=True, data_only=True)
        except Exception:
            return [], ["Não foi possível abrir a planilha. Salve-a como Excel (.xlsx) e tente novamente."]
        linhas = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    elif nome.endswith(".csv"):
        texto = arquivo.read().decode("utf-8-sig", errors="replace")
        separador = ";" if texto.count(";") >= texto.count(",") else ","
        linhas = list(csv.reader(io.StringIO(texto), delimiter=separador))
    else:
        return [], ["Envie uma planilha Excel (.xlsx) ou CSV. Arquivos .xls antigos: salve como .xlsx."]
    # localiza a linha de cabeçalho (a primeira que tenha a coluna Código)
    for pos, linha in enumerate(linhas[:20]):
        mapa = {i: COLUNAS_PLANILHA[normalizar(c)] for i, c in enumerate(linha) if normalizar(c) in COLUNAS_PLANILHA}
        if "codigo" in mapa.values():
            break
    else:
        return [], ["Cabeçalho não encontrado: a planilha deve ter as colunas Código, Descrição, Quantidade e "
                    "Preço Original."]
    faltando = [n for c, n in (("quantidade", "Quantidade"), ("valor_unit", "Preço Original"))
                if c not in mapa.values()]
    if faltando:
        return [], ["Coluna(s) não encontrada(s): " + ", ".join(faltando) + "."]
    itens, erros = [], []
    for num_linha, linha in enumerate(linhas[pos + 1:], start=pos + 2):
        valores = {campo: (linha[i] if i < len(linha) else None) for i, campo in mapa.items()}
        if all(v is None or str(v).strip() == "" for v in valores.values()):
            continue
        def numero(v):
            return float(v) if isinstance(v, (int, float)) else parse_num(v)
        it = {"codigo": str(valores.get("codigo") or "").strip(),
              "item": str(valores.get("item") or "").strip(),
              "quantidade": numero(valores.get("quantidade")),
              "valor_unit": numero(valores.get("valor_unit"))}
        it["valor_unit_original"] = it["valor_unit"]
        if it["codigo"].endswith(".0") and it["codigo"][:-2].isdigit():  # código numérico lido como número
            it["codigo"] = it["codigo"][:-2]
        if not it["codigo"] or not it["quantidade"] or it["quantidade"] <= 0 or it["valor_unit"] is None \
                or it["valor_unit"] < 0:
            erros.append(f"Linha {num_linha}: código, quantidade e preço são obrigatórios.")
            continue
        itens.append(it)
        if len(itens) > MAX_LINHAS_PLANILHA:
            return [], [f"A planilha tem mais de {MAX_LINHAS_PLANILHA} itens."]
    if not itens and not erros:
        erros.append("Nenhum item encontrado na planilha.")
    return itens, erros


@app.route("/invoices/importar-itens", methods=["POST"])
@login_obrigatorio
def importar_itens():
    if not pode("importacao"):
        abort(403)
    arquivo = request.files.get("arquivo")
    if not arquivo or not arquivo.filename:
        return jsonify({"itens": [], "erros": ["Selecione a planilha."]})
    itens, erros = ler_planilha(arquivo)
    for it in itens:
        it["quantidade_fmt"] = fmt_qtd(it["quantidade"])
        it["valor_unit_fmt"] = fmt_num(it["valor_unit"])
    return jsonify({"itens": itens, "erros": erros})


def itens_com_desconto(itens, deducoes):
    """Calcula, para exibição/exportação, o preço e o total com desconto de cada item (rateio proporcional)."""
    total = round(sum((it["quantidade"] or 0) * (it["valor_unit"] or 0) for it in itens), 2)
    total_desc = round(total - deducoes, 2)
    fator = total_desc / total if total else 1
    linhas, acumulado = [], 0
    for n, it in enumerate(itens, 1):
        t = round((it["quantidade"] or 0) * (it["valor_unit"] or 0), 2)
        td = round(t * fator, 2) if n < len(itens) else round(total_desc - acumulado, 2)
        acumulado += td
        linhas.append(dict(it, total=t, preco_desc=(it["valor_unit"] or 0) * fator, total_desc=td))
    return linhas, total, total_desc


def planilha_itens(itens, deducoes, moeda, cabecalho):
    """Gera o .xlsx da tabela de itens com os valores calculados."""
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    linhas, total, total_desc = itens_com_desconto(itens, deducoes)
    com_desc = deducoes > TOLERANCIA
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Itens"
    for rotulo, valor in cabecalho:
        ws.append([rotulo, valor])
        ws.cell(ws.max_row, 1).font = Font(bold=True)
    ws.append([])
    colunas = ["Código", "Descrição", "Quantidade", f"Preço Original ({moeda})", f"Total ({moeda})"]
    if com_desc:
        colunas += [f"Preço com Desconto ({moeda})", f"Total com Desconto ({moeda})"]
    ws.append(colunas)
    linha_cab = ws.max_row
    destaque = PatternFill("solid", start_color="E6F3EE")
    for c in ws[linha_cab]:
        c.font = Font(bold=True)
        c.alignment = Alignment(horizontal="center", wrap_text=True)
    for it in linhas:
        valores = [it.get("codigo") or "", it.get("item") or "", it["quantidade"], it["valor_unit"], it["total"]]
        if com_desc:
            valores += [round(it["preco_desc"], 4), it["total_desc"]]
        ws.append(valores)
    ultima = ws.max_row
    ws.append(["", "Valor total do invoice", None, None, total] + (["", total_desc] if com_desc else []))
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    if com_desc:
        ws.append(["", "(−) Descontos aplicados", None, None, deducoes])
        ws.append(["", "Valor final", None, None, total_desc])
        for r in (ws.max_row - 1, ws.max_row):
            for c in ws[r]:
                c.font = Font(bold=True)
    for linha in ws.iter_rows(min_row=linha_cab + 1, max_row=ws.max_row):
        for c in linha:
            if c.column == 3:
                c.number_format = "#,##0.##"
            elif c.column == 6:
                c.number_format = "#,##0.0000"
            elif c.column >= 4:
                c.number_format = "#,##0.00"
            if com_desc and c.column >= 6 and c.row <= ultima:
                c.fill = destaque
    for col, largura in zip("ABCDEFG", (16, 42, 12, 18, 16, 22, 22)):
        ws.column_dimensions[col].width = largura
    ws.freeze_panes = ws.cell(linha_cab + 1, 1)
    saida = io.BytesIO()
    wb.save(saida)
    return saida.getvalue()


def resposta_xlsx(conteudo, nome):
    nome = re.sub(r"[^A-Za-z0-9._-]+", "_", nome)
    return Response(conteudo, headers={"Content-Disposition": f"attachment; filename={nome}"},
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/invoices/<int:id>/itens.xlsx")
@login_obrigatorio
def exportar_itens(id):
    r = carregar_invoice(id)
    itens = [dict(x) for x in r["itens"]]
    cabecalho = [("Fornecedor", r["fornecedor"]), ("Invoice", r["numero"]), ("Moeda", r["moeda"]),
                 ("Data de assinatura", fmt_data(r["data_assinatura"])), ("Condição de pagamento", condicao_texto(r))]
    for x in r["deducoes_recebidas"]:
        cabecalho.append((f"Desconto deduzido (invoice {x['invoice_origem']})", x["valor"]))
    return resposta_xlsx(planilha_itens(itens, r["deducoes_aplicadas"], r["moeda"], cabecalho),
                         f"itens-{r['fornecedor']}-{r['numero']}.xlsx")


@app.route("/invoices/itens-tela.xlsx", methods=["POST"])
@login_obrigatorio
def exportar_itens_tela():
    """Exporta os itens que estão na tela de cadastro/correção, antes de salvar."""
    if not pode("importacao"):
        abort(403)
    dados, itens, _erros = ler_form_invoice()
    itens = [it for it in itens if it["quantidade"] and it["valor_unit"] is not None]
    if not itens:
        flash("Não há itens válidos para exportar.", "erro")
        return redirect(request.referrer or url_for("invoice_novo"))
    deducoes = parse_num(request.form.get("deducoes_existentes")) or 0
    deducoes += sum(parse_num(v) or 0 for k, v in request.form.items() if k.startswith("ded_"))
    cabecalho = [("Fornecedor", dados["fornecedor"]), ("Invoice", dados["numero"] or "(não salvo)"),
                 ("Moeda", dados["moeda"]), ("Situação", "Rascunho exportado antes de salvar")]
    if deducoes > TOLERANCIA:
        cabecalho.append(("Descontos aplicados", round(deducoes, 2)))
    return resposta_xlsx(planilha_itens(itens, round(deducoes, 2), dados["moeda"], cabecalho),
                         f"itens-{dados['fornecedor'] or 'invoice'}-{dados['numero'] or 'rascunho'}.xlsx")


@app.route("/invoices/modelo-itens.xlsx")
@login_obrigatorio
def modelo_itens():
    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Itens"
    ws.append(["Código", "Descrição", "Quantidade", "Preço Original"])
    for c in ws[1]:
        c.font = Font(bold=True)
    ws.append(["ABC-001", "Exemplo: pinça 15 cm (apague esta linha)", 100, 5.5])
    for col, largura in zip("ABCD", (16, 45, 12, 16)):
        ws.column_dimensions[col].width = largura
    saida = io.BytesIO()
    wb.save(saida)
    return Response(saida.getvalue(), headers={"Content-Disposition": "attachment; filename=modelo-itens-invoice.xlsx"},
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/api/descontos-disponiveis")
@login_obrigatorio
def api_descontos_disponiveis():
    fid = obter_fornecedor(" ".join(request.args.get("fornecedor", "").split()), criar=False)
    moeda = request.args.get("moeda", "")
    if not fid or moeda not in MOEDAS:
        return jsonify([])
    lista = descontos_disponiveis(fid, moeda, excluir_invoice_id=request.args.get("excluir", type=int))
    for d in lista:
        d["saldo_fmt"] = fmt_moeda(d["saldo"], moeda)
        d["saldo_num"] = fmt_num(d["saldo"])
        d["data_relatorio_fmt"] = fmt_data(d["data_relatorio"])
    return jsonify(lista)


@app.route("/invoices/novo", methods=["GET", "POST"])
@login_obrigatorio
def invoice_novo():
    if not pode("importacao"):
        abort(403)
    db = get_db()
    fornecedores = db.execute("SELECT * FROM fornecedores WHERE ativo = 1 ORDER BY nome").fetchall()
    if request.method == "POST":
        dados, itens, erros = ler_form_invoice()
        fid = obter_fornecedor(dados["fornecedor"], criar=False) if dados["fornecedor"] else None
        if fid and db.execute("SELECT 1 FROM invoices WHERE fornecedor_id = ? AND lower(numero) = lower(?)",
                              (fid, dados["numero"])).fetchone():
            erros.append(f"O invoice {dados['numero']} deste fornecedor já está cadastrado.")
        # janela de descontos aprovados: deduções escolhidas na criação
        deducoes = []
        disponiveis = {d["id"]: d for d in descontos_disponiveis(fid, dados["moeda"])} if fid else {}
        for chave, valor in request.form.items():
            if chave.startswith("ded_") and valor.strip():
                did, v = int(chave[4:]), parse_num(valor)
                if v is None or v <= 0:
                    continue
                if did not in disponiveis:
                    erros.append("Um dos descontos escolhidos não está mais disponível.")
                elif v > disponiveis[did]["saldo"] + TOLERANCIA:
                    erros.append(f"A dedução do desconto do invoice {disponiveis[did]['invoice']} é maior que o saldo"
                                 f" ({fmt_moeda(disponiveis[did]['saldo'], dados['moeda'])}).")
                else:
                    deducoes.append((did, round(v, 2)))
        total_itens = sum((it["quantidade"] or 0) * (it["valor_unit"] or 0) for it in itens)
        if sum(v for _d, v in deducoes) > total_itens + TOLERANCIA:
            erros.append("O total das deduções não pode ser maior que o valor dos itens do invoice.")
        if not erros:
            cur = db.execute(
                "INSERT INTO invoices (fornecedor_id, numero, moeda, data_assinatura, condicao_tipo, condicao_dias,"
                " agente_carga, despachante, grupo_consolidacao, data_prog_coleta, obs, criado_por, criado_em)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (obter_fornecedor(dados["fornecedor"]), dados["numero"], dados["moeda"], dados["data_assinatura"],
                 dados["condicao_tipo"], dados["condicao_dias"], dados["agente_carga"], dados["despachante"],
                 dados["grupo_consolidacao"], dados["data_prog_coleta"], dados["obs"], g.usuario["id"], agora()))
            iid = cur.lastrowid
            salvar_itens(iid, itens)
            salvar_condicao(iid, dados["condicao"])
            registrar_evento(iid, "Invoice criado", f"{len(itens)} item(ns) · {fmt_moeda(total_itens, dados['moeda'])}")
            for did, v in deducoes:
                db.execute("INSERT INTO deducoes (desconto_id, invoice_id, valor, criado_por, criado_em)"
                           " VALUES (?, ?, ?, ?, ?)", (did, iid, v, g.usuario["id"], agora()))
                origem = disponiveis[did]["invoice"]
                registrar_evento(iid, f"Dedução de {fmt_moeda(v, dados['moeda'])} do desconto do invoice {origem}")
                origem_id = db.execute("SELECT invoice_id FROM descontos WHERE id = ?", (did,)).fetchone()[0]
                registrar_evento(origem_id, f"Desconto deduzido no invoice {dados['numero']}: {fmt_moeda(v, dados['moeda'])}")
            db.commit()
            flash("Invoice criado." + (f" {len(deducoes)} desconto(s) deduzido(s)." if deducoes else ""), "ok")
            avisar_cadastro_pendente(iid)
            return redirect(url_for("invoice_detalhe", id=iid))
        for e in erros:
            flash(e, "erro")
        return render_template("invoice_form.html", d=dados, itens=itens, fornecedores=fornecedores, editando=False,
                               deducoes_escolhidas=dict(deducoes))
    return render_template("invoice_form.html", d={"moeda": "USD", "condicao": [{"percentual": 100, "tipo": "PRAZO"}]},
                           itens=[],
                           fornecedores=fornecedores, editando=False, deducoes_escolhidas={})


@app.route("/invoices/<int:id>/editar", methods=["GET", "POST"])
@login_obrigatorio
def invoice_editar(id):
    if not pode("importacao"):
        abort(403)
    r = carregar_invoice(id)
    if r["cancelado_em"]:
        abort(403)
    db = get_db()
    # quem lançou pode corrigir tudo; só fornecedor e moeda ficam travados quando há desconto ou deduções
    # (que dependem deles), e os itens não podem baixar o valor abaixo do que já foi pago
    chave_travada = bool(r["desconto"] or r["deducoes_recebidas"])
    fornecedores = db.execute("SELECT * FROM fornecedores WHERE ativo = 1 ORDER BY nome").fetchall()
    if request.method == "POST":
        dados, itens, erros = ler_form_invoice()
        if chave_travada:
            dados.update(fornecedor=r["fornecedor"], moeda=r["moeda"])
        fid = obter_fornecedor(dados["fornecedor"], criar=False) if dados["fornecedor"] else None
        if fid and db.execute("SELECT 1 FROM invoices WHERE fornecedor_id = ? AND lower(numero) = lower(?) AND id <> ?",
                              (fid, dados["numero"], id)).fetchone():
            erros.append(f"O invoice {dados['numero']} deste fornecedor já está cadastrado.")
        total_itens = sum((it["quantidade"] or 0) * (it["valor_unit"] or 0) for it in itens)
        if total_itens + TOLERANCIA < r["deducoes_aplicadas"]:
            erros.append("O valor dos itens não pode ficar menor que as deduções aplicadas neste invoice.")
        elif total_itens - r["deducoes_aplicadas"] - r["segurado"] + TOLERANCIA < r["principal_pago"]:
            erros.append(f"Com esses itens, o valor a pagar ficaria menor que o já pago "
                         f"({fmt_moeda(r['principal_pago'], r['moeda'])}). Corrija primeiro o pagamento.")
        if not erros:
            mudancas = []
            if round(total_itens, 2) != r["itens_total"]:
                mudancas.append(f"valor total {fmt_moeda(r['itens_total'], r['moeda'])} → "
                                f"{fmt_moeda(total_itens, r['moeda'])}")
            novo_texto = condicao_texto({"condicao": dados["condicao"]})
            if novo_texto != condicao_texto(r):
                mudancas.append(f"condição: {condicao_texto(r)} → {novo_texto}")
            db.execute(
                "UPDATE invoices SET fornecedor_id=?, numero=?, moeda=?, data_assinatura=?, condicao_tipo=?,"
                " condicao_dias=?, agente_carga=?, despachante=?, grupo_consolidacao=?, data_prog_coleta=?, obs=?"
                " WHERE id=?",
                (obter_fornecedor(dados["fornecedor"]), dados["numero"], dados["moeda"], dados["data_assinatura"],
                 dados["condicao_tipo"], dados["condicao_dias"], dados["agente_carga"], dados["despachante"],
                 dados["grupo_consolidacao"], dados["data_prog_coleta"], dados["obs"], id))
            salvar_itens(id, itens)
            salvar_condicao(id, dados["condicao"])
            registrar_evento(id, "Dados do invoice corrigidos", "; ".join(mudancas) or None)
            db.commit()
            flash("Invoice atualizado.", "ok")
            avisar_cadastro_pendente(id)
            return redirect(url_for("invoice_detalhe", id=id))
        for e in erros:
            flash(e, "erro")
        return render_template("invoice_form.html", d=dados, itens=itens, fornecedores=fornecedores, editando=True,
                               id=id, chave_travada=chave_travada, deducoes_aplicadas=r["deducoes_aplicadas"])
    d = dict(r)
    return render_template("invoice_form.html", d=d, itens=[dict(x) for x in r["itens"]],
                           fornecedores=fornecedores, editando=True, id=id, chave_travada=chave_travada,
                           deducoes_aplicadas=r["deducoes_aplicadas"])


@app.route("/invoices/<int:id>")
@login_obrigatorio
def invoice_detalhe(id):
    r = carregar_invoice(id)
    db = get_db()
    eventos = db.execute(
        "SELECT e.*, u.nome AS usuario FROM eventos e LEFT JOIN usuarios u ON u.id = e.usuario_id"
        " WHERE e.invoice_id = ? ORDER BY e.id", (id,)).fetchall()
    revisoes = db.execute(
        "SELECT v.*, u.nome AS usuario FROM revisoes_desconto v LEFT JOIN usuarios u ON u.id = v.usuario_id"
        " WHERE v.desconto_id = ? ORDER BY v.id", (r["desconto"]["id"],)).fetchall() if r["desconto"] else []
    disponiveis = descontos_disponiveis(r["fornecedor_id"], r["moeda"], excluir_invoice_id=id)
    nomes = {u["id"]: u["nome"] for u in db.execute("SELECT id, nome FROM usuarios")}
    pasta = config("onedrive_pasta", "")
    return render_template("invoice_detalhe.html", r=r, eventos=eventos, revisoes=revisoes, disponiveis=disponiveis,
                           nomes=nomes, pasta_onedrive=pasta, hoje=hoje())


# ---------------------------------------------------------------------------
# Etapas logísticas e banco de débito
# ---------------------------------------------------------------------------
@app.route("/invoices/<int:id>/etapa/<etapa>", methods=["POST"])
@login_obrigatorio
def invoice_etapa(id, etapa):
    if etapa not in CAMPOS_ETAPA:
        abort(404)
    if not pode("importacao"):
        abort(403)
    r = carregar_invoice(id)
    if r["cancelado_em"]:
        abort(403)
    falta_cadastro = criacao_faltando(r)
    if falta_cadastro:  # a etapa 2 (Coleta) em diante só começa com o cadastro completo
        flash("Complete o cadastro do invoice antes de seguir para a logística. Falta: "
              + ", ".join(falta_cadastro) + ".", "erro")
        return redirect(url_for("invoice_detalhe", id=id) + "#logistica")
    novos, mudancas = {}, []
    for campo, rotulo, tipo, _obrig in CAMPOS_ETAPA[etapa]:
        valor = request.form.get(campo, "").strip()
        if tipo == "data":
            if valor and not data_valida(valor):
                flash(f"{rotulo}: data inválida.", "erro")
                return redirect(url_for("invoice_detalhe", id=id) + f"#etapa-{etapa}")
            valor = data_valida(valor)
        elif tipo == "canal":
            valor = valor if valor in CORES_CANAL else None
        else:
            valor = valor or None
        novos[campo] = valor
        if valor != r[campo]:
            mudancas.append(f"{rotulo}: {fmt_data(valor) if tipo == 'data' else (valor or '—')}")
    if mudancas:
        db = get_db()
        db.execute(f"UPDATE invoices SET {', '.join(c + ' = ?' for c in novos)} WHERE id = ?", (*novos.values(), id))
        registrar_evento(id, f"Etapa {TITULO_ETAPA[etapa]} atualizada", "; ".join(mudancas))
        if etapa == "CHEGADA" and novos["data_chegada"] and not r["data_chegada"]:
            # chegada de um invoice com deduções libera o valor segurado dos invoices de origem
            for x in r["deducoes_recebidas"]:
                registrar_evento(x["origem_id"], f"Invoice {r['numero']} (com dedução de "
                                 f"{fmt_moeda(x['valor'], r['moeda'])}) chegou: valor segurado pode ser liberado")
        db.commit()
    # salvar sempre funciona; a etapa só é concluída com todas as informações obrigatórias
    falta = etapa_faltando(dict(r, **novos), etapa)
    if falta:
        flash(f"Etapa {TITULO_ETAPA[etapa]} salva. Para concluir a etapa, falta: " + ", ".join(falta) + ".", "aviso-box")
    else:
        flash(f"Etapa {TITULO_ETAPA[etapa]} salva e concluída.", "ok")
    return redirect(url_for("invoice_detalhe", id=id) + f"#etapa-{etapa}")


@app.route("/invoices/<int:id>/banco", methods=["POST"])
@login_obrigatorio
def invoice_banco(id):
    if not pode("financeiro"):
        abort(403)
    carregar_invoice(id)
    banco = request.form.get("banco_debito", "").strip() or None
    db = get_db()
    db.execute("UPDATE invoices SET banco_debito = ? WHERE id = ?", (banco, id))
    registrar_evento(id, "Banco de débito informado", banco)
    db.commit()
    flash("Banco de débito salvo.", "ok")
    return redirect(url_for("invoice_detalhe", id=id) + "#etapa-EMBARQUE")


# ---------------------------------------------------------------------------
# Deduções de descontos em um invoice (Importação)
# ---------------------------------------------------------------------------
@app.route("/invoices/<int:id>/deducoes", methods=["POST"])
@login_obrigatorio
def deducao_nova(id):
    if not pode("importacao"):
        abort(403)
    r = carregar_invoice(id)
    volta = redirect(url_for("invoice_detalhe", id=id) + "#deducoes")
    did = request.form.get("desconto_id", type=int)
    valor = parse_num(request.form.get("valor"))
    disponiveis = {d["id"]: d for d in descontos_disponiveis(r["fornecedor_id"], r["moeda"], excluir_invoice_id=id)}
    erro = None
    if r["cancelado_em"]:
        erro = "Invoice cancelado."
    elif any(p["tipo"] == "PRINCIPAL" for p in r["pagamentos"]):
        erro = "Este invoice já tem pagamento registrado: não é possível incluir deduções."
    elif did not in disponiveis:
        erro = "Selecione um desconto disponível."
    elif any(x["desconto_id"] == did for x in r["deducoes_recebidas"]):
        erro = "Este desconto já foi deduzido neste invoice."
    elif valor is None or valor <= 0:
        erro = "Informe o valor a deduzir."
    elif valor > disponiveis[did]["saldo"] + TOLERANCIA:
        erro = f"O valor é maior que o saldo do desconto ({fmt_moeda(disponiveis[did]['saldo'], r['moeda'])})."
    elif valor > r["valor_invoice"] + TOLERANCIA:
        erro = "O valor é maior que o valor restante do invoice."
    if erro:
        flash(erro, "erro")
        return volta
    db = get_db()
    valor = round(valor, 2)
    db.execute("INSERT INTO deducoes (desconto_id, invoice_id, valor, criado_por, criado_em) VALUES (?, ?, ?, ?, ?)",
               (did, id, valor, g.usuario["id"], agora()))
    origem = disponiveis[did]["invoice"]
    registrar_evento(id, f"Dedução de {fmt_moeda(valor, r['moeda'])} do desconto do invoice {origem}")
    origem_id = db.execute("SELECT invoice_id FROM descontos WHERE id = ?", (did,)).fetchone()[0]
    registrar_evento(origem_id, f"Desconto deduzido no invoice {r['numero']}: {fmt_moeda(valor, r['moeda'])}")
    db.commit()
    flash("Dedução registrada.", "ok")
    return volta


@app.route("/deducoes/<int:xid>/editar", methods=["POST"])
@login_obrigatorio
def deducao_editar(xid):
    """A Importação corrige o valor deduzido enquanto o invoice não chegou nem foi pago."""
    if not pode("importacao"):
        abort(403)
    db = get_db()
    x = db.execute("SELECT x.*, d.valor_final, d.invoice_id AS origem_id FROM deducoes x"
                   " JOIN descontos d ON d.id = x.desconto_id WHERE x.id = ?", (xid,)).fetchone()
    if x is None:
        abort(404)
    destino = carregar_invoice(x["invoice_id"])
    volta = redirect(url_for("invoice_detalhe", id=destino["id"]) + "#deducoes")
    valor = parse_num(request.form.get("valor"))
    outras = db.execute("SELECT COALESCE(SUM(valor), 0) FROM deducoes WHERE desconto_id = ? AND id <> ?",
                        (x["desconto_id"], xid)).fetchone()[0]
    saldo = round(x["valor_final"] - outras, 2)
    if destino["data_chegada"] or any(p["tipo"] == "PRINCIPAL" for p in destino["pagamentos"]):
        flash("O invoice já chegou ou tem pagamento: a dedução não pode mais ser alterada.", "erro")
    elif valor is None or valor <= 0:
        flash("Informe o valor deduzido (para retirar a dedução, use Remover).", "erro")
    elif valor > saldo + TOLERANCIA:
        flash(f"O valor é maior que o saldo do desconto ({fmt_moeda(saldo, destino['moeda'])}).", "erro")
    elif valor > destino["itens_total"] - (destino["deducoes_aplicadas"] - x["valor"]) + TOLERANCIA:
        flash("O valor é maior que o valor restante do invoice.", "erro")
    else:
        valor = round(valor, 2)
        db.execute("UPDATE deducoes SET valor = ? WHERE id = ?", (valor, xid))
        origem = db.execute("SELECT numero FROM invoices WHERE id = ?", (x["origem_id"],)).fetchone()[0]
        texto = f"{fmt_moeda(x['valor'], destino['moeda'])} → {fmt_moeda(valor, destino['moeda'])}"
        registrar_evento(destino["id"], f"Dedução do desconto do invoice {origem} corrigida", texto)
        registrar_evento(x["origem_id"], f"Dedução no invoice {destino['numero']} corrigida", texto)
        db.commit()
        flash("Dedução corrigida.", "ok")
    return volta


@app.route("/deducoes/<int:xid>/remover", methods=["POST"])
@login_obrigatorio
def deducao_remover(xid):
    if not pode("importacao"):
        abort(403)
    db = get_db()
    x = db.execute("SELECT x.*, d.invoice_id AS origem_id FROM deducoes x JOIN descontos d ON d.id = x.desconto_id"
                   " WHERE x.id = ?", (xid,)).fetchone()
    if x is None:
        abort(404)
    destino = carregar_invoice(x["invoice_id"])
    volta = redirect(url_for("invoice_detalhe", id=destino["id"]) + "#deducoes")
    if destino["data_chegada"]:
        flash("O invoice já chegou: a dedução não pode mais ser removida.", "erro")
    elif any(p["tipo"] == "PRINCIPAL" for p in destino["pagamentos"]):
        flash("O invoice já tem pagamento registrado: a dedução não pode ser removida.", "erro")
    else:
        db.execute("DELETE FROM deducoes WHERE id = ?", (xid,))
        origem = db.execute("SELECT numero FROM invoices WHERE id = ?", (x["origem_id"],)).fetchone()[0]
        registrar_evento(destino["id"], f"Dedução removida: {fmt_moeda(x['valor'], destino['moeda'])} do invoice {origem}")
        registrar_evento(x["origem_id"], f"Dedução no invoice {destino['numero']} removida")
        db.commit()
        flash("Dedução removida.", "ok")
    return volta


# ---------------------------------------------------------------------------
# Inspeção e desconto (Engenharia de Processos - administrador)
# ---------------------------------------------------------------------------
def segurado_possivel(r, valor_final):
    """Só é possível segurar o que ainda não foi pago do invoice."""
    return round(min(valor_final, max(0, r["valor_invoice"] - r["principal_pago"])), 2)


@app.route("/invoices/<int:id>/inspecao", methods=["POST"])
@somente_admin
def invoice_inspecao(id):
    r = carregar_invoice(id)
    volta = redirect(url_for("invoice_detalhe", id=id) + "#inspecao")
    f = request.form
    data_insp = data_valida(f.get("data_fim_inspecao"))
    tem = f.get("tem_desconto")
    d = r["desconto"]
    if r["cancelado_em"]:
        abort(403)
    if not r["data_chegada"]:
        flash("A inspeção é registrada depois da chegada do invoice na fábrica.", "erro")
        return volta
    if not data_insp or tem not in ("0", "1"):
        flash("Informe a data do fim da inspeção e se há desconto.", "erro")
        return volta
    db = get_db()
    if tem == "0":
        if d:
            if r["deducoes_do_desconto"] or r["liberado"] or db.execute(
                    "SELECT 1 FROM revisoes_desconto WHERE desconto_id = ?", (d["id"],)).fetchone():
                flash("O desconto já tem deduções, liberações ou revalidações: para zerar, use a revalidação.", "erro")
                return volta
            db.execute("DELETE FROM descontos WHERE id = ?", (d["id"],))
            registrar_evento(id, "Desconto removido")
        db.execute("UPDATE invoices SET data_fim_inspecao = ?, tem_desconto = 0 WHERE id = ?", (data_insp, id))
        registrar_evento(id, "Inspeção registrada: sem desconto", f"Fim da inspeção: {fmt_data(data_insp)}")
        db.commit()
        flash("Inspeção registrada sem desconto. O invoice segue para pagamento integral.", "ok")
        return volta
    data_rel = data_valida(f.get("data_relatorio"))
    status = f.get("status") if f.get("status") in STATUS_DESCONTO else "ENVIADO"
    obs = f.get("obs", "").strip() or None
    if not data_rel:
        flash("Informe a data do relatório.", "erro")
        return volta
    if d:  # desconto já existe: valor só muda pela revalidação
        db.execute("UPDATE descontos SET data_relatorio = ?, obs = ? WHERE id = ?", (data_rel, obs, d["id"]))
        db.execute("UPDATE invoices SET data_fim_inspecao = ?, tem_desconto = 1 WHERE id = ?", (data_insp, id))
        if status != d["status"]:
            alterar_status_desconto(r, d, status)
        registrar_evento(id, "Inspeção/desconto atualizados")
        db.commit()
        flash("Inspeção atualizada.", "ok")
        return volta
    valor = parse_num(f.get("valor"))
    if valor is None or valor <= 0:
        flash("Informe o valor do desconto.", "erro")
        return volta
    valor = round(valor, 2)
    if valor > r["valor_invoice"] + TOLERANCIA:
        flash("O desconto não pode ser maior que o valor do invoice.", "erro")
        return volta
    segurado = segurado_possivel(r, valor)
    db.execute("INSERT INTO descontos (invoice_id, valor_inicial, valor_final, valor_segurado, data_relatorio, status,"
               " aprovado_em, aprovado_por, obs, criado_por, criado_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (id, valor, valor, segurado, data_rel, status, agora() if status == "APROVADO" else None,
                g.usuario["id"] if status == "APROVADO" else None, obs, g.usuario["id"], agora()))
    db.execute("UPDATE invoices SET data_fim_inspecao = ?, tem_desconto = 1 WHERE id = ?", (data_insp, id))
    detalhe = f"Valor: {fmt_moeda(valor, r['moeda'])} · segurado: {fmt_moeda(segurado, r['moeda'])}"
    if segurado + TOLERANCIA < valor:
        detalhe += " (o invoice já estava pago em parte: o restante será apenas deduzido no próximo invoice)"
    registrar_evento(id, f"Inspeção registrada com desconto · {STATUS_DESCONTO[status]}", detalhe)
    db.commit()
    flash("Desconto cadastrado. " + (f"Valor segurado: {fmt_moeda(segurado, r['moeda'])}." if segurado > TOLERANCIA
                                     else "O invoice já foi pago: o desconto será apenas deduzido no próximo invoice."),
          "ok")
    return volta


def alterar_status_desconto(r, d, status):
    get_db().execute("UPDATE descontos SET status = ?, aprovado_em = ?, aprovado_por = ? WHERE id = ?",
                     (status, agora() if status == "APROVADO" else None,
                      g.usuario["id"] if status == "APROVADO" else None, d["id"]))
    registrar_evento(r["id"], f"Desconto: {STATUS_DESCONTO[status]}")


@app.route("/descontos/<int:did>/status", methods=["POST"])
@somente_admin
def desconto_status(did):
    d = get_db().execute("SELECT * FROM descontos WHERE id = ?", (did,)).fetchone()
    if d is None:
        abort(404)
    r = carregar_invoice(d["invoice_id"])
    status = request.form.get("status")
    if status not in STATUS_DESCONTO:
        abort(400)
    if status == "ENVIADO" and r["deducoes_do_desconto"]:
        flash("O desconto já foi deduzido em outro invoice: não pode voltar para 'enviado'.", "erro")
    elif status != d["status"]:
        alterar_status_desconto(r, d, status)
        get_db().commit()
        flash(f"Desconto: {STATUS_DESCONTO[status]}.", "ok")
    return redirect(request.form.get("voltar") or url_for("invoice_detalhe", id=r["id"]) + "#inspecao")


@app.route("/descontos/<int:did>/revalidar", methods=["POST"])
@somente_admin
def desconto_revalidar(did):
    db = get_db()
    d = db.execute("SELECT * FROM descontos WHERE id = ?", (did,)).fetchone()
    if d is None:
        abort(404)
    r = carregar_invoice(d["invoice_id"])
    volta = redirect(url_for("invoice_detalhe", id=r["id"]) + "#inspecao")
    novo = parse_num(request.form.get("valor_final"))
    novo = round(novo, 2) if novo is not None else None
    motivo = request.form.get("motivo", "").strip()
    erro = None
    if novo is None or novo < 0:
        erro = "Informe o valor final do desconto (use 0 para zerar)."
    elif not motivo:
        erro = "Informe o motivo da revalidação."
    elif abs(novo - d["valor_final"]) <= TOLERANCIA:
        erro = "O valor final informado é igual ao atual."
    elif novo < r["deduzido"] - TOLERANCIA:
        erro = (f"O valor final não pode ser menor que o já deduzido em outros invoices "
                f"({fmt_moeda(r['deduzido'], r['moeda'])}).")
    elif novo > r["valor_invoice"] + TOLERANCIA:
        erro = "O desconto não pode ser maior que o valor do invoice."
    if erro:
        flash(erro, "erro")
        return volta
    # enquanto o invoice não foi totalmente pago, o valor segurado acompanha o valor final
    segurado = d["valor_segurado"]
    if r["principal_pago"] < r["principal_devido"] - TOLERANCIA or r["principal_pago"] <= TOLERANCIA:
        segurado = max(segurado_possivel(r, novo), r["liberado"])
    db.execute("INSERT INTO revisoes_desconto (desconto_id, valor_anterior, valor_novo, motivo, usuario_id, criado_em)"
               " VALUES (?, ?, ?, ?, ?, ?)", (did, d["valor_final"], novo, motivo, g.usuario["id"], agora()))
    db.execute("UPDATE descontos SET valor_final = ?, valor_segurado = ? WHERE id = ?", (novo, segurado, did))
    obs = motivo
    if abs(segurado - d["valor_segurado"]) > TOLERANCIA:
        obs += f" · segurado: {fmt_moeda(d['valor_segurado'], r['moeda'])} → {fmt_moeda(segurado, r['moeda'])}"
    registrar_evento(r["id"], f"Desconto revalidado: {fmt_moeda(d['valor_final'], r['moeda'])} → "
                     f"{fmt_moeda(novo, r['moeda'])}" + (" (zerado)" if novo == 0 else ""), obs)
    db.commit()
    msg = "Desconto revalidado."
    if novo > segurado + TOLERANCIA and novo > d["valor_final"]:
        msg += " O invoice já foi pago: o valor acima do segurado será apenas deduzido no próximo invoice."
    flash(msg, "ok")
    return volta


# ---------------------------------------------------------------------------
# Pagamentos (Financeiro)
# ---------------------------------------------------------------------------
@app.route("/invoices/<int:id>/pagamentos", methods=["POST"])
@login_obrigatorio
def pagamento_novo(id):
    if not pode("financeiro"):
        abort(403)
    r = carregar_invoice(id)
    volta = redirect(url_for("invoice_detalhe", id=id) + "#pagamentos")
    f = request.form
    tipo = f.get("tipo")
    valor = parse_num(f.get("valor"))
    data_pg = data_valida(f.get("data_pagamento"))
    swift = f.get("swift_url", "").strip() or None
    banco = f.get("banco", "").strip() or r["banco_debito"]
    limite = {"PRINCIPAL": r["principal_pendente"], "LIBERACAO": r["liberacao_disponivel"]}.get(tipo)
    erro = None
    if r["cancelado_em"]:
        erro = "Invoice cancelado."
    elif limite is None:
        erro = "Selecione o tipo de pagamento."
    elif not data_pg:
        erro = "Informe a data do pagamento."
    elif valor is None or valor <= 0:
        erro = "Informe o valor pago."
    elif valor > limite + TOLERANCIA:
        erro = (f"O valor é maior que o disponível para {TIPOS_PAGAMENTO[tipo].lower()} "
                f"({fmt_moeda(limite, r['moeda'])}).")
    elif swift and not link_valido(swift):
        erro = "O link do Swift deve começar com https://."
    if erro:
        flash(erro, "erro")
        return volta
    db = get_db()
    valor = round(valor, 2)
    db.execute("INSERT INTO pagamentos (invoice_id, tipo, valor, data_pagamento, banco, swift_url, obs, registrado_por,"
               " registrado_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (id, tipo, valor, data_pg, banco, swift, f.get("obs", "").strip() or None, g.usuario["id"], agora()))
    registrar_evento(id, f"{TIPOS_PAGAMENTO[tipo]}: {fmt_moeda(valor, r['moeda'])} em {fmt_data(data_pg)}",
                     ("Swift anexado" if swift else "Swift pendente") + (f" · banco: {banco}" if banco else ""))
    db.commit()
    flash("Pagamento registrado." + ("" if swift else " O Swift ficou pendente."), "ok")
    return volta


@app.route("/pagamentos/<int:pid>/swift", methods=["POST"])
@login_obrigatorio
def pagamento_swift(pid):
    if not pode("financeiro"):
        abort(403)
    db = get_db()
    p = db.execute("SELECT * FROM pagamentos WHERE id = ?", (pid,)).fetchone()
    if p is None:
        abort(404)
    url = request.form.get("swift_url", "").strip()
    if not link_valido(url):
        flash("Cole o link do Swift no OneDrive (começando com https://).", "erro")
    else:
        db.execute("UPDATE pagamentos SET swift_url = ? WHERE id = ?", (url, pid))
        registrar_evento(p["invoice_id"], f"Swift anexado ao pagamento de {fmt_data(p['data_pagamento'])}")
        db.commit()
        flash("Swift anexado.", "ok")
    return redirect(url_for("invoice_detalhe", id=p["invoice_id"]) + "#pagamentos")


@app.route("/pagamentos/<int:pid>/editar", methods=["POST"])
@login_obrigatorio
def pagamento_editar(pid):
    """O Financeiro corrige um pagamento lançado (data, valor, banco, Swift ou observação)."""
    if not pode("financeiro"):
        abort(403)
    db = get_db()
    p = db.execute("SELECT * FROM pagamentos WHERE id = ?", (pid,)).fetchone()
    if p is None:
        abort(404)
    r = carregar_invoice(p["invoice_id"])
    volta = redirect(url_for("invoice_detalhe", id=r["id"]) + "#pagamentos")
    f = request.form
    valor = parse_num(f.get("valor"))
    data_pg = data_valida(f.get("data_pagamento"))
    swift = f.get("swift_url", "").strip() or None
    banco = f.get("banco", "").strip() or None
    obs = f.get("obs", "").strip() or None
    # limite: o que ainda pode ser pago desse tipo, devolvendo o valor atual deste pagamento
    if p["tipo"] == "PRINCIPAL":
        limite = r["principal_pendente"] + p["valor"]
    else:
        limite = r["liberacao_disponivel"] + p["valor"]
    erro = None
    if not data_pg:
        erro = "Informe a data do pagamento."
    elif valor is None or valor <= 0:
        erro = "Informe o valor pago."
    elif valor > limite + TOLERANCIA:
        erro = f"O valor é maior que o permitido para este pagamento ({fmt_moeda(limite, r['moeda'])})."
    elif swift and not link_valido(swift):
        erro = "O link do Swift deve começar com https://."
    if erro:
        flash(erro, "erro")
        return volta
    valor = round(valor, 2)
    mudancas = []
    if data_pg != p["data_pagamento"]:
        mudancas.append(f"data {fmt_data(p['data_pagamento'])} → {fmt_data(data_pg)}")
    if abs(valor - p["valor"]) > TOLERANCIA:
        mudancas.append(f"valor {fmt_moeda(p['valor'], r['moeda'])} → {fmt_moeda(valor, r['moeda'])}")
    if banco != p["banco"]:
        mudancas.append(f"banco {p['banco'] or '—'} → {banco or '—'}")
    if swift != p["swift_url"]:
        mudancas.append("Swift " + ("corrigido" if p["swift_url"] and swift else "anexado" if swift else "removido"))
    if obs != p["obs"]:
        mudancas.append("observação")
    if mudancas:
        db.execute("UPDATE pagamentos SET data_pagamento = ?, valor = ?, banco = ?, swift_url = ?, obs = ? WHERE id = ?",
                   (data_pg, valor, banco, swift, obs, pid))
        registrar_evento(r["id"], f"Pagamento corrigido ({TIPOS_PAGAMENTO[p['tipo']]})", "; ".join(mudancas))
        db.commit()
        flash("Pagamento corrigido.", "ok")
    return volta


@app.route("/pagamentos/<int:pid>/excluir", methods=["POST"])
@login_obrigatorio
def pagamento_excluir(pid):
    if not pode("financeiro"):
        abort(403)
    db = get_db()
    p = db.execute("SELECT * FROM pagamentos WHERE id = ?", (pid,)).fetchone()
    if p is None:
        abort(404)
    motivo = request.form.get("motivo", "").strip()
    r = carregar_invoice(p["invoice_id"])
    if not motivo:
        flash("Informe o motivo da exclusão.", "erro")
    elif p["tipo"] == "PRINCIPAL" and r["liberado"] > TOLERANCIA:
        flash("Exclua antes as liberações do valor segurado deste invoice.", "erro")
    else:
        db.execute("DELETE FROM pagamentos WHERE id = ?", (pid,))
        registrar_evento(p["invoice_id"], f"Pagamento excluído: {TIPOS_PAGAMENTO[p['tipo']]} de "
                         f"{fmt_moeda(p['valor'], r['moeda'])} em {fmt_data(p['data_pagamento'])}", motivo)
        db.commit()
        flash("Pagamento excluído.", "ok")
    return redirect(url_for("invoice_detalhe", id=p["invoice_id"]) + "#pagamentos")


@app.route("/invoices/<int:id>/cancelar", methods=["POST"])
@somente_admin
def invoice_cancelar(id):
    r = carregar_invoice(id)
    motivo = request.form.get("motivo", "").strip()
    if r["cancelado_em"]:
        flash("Este invoice já está cancelado.", "erro")
    elif r["pagamentos"] or r["deducoes_recebidas"] or r["desconto"]:
        flash("O invoice tem pagamentos, deduções ou desconto: remova-os antes de cancelar.", "erro")
    elif not motivo:
        flash("Informe o motivo do cancelamento.", "erro")
    else:
        db = get_db()
        db.execute("UPDATE invoices SET cancelado_em = ?, cancelado_por = ?, motivo_cancelamento = ? WHERE id = ?",
                   (agora(), g.usuario["id"], motivo, id))
        registrar_evento(id, "Invoice cancelado", motivo)
        db.commit()
        flash("Invoice cancelado.", "ok")
    return redirect(url_for("invoice_detalhe", id=id))


# ---------------------------------------------------------------------------
# Lista de descontos
# ---------------------------------------------------------------------------
@app.route("/descontos")
@login_obrigatorio
def descontos():
    ids = [x[0] for x in get_db().execute(
        "SELECT d.invoice_id FROM descontos d JOIN invoices i ON i.id = d.invoice_id ORDER BY d.id DESC")]
    lista = [carregar_invoice(i) for i in ids]
    filtro = request.args.get("f", "abertos")
    if filtro == "abertos":
        lista = [r for r in lista if r["situacao"] not in ("CONCLUIDO", "CANCELADO")]
    elif filtro in STATUS_DESCONTO:
        lista = [r for r in lista if r["desconto"]["status"] == filtro]
    return render_template("descontos.html", lista=lista, filtro=filtro)


# ---------------------------------------------------------------------------
# Fornecedores
# ---------------------------------------------------------------------------
@app.route("/fornecedores", methods=["GET", "POST"])
@login_obrigatorio
def fornecedores():
    db = get_db()
    pode_editar = pode("importacao")
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
        "SELECT f.*, (SELECT COUNT(*) FROM invoices i WHERE i.fornecedor_id = f.id) AS qtd"
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
    return render_template("sistema.html", usa_git=usa_git, commit=commit, backups=backups,
                           pasta_onedrive=config("onedrive_pasta", ""))


@app.route("/sistema/onedrive", methods=["POST"])
@somente_admin
def sistema_onedrive():
    url = request.form.get("onedrive_pasta", "").strip()
    if url and not link_valido(url):
        flash("O link da pasta deve começar com https://.", "erro")
    else:
        salvar_config("onedrive_pasta", url)
        get_db().commit()
        flash("Pasta do OneDrive salva.", "ok")
    return redirect(url_for("sistema"))


# ---------------------------------------------------------------------------
# E-mails automáticos
#
# A cada alteração salva no sistema, a "fotografia" de previsibilidade de cada invoice (parcelas,
# janela de nacionalização, liberações, desconto, chegada...) é comparada com a anterior; as
# diferenças viram e-mails, na fila da tabela `emails`. O envio acontece logo após a resposta da
# página (o usuário não espera) pela conta Gmail configurada na aba E-mails.
# ---------------------------------------------------------------------------
FLUXOS_EMAIL = {
    # chave: (título, quando é enviado, setor sugerido em "Para", setor sugerido em "Cc")
    "pagamento_programado": (
        "Pagamento programado",
        "Parcela antecipada (data de assinatura + 5 dias úteis) e parcelas após a HAWB, assim que a data de "
        "vencimento e o valor ficam definidos.", "financeiro", ""),
    "pagamento_alterado": (
        "Pagamento alterado",
        "Mudança na data de vencimento ou no valor de uma parcela já programada (ex.: HAWB corrigida, itens, "
        "dedução, desconto) e cancelamento de invoice.", "financeiro", ""),
    "nacionalizacao": (
        "Impostos de nacionalização",
        "Janela de pagamento: ETA planejado −1 a +3 dias úteis; com o ETA real, ETA real a +2 dias úteis. "
        "Avisa no cadastro e em cada alteração.", "financeiro", ""),
    "banco_debito": (
        "Informar banco de débito",
        "Pedido para o Financeiro informar o banco de débito, quando o ETA planejado é cadastrado.",
        "financeiro", ""),
    "desconto_segurar": (
        "Desconto cadastrado: segurar valor",
        "Quando um desconto é cadastrado: o valor deve ser segurado no pagamento do invoice. A dedução em "
        "outro invoice só acontece após a aprovação do fornecedor.", "financeiro", "importacao"),
    "liberacao": (
        "Valor segurado liberado",
        "Quando o invoice com a dedução chega à fábrica (ou o desconto é revalidado para baixo): realizar o "
        "pagamento do valor segurado até a chegada + 5 dias úteis.", "financeiro", ""),
    "chegada_deducao": (
        "Chegada de invoice com dedução",
        "Aviso à Engenharia de Processos quando chega à fábrica um invoice que recebeu dedução de desconto de "
        "outro invoice.", "admin", ""),
    "resumo_diario": (
        "Resumo diário de pendências do Financeiro",
        "Em dias úteis, a partir do horário configurado, se o Financeiro tiver pendências.", "financeiro", ""),
}
STATUS_EMAIL = {"PENDENTE": "Na fila", "ENVIADO": "Enviado", "ERRO": "Erro", "NAO_ENVIADO": "Não enviado"}
SMTP_PADRAO = {"smtp_host": "smtp.gmail.com", "smtp_porta": "587",
               "smtp_nome": "Controle de Invoices · Quinelato"}
RE_EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
FUSO_BRASIL = timedelta(hours=-3)  # horário de Brasília (sem horário de verão)


def agora_brasil():
    return datetime.now(timezone.utc).replace(tzinfo=None) + FUSO_BRASIL


def lista_emails(texto):
    return [e.strip().lower() for e in re.split(r"[,;\s]+", texto or "") if e.strip()]


def emails_do_setor(perfil):
    if not perfil:
        return ""
    return ", ".join(u["email"] for u in get_db().execute(
        "SELECT email FROM usuarios WHERE ativo = 1 AND perfil = ? ORDER BY nome", (perfil,)))


def config_fluxos():
    """Configuração salva de cada fluxo: {fluxo: {ativo, para, cc}} (vazio se nunca foi salva)."""
    try:
        return json.loads(config("email_fluxos", "{}"))
    except ValueError:
        return {}


def fluxos_para_tela():
    salvos = config_fluxos()
    linhas = []
    for chave, (titulo, quando, para, cc) in FLUXOS_EMAIL.items():
        c = salvos.get(chave)
        linhas.append({"chave": chave, "titulo": titulo, "quando": quando,
                       "sugestao_para": SETORES.get(para, ""), "sugestao_cc": SETORES.get(cc, ""),
                       "ativo": c["ativo"] if c else True,
                       "para": c["para"] if c else emails_do_setor(para),
                       "cc": c["cc"] if c else emails_do_setor(cc), "salvo": bool(c)})
    return linhas


def conta_configurada():
    return bool(config("smtp_usuario") and config("smtp_senha"))


# ---------- conteúdo dos e-mails ----------
def corpo_email(titulo, r, linhas, obs=None):
    """HTML simples (sem links para o sistema), com os dados do invoice e a lista de informações."""
    cab = ""
    if r is not None:
        dados = [("Fornecedor", r["fornecedor"]), ("Invoice", r["numero"]), ("Moeda", r["moeda"]),
                 ("Valor do invoice", fmt_moeda(r["valor_invoice"], r["moeda"])),
                 ("Condição de pagamento", condicao_texto(r) if r["condicao"] else "não informada")]
        cab = "".join(f'<tr><td style="color:#5b6170;padding:2px 12px 2px 0">{escape(k)}</td>'
                      f'<td style="padding:2px 0"><strong>{escape(v)}</strong></td></tr>' for k, v in dados)
        cab = f'<table style="border-collapse:collapse;margin:8px 0 14px">{cab}</table>'
    itens = "".join(f'<li style="margin:4px 0">{escape(x)}</li>' for x in linhas)
    obs_html = f'<p style="margin:12px 0 0">{escape(obs)}</p>' if obs else ""
    return (f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#212529;max-width:680px">'
            f'<div style="border-bottom:3px solid #327fef;padding-bottom:6px;margin-bottom:10px">'
            f'<strong style="color:#0a2a7b;font-size:16px">{escape(titulo)}</strong></div>{cab}'
            f'<ul style="padding-left:20px;margin:0">{itens}</ul>{obs_html}'
            f'<p style="color:#8a8f9c;font-size:12px;margin-top:22px">E-mail automático do sistema Controle de '
            f'Invoices e Descontos · Quinelato. Não é necessário responder.</p></div>')


def texto_simples(html):
    txt = re.sub(r"</(li|tr|p|div)>", "\n", html)
    txt = re.sub(r"<li[^>]*>", "• ", txt)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"[ \t]+", " ", txt)
    import html as _html
    return "\n".join(l.strip() for l in _html.unescape(txt).splitlines() if l.strip())


def enfileirar_email(fluxo, r, assunto_extra, linhas, obs=None):
    """Grava o e-mail na fila (ou como não enviado, explicando o motivo)."""
    titulo = FLUXOS_EMAIL[fluxo][0]
    assunto = f"[Invoices] {titulo}" + (f" · {r['fornecedor']} {r['numero']}" if r is not None else "") + \
              (f" · {assunto_extra}" if assunto_extra else "")
    corpo = corpo_email(titulo + (f" · {assunto_extra}" if assunto_extra else ""), r, linhas, obs)
    c = config_fluxos().get(fluxo)
    para, cc = (c["para"], c["cc"]) if c else ("", "")
    if not c:
        status, erro = "NAO_ENVIADO", "Fluxo ainda não configurado (salve a aba E-mails)."
    elif not c.get("ativo"):
        status, erro = "NAO_ENVIADO", "Fluxo desativado."
    elif not lista_emails(para):
        status, erro = "NAO_ENVIADO", "Sem destinatário em 'Para'."
    elif not conta_configurada():
        status, erro = "NAO_ENVIADO", "Conta de envio (Gmail) não configurada."
    else:
        status, erro = "PENDENTE", None
        g.ha_emails_na_fila = True
    get_db().execute("INSERT INTO emails (fluxo, invoice_id, assunto, corpo, para, cc, status, erro, criado_em)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (fluxo, r["id"] if r is not None else None, assunto, corpo, para, cc, status, erro, agora()))


# ---------- detecção de mudanças ----------
def estado_previsao(r):
    """Fotografia do que interessa às notificações de um invoice."""
    if r["cancelado_em"]:
        return {"cancelado": True}
    return {
        "cancelado": False,
        "parcelas": {str(p["idx"]): {"texto": p["texto"], "venc": p["vencimento"], "valor": p["devido"],
                                     "quitada": p["pendente"] <= TOLERANCIA} for p in r["parcelas"]},
        "nac": r["nacionalizacao"],
        "eta_planejado": r["eta_planejado"],
        "banco": bool(r["banco_debito"]),
        "chegada": r["data_chegada"],
        "desconto": r["desconto"]["id"] if r["desconto"] else None,
        "lib": {p["texto"]: {"valor": p["valor"], "venc": p["vencimento"]}
                for p in r["parcelas_liberacao"] if p["status"] == "DISPONIVEL"},
    }


def descrever_parcela(r, p):
    if p["tipo"] == "ANTECIPADO":
        regra = f"data de assinatura {fmt_data(r['data_assinatura'])} + {PRAZO_ANTECIPADO_DIAS} dias úteis"
    else:
        regra = f"HAWB {fmt_data(r['data_hawb'])} + {p['dias']} dias"
    return f"Parcela {p['texto']} ({regra})"


def gerar_emails(r, antigo, novo):
    m = r["moeda"]
    if novo.get("cancelado"):
        if not antigo.get("cancelado"):
            prog = [p for p in (antigo.get("parcelas") or {}).values() if p["venc"] and not p["quitada"]]
            if prog:
                enfileirar_email("pagamento_alterado", r, "invoice cancelado",
                                 [f"Parcela {p['texto']}: vencimento {fmt_data(p['venc'])}, "
                                  f"{fmt_moeda(p['valor'], m)} — desconsiderar" for p in prog],
                                 "O invoice foi cancelado: os pagamentos programados acima não devem ser feitos.")
        return
    parcelas = {str(p["idx"]): p for p in r["parcelas"]}
    antigas = antigo.get("parcelas") or {}
    novo_desconto = novo["desconto"] and novo["desconto"] != antigo.get("desconto")

    # parcelas: programação e alterações
    programadas, alteradas = [], []
    for k, n in novo["parcelas"].items():
        p, a = parcelas[k], antigas.get(k)
        if n["quitada"]:
            continue
        definida = n["venc"] and n["valor"] > TOLERANCIA
        a_definida = a and a["venc"] and a["valor"] > TOLERANCIA
        if definida and not a_definida:
            programadas.append(f"{descrever_parcela(r, p)}: vencimento {fmt_data(n['venc'])} · "
                               f"valor a pagar {fmt_moeda(n['valor'], m)}")
        elif a_definida and not definida:
            alteradas.append(f"Parcela {n['texto']}: vencimento {fmt_data(a['venc'])} e valor "
                             f"{fmt_moeda(a['valor'], m)} deixaram de valer (aguardando nova definição)")
        elif definida and (a["venc"] != n["venc"] or abs(a["valor"] - n["valor"]) > TOLERANCIA
                           or a["texto"] != n["texto"]):
            if novo_desconto and a["venc"] == n["venc"] and a["texto"] == n["texto"]:
                continue  # redução pelo valor segurado: já informada no e-mail do desconto
            mud = []
            if a["venc"] != n["venc"]:
                mud.append(f"vencimento {fmt_data(a['venc'])} → {fmt_data(n['venc'])}")
            if abs(a["valor"] - n["valor"]) > TOLERANCIA:
                mud.append(f"valor {fmt_moeda(a['valor'], m)} → {fmt_moeda(n['valor'], m)}")
            if a["texto"] != n["texto"]:
                mud.append(f"condição {a['texto']} → {n['texto']}")
            alteradas.append(f"{descrever_parcela(r, p)}: " + "; ".join(mud))
    for k, a in antigas.items():
        if k not in novo["parcelas"] and a["venc"] and not a["quitada"]:
            alteradas.append(f"Parcela {a['texto']} (vencimento {fmt_data(a['venc'])}, "
                             f"{fmt_moeda(a['valor'], m)}) foi removida da condição de pagamento")
    if programadas:
        enfileirar_email("pagamento_programado", r, None, programadas,
                         "Programar o pagamento conforme acima.")
    if alteradas:
        enfileirar_email("pagamento_alterado", r, None, alteradas,
                         "A programação de pagamento deste invoice mudou. Atualize a programação.")

    # impostos de nacionalização
    jan, jan_a = novo["nac"], antigo.get("nac")
    if jan and jan != jan_a:
        valor = fmt_moeda(round(r["itens_total"] * CUSTO_IMPORTACAO_ESTIMADO, 2), m)
        regra = ("ETA planejado −1 a +3 dias úteis" if jan["base"] == "planejado" else "ETA real a +2 dias úteis")
        linhas = [f"Pagamento dos impostos de nacionalização entre {fmt_data(jan['ini'])} e {fmt_data(jan['fim'])}",
                  f"Base: ETA {jan['base']} {fmt_data(jan['eta'])} ({regra})",
                  f"Valor estimado: {valor} (40% do valor dos itens; o valor real vem do despachante)"]
        if jan_a:
            linhas.insert(1, f"Janela anterior: {fmt_data(jan_a['ini'])} a {fmt_data(jan_a['fim'])} "
                             f"(ETA {jan_a['base']} {fmt_data(jan_a['eta'])})")
        extra = ("ETA real cadastrado" if jan["base"] == "real" and (not jan_a or jan_a["base"] != "real")
                 else "data alterada" if jan_a else None)
        enfileirar_email("nacionalizacao", r, extra, linhas)

    # banco de débito
    if novo["eta_planejado"] and not antigo.get("eta_planejado") and not novo["banco"]:
        enfileirar_email("banco_debito", r, None,
                         [f"ETA planejado cadastrado: {fmt_data(novo['eta_planejado'])}",
                          "Informe no sistema o banco de débito deste invoice (seção Logística → etapa Embarque)."],
                         "Sem o banco de débito, a etapa de Embarque não é concluída.")

    # chegada de invoice que recebeu dedução
    if novo["chegada"] and not antigo.get("chegada") and r["deducoes_recebidas"]:
        enfileirar_email("chegada_deducao", r, None,
                         [f"Chegada real na fábrica: {fmt_data(novo['chegada'])}"] +
                         [f"Dedução de {fmt_moeda(x['valor'], m)} do desconto do invoice {x['invoice_origem']}"
                          for x in r["deducoes_recebidas"]],
                         "O valor segurado dos invoices de origem fica disponível para liberação pelo Financeiro.")

    # desconto cadastrado: segurar
    if novo_desconto and r["segurado"] > TOLERANCIA:
        d = r["desconto"]
        linhas = [f"Valor do desconto: {fmt_moeda(d['valor_final'], m)} ({STATUS_DESCONTO[d['status']]})",
                  f"Valor a segurar no pagamento deste invoice: {fmt_moeda(r['segurado'], m)}"]
        linhas += [f"Parcela {p['texto']}: pagar {fmt_moeda(p['devido'], m)}"
                   + (f" (vencimento {fmt_data(p['vencimento'])})" if p["vencimento"] else " (vencimento a definir)")
                   for p in r["parcelas"] if p["pendente"] > TOLERANCIA]
        enfileirar_email("desconto_segurar", r, None, linhas,
                         "Segure este valor até a liberação. A Importação só poderá deduzi-lo em outro invoice "
                         "depois que o fornecedor aprovar o desconto.")

    # liberações do valor segurado
    antigas_lib = antigo.get("lib") or {}
    novas = [(k, v) for k, v in novo["lib"].items()
             if k not in antigas_lib or abs(antigas_lib[k]["valor"] - v["valor"]) > TOLERANCIA
             or antigas_lib[k]["venc"] != v["venc"]]
    if novas:
        enfileirar_email("liberacao", r, None,
                         [f"{k}: liberar {fmt_moeda(v['valor'], m)} · pagar até {fmt_data(v['venc'])}"
                          for k, v in novas],
                         f"Realize o pagamento do valor segurado (chegada + {PRAZO_LIBERACAO_DIAS_UTEIS} dias "
                         f"úteis) e registre o pagamento com o Swift no sistema.")


def processar_notificacoes():
    """Compara a previsibilidade atual de cada invoice com a última registrada e gera os e-mails.
    Na primeira execução apenas registra a situação (não envia nada sobre o histórico)."""
    db = get_db()
    inicial = config("notif_inicio") is None
    estados = {row["invoice_id"]: row["estado"] for row in db.execute("SELECT * FROM notificacoes_estado")}
    for r in todos_invoices(incluir_cancelados=True):
        novo = json.dumps(estado_previsao(r), sort_keys=True)
        antigo = estados.get(r["id"])
        if antigo == novo:
            continue
        if not inicial:
            gerar_emails(r, json.loads(antigo) if antigo else {}, json.loads(novo))
        db.execute("INSERT INTO notificacoes_estado (invoice_id, estado, atualizado_em) VALUES (?, ?, ?)"
                   " ON CONFLICT(invoice_id) DO UPDATE SET estado = excluded.estado,"
                   " atualizado_em = excluded.atualizado_em", (r["id"], novo, agora()))
    if inicial:
        salvar_config("notif_inicio", agora())
    db.commit()


# ---------- resumo diário ----------
def resumo_diario(forcar=False):
    """Envia (uma vez por dia útil) as pendências do Financeiro. Retorna uma mensagem do que aconteceu."""
    agora_br = agora_brasil()
    dia = agora_br.date().isoformat()
    if not forcar:
        if agora_br.weekday() >= 5:
            return "Fim de semana: o resumo só é enviado em dias úteis."
        if agora_br.hour < int(config("resumo_hora", "8")):
            return "Ainda não chegou o horário do resumo."
        if config("resumo_ultimo") == dia:
            return "O resumo de hoje já foi processado."
    salvar_config("resumo_ultimo", dia)
    get_db().commit()
    pend = [p for p in listar_pendencias(todos_invoices(), perfil="financeiro") if p["quem"] == "financeiro"]
    if not pend:
        return "O Financeiro não tem pendências: nenhum e-mail enviado."
    linhas = []
    for p in pend:
        txt = f"{p['r']['fornecedor']} · {p['r']['numero']}: {p['acao']}"
        if p["data"]:
            txt += f" · {fmt_data(p['data'])}"
        if p["valor"] is not None:
            txt += f" · {fmt_moeda(p['valor'], p['r']['moeda'])}"
        if p["alerta"]:
            txt = "⚠ " + txt
        linhas.append(txt)
    enfileirar_email("resumo_diario", None, f"{len(pend)} pendência(s) · {agora_br.strftime('%d/%m/%Y')}",
                     linhas, "Por favor, realize as ações acima e registre-as no sistema. "
                             "Itens com ⚠ estão vencidos ou vencem nos próximos 7 dias.")
    get_db().commit()
    return f"Resumo gerado com {len(pend)} pendência(s)."


# ---------- envio ----------
def conectar_smtp():
    host = config("smtp_host", SMTP_PADRAO["smtp_host"])
    porta = int(config("smtp_porta", SMTP_PADRAO["smtp_porta"]))
    if porta == 465:
        srv = smtplib.SMTP_SSL(host, porta, timeout=20)
    else:
        srv = smtplib.SMTP(host, porta, timeout=20)
        srv.starttls()
    srv.login(config("smtp_usuario"), config("smtp_senha"))
    return srv


def montar_mensagem(assunto, html, para, cc):
    msg = EmailMessage()
    msg["Subject"] = assunto
    msg["From"] = formataddr((config("smtp_nome", SMTP_PADRAO["smtp_nome"]), config("smtp_usuario")))
    msg["To"] = ", ".join(lista_emails(para))
    if lista_emails(cc):
        msg["Cc"] = ", ".join(lista_emails(cc))
    msg.set_content(texto_simples(html))
    msg.add_alternative(html, subtype="html")
    return msg


def enviar_fila():
    """Envia os e-mails pendentes (até 3 tentativas cada)."""
    db = get_db()
    fila = db.execute("SELECT * FROM emails WHERE status = 'PENDENTE' ORDER BY id LIMIT 50").fetchall()
    if not fila:
        return 0
    enviados = 0
    try:
        srv = conectar_smtp()
    except Exception as e:  # conta/senha/rede: registra a falha em cada e-mail
        for em in fila:
            tent = em["tentativas"] + 1
            db.execute("UPDATE emails SET tentativas = ?, erro = ?, status = ? WHERE id = ?",
                       (tent, f"Falha ao conectar: {e}", "ERRO" if tent >= 3 else "PENDENTE", em["id"]))
        db.commit()
        return 0
    try:
        for em in fila:
            try:
                srv.send_message(montar_mensagem(em["assunto"], em["corpo"], em["para"], em["cc"]))
                db.execute("UPDATE emails SET status = 'ENVIADO', erro = NULL, enviado_em = ?,"
                           " tentativas = tentativas + 1 WHERE id = ?", (agora(), em["id"]))
                enviados += 1
            except Exception as e:
                tent = em["tentativas"] + 1
                db.execute("UPDATE emails SET tentativas = ?, erro = ?, status = ? WHERE id = ?",
                           (tent, str(e), "ERRO" if tent >= 3 else "PENDENTE", em["id"]))
            db.commit()
    finally:
        try:
            srv.quit()
        except Exception:
            pass
    return enviados


def tarefas_em_segundo_plano(resumo=True):
    """Roda depois que a página já foi entregue ao usuário: resumo diário (se for a hora) e envio da fila."""
    with app.app_context():
        try:
            if resumo:
                resumo_diario()
            enviar_fila()
        except Exception:
            app.logger.exception("Falha nas tarefas de e-mail")


def tarefa_diaria():
    """Para tarefa agendada (deploy/tarefa_diaria.sh) ou agendador externo: resumo + fila."""
    with app.app_context():
        msg = resumo_diario()
        n = enviar_fila()
        return f"{msg} E-mails enviados agora: {n}."


@app.before_request
def notificacoes_linha_de_base():
    # na primeira alteração após instalar esta versão, registra a situação atual antes de qualquer mudança
    if request.method == "POST" and g.get("usuario") and config("notif_inicio") is None:
        processar_notificacoes()


@app.after_request
def notificacoes_apos_alteracao(resp):
    if request.endpoint == "static":
        return resp
    if request.method == "POST" and g.get("usuario") and resp.status_code < 400:
        try:
            processar_notificacoes()
        except Exception:
            app.logger.exception("Falha ao processar notificações")
    try:
        precisa_resumo = (config("resumo_ultimo") != agora_brasil().date().isoformat()
                          and config("email_fluxos") is not None)
        if g.get("ha_emails_na_fila") or precisa_resumo:
            resp.call_on_close(lambda: tarefas_em_segundo_plano(resumo=precisa_resumo))
    except Exception:
        app.logger.exception("Falha ao agendar e-mails")
    return resp


@app.route("/tarefas/diaria/<chave>")
def tarefa_diaria_url(chave):
    """Endereço para um agendador externo (ex.: cron-job.org) chamar uma vez por dia."""
    certa = config("tarefa_chave")
    if not certa or not secrets.compare_digest(certa, chave):
        abort(404)
    msg = resumo_diario()
    n = enviar_fila()
    return Response(f"OK · {msg} E-mails enviados agora: {n}.", mimetype="text/plain")


# ---------- aba E-mails (administrador) ----------
@app.route("/emails")
@somente_admin
def emails_config():
    if not config("tarefa_chave"):
        salvar_config("tarefa_chave", secrets.token_urlsafe(24))
        get_db().commit()
    historico = get_db().execute(
        "SELECT e.*, i.numero FROM emails e LEFT JOIN invoices i ON i.id = e.invoice_id"
        " ORDER BY e.id DESC LIMIT 60").fetchall()
    contagem = {k: n for k, n in get_db().execute("SELECT status, COUNT(*) FROM emails GROUP BY status")}
    return render_template(
        "emails.html", fluxos=fluxos_para_tela(), salvo=config("email_fluxos") is not None,
        smtp={k: config(k, SMTP_PADRAO.get(k, "")) for k in ("smtp_host", "smtp_porta", "smtp_usuario", "smtp_nome")},
        tem_senha=bool(config("smtp_senha")), resumo_hora=config("resumo_hora", "8"),
        resumo_ultimo=config("resumo_ultimo"), historico=historico, contagem=contagem,
        url_tarefa=re.sub(r"^http://(?=[^/]*pythonanywhere\.com)", "https://",
                          url_for("tarefa_diaria_url", chave=config("tarefa_chave"), _external=True)),
        FLUXOS_EMAIL=FLUXOS_EMAIL, STATUS_EMAIL=STATUS_EMAIL)


@app.route("/emails/conta", methods=["POST"])
@somente_admin
def emails_conta():
    f = request.form
    usuario = f.get("smtp_usuario", "").strip().lower()
    porta = f.get("smtp_porta", "587").strip()
    if usuario and not RE_EMAIL.match(usuario):
        flash("Informe um e-mail válido para a conta de envio.", "erro")
    elif not porta.isdigit():
        flash("A porta deve ser um número (Gmail: 587).", "erro")
    else:
        salvar_config("smtp_usuario", usuario)
        salvar_config("smtp_host", f.get("smtp_host", "").strip() or SMTP_PADRAO["smtp_host"])
        salvar_config("smtp_porta", porta)
        salvar_config("smtp_nome", f.get("smtp_nome", "").strip() or SMTP_PADRAO["smtp_nome"])
        senha = f.get("smtp_senha", "").replace(" ", "")
        if senha:
            salvar_config("smtp_senha", senha)
        if f.get("remover_senha"):
            salvar_config("smtp_senha", None)
        get_db().commit()
        flash("Conta de envio salva.", "ok")
    return redirect(url_for("emails_config") + "#conta")


@app.route("/emails/teste", methods=["POST"])
@somente_admin
def emails_teste():
    destino = request.form.get("destino", "").strip()
    if not RE_EMAIL.match(destino):
        flash("Informe um e-mail válido para o teste.", "erro")
    elif not conta_configurada():
        flash("Configure a conta de envio (e-mail e senha de app) antes do teste.", "erro")
    else:
        html = corpo_email("E-mail de teste", None, ["Se você recebeu esta mensagem, a conta de envio está correta."])
        try:
            srv = conectar_smtp()
            srv.send_message(montar_mensagem("[Invoices] E-mail de teste", html, destino, ""))
            srv.quit()
            flash(f"E-mail de teste enviado para {destino}. Confira a caixa de entrada (e o spam).", "ok")
        except Exception as e:
            flash(f"Não foi possível enviar: {e}", "erro")
    return redirect(url_for("emails_config") + "#conta")


@app.route("/emails/fluxos", methods=["POST"])
@somente_admin
def emails_fluxos():
    f = request.form
    novos, erros = {}, []
    for chave, (titulo, *_r) in FLUXOS_EMAIL.items():
        para, cc = f.get(f"para_{chave}", ""), f.get(f"cc_{chave}", "")
        invalidos = [e for e in lista_emails(para) + lista_emails(cc) if not RE_EMAIL.match(e)]
        if invalidos:
            erros.append(f"{titulo}: e-mail inválido ({', '.join(invalidos)})")
        novos[chave] = {"ativo": bool(f.get(f"ativo_{chave}")), "para": ", ".join(lista_emails(para)),
                        "cc": ", ".join(lista_emails(cc))}
    hora = f.get("resumo_hora", "8")
    if not hora.isdigit() or not 0 <= int(hora) <= 23:
        erros.append("Horário do resumo diário: informe uma hora entre 0 e 23.")
    if erros:
        for e in erros:
            flash(e, "erro")
    else:
        salvar_config("email_fluxos", json.dumps(novos, ensure_ascii=False))
        salvar_config("resumo_hora", hora)
        get_db().commit()
        flash("Destinatários salvos.", "ok")
    return redirect(url_for("emails_config") + "#fluxos")


@app.route("/emails/acao", methods=["POST"])
@somente_admin
def emails_acao():
    acao = request.form.get("acao")
    db = get_db()
    if acao == "reenviar":
        db.execute("UPDATE emails SET status = 'PENDENTE', tentativas = 0 WHERE status = 'ERRO'")
        db.commit()
        n = enviar_fila()
        flash(f"Reenvio concluído: {n} e-mail(s) enviado(s). Veja o histórico abaixo.", "ok")
    elif acao == "resumo":
        msg = resumo_diario(forcar=True)
        n = enviar_fila()
        flash(f"{msg} E-mails enviados agora: {n}.", "ok")
    elif acao == "nova_chave":
        salvar_config("tarefa_chave", secrets.token_urlsafe(24))
        db.commit()
        flash("Novo endereço gerado: atualize-o no agendador externo.", "ok")
    elif acao == "enviar_fila":
        flash(f"E-mails enviados agora: {enviar_fila()}.", "ok")
    return redirect(url_for("emails_config") + "#historico")


@app.route("/emails/<int:eid>")
@somente_admin
def email_ver(eid):
    em = get_db().execute("SELECT * FROM emails WHERE id = ?", (eid,)).fetchone()
    if em is None:
        abort(404)
    return render_template("email_ver.html", em=em, STATUS_EMAIL=STATUS_EMAIL, FLUXOS_EMAIL=FLUXOS_EMAIL)


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
    print(f" Controle de Invoices e Descontos · versão {VERSAO}")
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
