"""
Controle de Invoices e Descontos - Fornecedores

Controla o ciclo completo de cada invoice, da criação até o pagamento completo:
  Importação         -> cria o invoice (itens, moeda, condição de pagamento, deduções de descontos
                        aprovados) e acompanha coleta, embarque, desembaraço, canal e chegada
  Engenharia (admin) -> registra a inspeção, o desconto por má qualidade e acompanha a aprovação
                        do fornecedor; revalida o valor quando necessário
  Financeiro         -> registra pagamentos (antecipado, integral ou parcial com valor segurado),
                        liberações do valor segurado e Swifts; acompanha o fluxo de pagamentos

Arquivos (Swifts e documentos dos lançamentos financeiros) são enviados pelo navegador e ficam no servidor.
"""
import csv
import glob
import io
import unicodedata
import os
import re
import secrets
import json
import mimetypes
import smtplib
import ssl
import socket
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from functools import wraps

from markupsafe import escape

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request,
                   send_file, session, url_for)

import duimp as leitor_duimp  # leitura do extrato da DUIMP (PDF) para o fator de importação
import inspecoes as insp  # inspeção de recebimento (Agregador MEGA) e tradução por termo
import relatorio as rel  # Excel e e-mail do relatório de inspeção ao fornecedor
import relatorios_imp as rimp  # v4.66: relatórios e indicadores da importação (regras de status e de dias)
import exportar_imp as exp_imp  # v4.66: Excel e PDF dos relatórios da importação
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
DB_PATH = os.environ.get("DESCONTOS_DB", os.path.join(INSTANCE_DIR, "descontos.db"))
# arquivos anexados no sistema (lançamentos, Swifts...): ficam no servidor, junto do banco; fora do GitHub
ARQUIVOS_DIR = os.path.join(os.path.dirname(DB_PATH), "arquivos")
ANEXOS_DIR_V49 = os.path.join(os.path.dirname(DB_PATH), "anexos_lancamentos")  # pasta da versão 4.9 (migrada)
ANEXO_MAX_MB = 10                       # por arquivo
EXTENSOES_ANEXO = {"pdf", "png", "jpg", "jpeg", "gif", "webp", "xlsx", "xlsm", "xls", "csv", "docx", "doc", "xml", "txt",
                   "zip", "rar", "7z", "msg", "eml", "ofx"}
MAX_USUARIOS = 20
VERSAO = "4.75"

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

MOEDAS = {"USD": "US$", "EUR": "€", "CHF": "CHF", "BRL": "R$"}
# Condição de pagamento: uma ou mais parcelas (ex.: 25% antecipado + 75% 60 dias após a HAWB)
CONDICOES = {"ANTECIPADO": "Antecipado", "PRAZO": "Após a HAWB"}
PRAZO_ANTECIPADO_DIAS = 5          # pagamento antecipado: conclusão do cadastro (etapa 1) + 5 dias úteis
NACIONALIZACAO_PLANEJADA = (-1, 3)  # impostos de nacionalização: ETA planejado -1 a +3 dias úteis
NACIONALIZACAO_REAL = (0, 2)        # com o ETA real: ETA real a +2 dias úteis
PRAZO_LIBERACAO_DIAS_UTEIS = 5     # liberação do segurado: chegada real do invoice deduzido + 5 dias úteis
CUSTO_IMPORTACAO_ESTIMADO = 0.40   # visibilidade do Financeiro a partir do embarque
# canal de parametrização: opção -> (cor de fundo pastel, cor do texto)
CORES_CANAL = {"Verde": ("#d4f0dc", "#1d5a31"), "Amarelo": ("#fff3c4", "#6b5200"), "Vermelho": ("#fbd5d5", "#8a1c1c")}
STATUS_DESCONTO = {"PENDENTE": "Relatório pendente de envio", "ENVIADO": "Enviado ao fornecedor",
                   "APROVADO": "Aprovado pelo fornecedor"}
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
                    ("exigencia_status", "Situação da exigência", "exigencia", False),
                    ("exigencia_acao", "Ação tomada na exigência", "texto", False),
                    ("exigencia_prazo", "Prazo de resposta da exigência", "data", False),
                    ("data_desembaraco", "Data de desembaraço", "data", True),
                    ("numero_nf", "Número da NF", "linha", True)],
    "CHEGADA": [("data_prog_chegada", "Data programada de chegada", "data", True),
                ("data_chegada", "Data real de chegada na fábrica", "data", True),
                ("nf_liberada", "NF Liberada", "flag", True)],
}
# v4.65: situação da exigência do fiscal (canal amarelo/vermelho); vazio = sem exigência formal
SITUACOES_EXIGENCIA = {"PENDENTE": "Pendente", "CUMPRIDA": "Cumprida", "FINALIZADA": "Finalizada"}
TITULO_ETAPA = {"COLETA": "Coleta", "EMBARQUE": "Embarque", "DESEMBARACO": "Desembaraço", "CHEGADA": "Transporte Terrestre"}
# Situação atual do invoice (calculada) e quem age nela
SITUACOES = {
    "CRIACAO": ("Cadastro incompleto", "importacao"),
    "COLETA": ("Aguardando coleta", "importacao"),
    "EMBARQUE": ("Em embarque", "importacao"),
    "DESEMBARACO": ("Em desembaraço", "importacao"),
    "CHEGADA": ("Em transporte terrestre", "importacao"),
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
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # vários arquivos de até 10 MB num envio


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
    cidade TEXT,                          -- desde a v4.35 guarda o PAÍS do fornecedor (campo "País")
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
    tipo TEXT NOT NULL,                   -- ANTECIPADO (conclusão do cadastro + 5 dias úteis) | PRAZO (HAWB + dias)
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
-- versão 4.38: descontos avulsos (sem invoice de origem), cadastrados pela Engenharia de Processos para um fornecedor,
-- com justificativa; a Importação os deduz nos próximos invoices do fornecedor (mesma moeda)
CREATE TABLE IF NOT EXISTS descontos_avulsos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fornecedor_id INTEGER NOT NULL REFERENCES fornecedores(id),
    moeda TEXT NOT NULL,
    valor REAL NOT NULL,
    justificativa TEXT NOT NULL,
    criado_por INTEGER REFERENCES usuarios(id),
    criado_em TEXT NOT NULL,
    encerrado_em TEXT,                    -- saldo restante cancelado pela Engenharia
    encerrado_por INTEGER REFERENCES usuarios(id),
    encerrado_motivo TEXT
);
CREATE TABLE IF NOT EXISTS deducoes_avulsas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    avulso_id INTEGER NOT NULL REFERENCES descontos_avulsos(id),
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    valor REAL NOT NULL,
    criado_por INTEGER REFERENCES usuarios(id),
    criado_em TEXT NOT NULL,
    UNIQUE (avulso_id, invoice_id)
);
-- versão 4.44: dados da DUIMP de cada invoice (lidos do PDF e conferidos pela Importação) — base do fator de importação
CREATE TABLE IF NOT EXISTS duimp_dados (
    invoice_id INTEGER PRIMARY KEY REFERENCES invoices(id),
    numero TEXT,
    data_registro TEXT,
    moeda TEXT,
    taxa_cambio REAL,                     -- taxa da DUIMP (nacionalização)
    fob_moeda REAL,
    fob_brl REAL,
    frete_brl REAL,
    valor_aduaneiro REAL,
    ii REAL, ipi REAL, pis REAL, cofins REAL, icms REAL, taxa_siscomex REAL, multa REAL,
    arquivo_id INTEGER,                   -- PDF de onde os dados foram lidos (vazio = digitado)
    conferido_por INTEGER REFERENCES usuarios(id),
    conferido_em TEXT
);
-- versão 4.53: inspeção de recebimento (eventos extraídos do Agregador MEGA) e glossário de tradução por termo
CREATE TABLE IF NOT EXISTS inspecao_eventos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grupo TEXT NOT NULL,                  -- "inv:<id do invoice>" ou "xls:<invoice do Excel>" (sem vínculo)
    invoice_id INTEGER REFERENCES invoices(id),
    invoice_excel TEXT, numero_nf TEXT, fornecedor_excel TEXT,
    produto TEXT, seq INTEGER, descricao TEXT, codigo_mega TEXT, quantidade REAL, criterio TEXT,
    categoria_original TEXT,              -- pelo critério do MEGA
    categoria TEXT,                       -- final (pode ser reclassificada pela Engenharia de Processos)
    comentario TEXT, data_inclusao TEXT, data_inspecao TEXT, inspetor TEXT, cod_inspetor TEXT, report_date TEXT,
    lote TEXT, area TEXT, familia TEXT, family TEXT, qtde_nf REAL, vlr_unit_nf REAL, outros TEXT,
    reclass_por INTEGER REFERENCES usuarios(id), reclass_em TEXT, reclass_motivo TEXT,
    importacao_id INTEGER, ordem INTEGER
);
CREATE INDEX IF NOT EXISTS idx_inspecao_grupo ON inspecao_eventos(grupo);
CREATE INDEX IF NOT EXISTS idx_inspecao_invoice ON inspecao_eventos(invoice_id);
CREATE TABLE IF NOT EXISTS inspecao_importacoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    arquivo TEXT, eventos INTEGER, grupos INTEGER, resumo TEXT,
    criado_por INTEGER REFERENCES usuarios(id), criado_em TEXT NOT NULL
);
-- versão 4.56: fechamento do relatório de desconto (reclassificações, políticas, ajustes e versões do cálculo)
CREATE TABLE IF NOT EXISTS inspecao_reclass (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grupo TEXT NOT NULL, invoice_id INTEGER, produto TEXT, comentario TEXT,
    quantidade REAL, de TEXT, para TEXT, eventos INTEGER, parcial INTEGER NOT NULL DEFAULT 0,
    motivo TEXT NOT NULL, criado_por INTEGER REFERENCES usuarios(id), criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS politicas_desconto (
    fornecedor_id INTEGER PRIMARY KEY,    -- 0 = política padrão da empresa
    tipo TEXT NOT NULL,                   -- FIXA: % fixos de C e D | FAIXAS: % de C conforme o % de A+B do item
    pct_c REAL, pct_d REAL, faixas TEXT,  -- faixas: [[% mínimo de A+B, % de C], ...]
    atualizado_por INTEGER REFERENCES usuarios(id), atualizado_em TEXT
);
CREATE TABLE IF NOT EXISTS desconto_ajustes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    produto TEXT,                         -- vazio = ajuste geral do invoice
    pct_c REAL, pct_d REAL,               -- vazios = volta à política
    motivo TEXT NOT NULL, criado_por INTEGER REFERENCES usuarios(id), criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relatorios_versoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    versao INTEGER NOT NULL, total REAL NOT NULL, linhas TEXT NOT NULL, politica TEXT,
    motivo TEXT, divergencias TEXT,       -- diferenças de quantidade confirmadas ao salvar
    criado_por INTEGER REFERENCES usuarios(id), criado_em TEXT NOT NULL,
    aplicado_em TEXT                      -- fase 3: quando vira o desconto do invoice (relatório enviado)
);
-- versão 4.59: nome do fornecedor no MEGA -> nome no sistema
CREATE TABLE IF NOT EXISTS fornecedor_mega (
    nome_mega TEXT PRIMARY KEY COLLATE NOCASE,
    nome_sistema TEXT NOT NULL,
    atualizado_por INTEGER REFERENCES usuarios(id), atualizado_em TEXT
);
CREATE TABLE IF NOT EXISTS traducao_termos (
    chave TEXT PRIMARY KEY,               -- termo normalizado (maiúsculas, sem acento)
    texto_pt TEXT, texto_en TEXT NOT NULL,
    origem TEXT,                          -- 'planilha' (cadastro antigo) ou 'sistema'
    atualizado_por INTEGER REFERENCES usuarios(id), atualizado_em TEXT
);
-- Pagamentos registrados pelo Financeiro
CREATE TABLE IF NOT EXISTS pagamentos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    tipo TEXT NOT NULL,                   -- PRINCIPAL | LIBERACAO
    valor REAL NOT NULL,
    data_pagamento TEXT NOT NULL,
    banco TEXT,
    swift_url TEXT,                       -- legado: link do Swift no OneDrive (desde a v4.10 o Swift é arquivo anexado)
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
-- versão 4.4: lançamentos financeiros (fora do fluxo; todos os campos opcionais)
CREATE TABLE IF NOT EXISTS lancamentos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    tipo TEXT,                            -- livre: COFINS, ICMS, PIS, Armazenagem...
    valor REAL,                           -- em reais
    data_pagamento TEXT,
    criado_por INTEGER REFERENCES usuarios(id),
    criado_em TEXT NOT NULL,
    atualizado_por INTEGER REFERENCES usuarios(id),
    atualizado_em TEXT,
    lancado_erp INTEGER NOT NULL DEFAULT 0,      -- marcado pelo Financeiro quando já lançou no MEGA
    lancado_erp_por INTEGER REFERENCES usuarios(id),
    lancado_erp_em TEXT,
    alterado_apos_erp INTEGER NOT NULL DEFAULT 0 -- a Importação mudou depois de lançado: conferir no MEGA
);
CREATE INDEX IF NOT EXISTS idx_lancamentos_invoice ON lancamentos(invoice_id);
-- versão 4.9: arquivos anexados aos lançamentos financeiros
CREATE TABLE IF NOT EXISTS lancamento_anexos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lancamento_id INTEGER NOT NULL REFERENCES lancamentos(id) ON DELETE CASCADE,
    nome TEXT NOT NULL,                   -- nome original do arquivo
    arquivo TEXT NOT NULL,                -- (v4.9; migrado para a tabela arquivos)
    tamanho INTEGER NOT NULL,
    enviado_por INTEGER REFERENCES usuarios(id),
    enviado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lanc_anexos ON lancamento_anexos(lancamento_id);
-- versão 4.10: todos os arquivos anexados do sistema (substitui lancamento_anexos e os links do OneDrive)
CREATE TABLE IF NOT EXISTS arquivos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origem TEXT NOT NULL,                 -- lancamento | pagamento
    origem_id INTEGER NOT NULL,
    nome TEXT NOT NULL,                   -- nome original do arquivo
    arquivo TEXT NOT NULL,                -- nome gravado no servidor (pasta arquivos)
    tamanho INTEGER NOT NULL,
    enviado_por INTEGER REFERENCES usuarios(id),
    enviado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_arquivos_origem ON arquivos(origem, origem_id);
-- versão 4.19: códigos de redefinição de senha (guardados só como hash)
CREATE TABLE IF NOT EXISTS senha_codigos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    codigo_hash TEXT NOT NULL,
    criado_em TEXT NOT NULL,
    expira_em TEXT NOT NULL,
    tentativas INTEGER NOT NULL DEFAULT 0,
    usado_em TEXT
);
-- versão 4.25: condição de pagamento padrão e lembretes por fornecedor
CREATE TABLE IF NOT EXISTS fornecedor_condicao (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fornecedor_id INTEGER NOT NULL REFERENCES fornecedores(id) ON DELETE CASCADE,
    ordem INTEGER NOT NULL,
    percentual REAL NOT NULL,
    tipo TEXT NOT NULL,                   -- ANTECIPADO | PRAZO
    dias INTEGER
);
CREATE TABLE IF NOT EXISTS fornecedor_lembretes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fornecedor_id INTEGER NOT NULL REFERENCES fornecedores(id) ON DELETE CASCADE,
    texto TEXT NOT NULL,
    criado_por INTEGER REFERENCES usuarios(id),
    criado_em TEXT NOT NULL,
    ok_por INTEGER REFERENCES usuarios(id),  -- quem deu OK (o lembrete deixa de aparecer)
    ok_em TEXT
);
-- versão 4.27: documentos = caminho do arquivo na pasta compartilhada da empresa (rede interna)
CREATE TABLE IF NOT EXISTS documentos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origem TEXT NOT NULL,                 -- lancamento | pagamento (Swift) | duimp
    origem_id INTEGER NOT NULL,
    caminho TEXT NOT NULL,                -- caminho do arquivo na rede (servidor/compartilhamento ou unidade Z:)
    status TEXT NOT NULL DEFAULT 'NAO_VERIFICADO',  -- NAO_VERIFICADO | OK | NAO_ENCONTRADO
    cadastrado_por INTEGER REFERENCES usuarios(id),
    cadastrado_em TEXT NOT NULL,
    verificado_em TEXT,
    problema_em TEXT,
    problema_por INTEGER REFERENCES usuarios(id),  -- vazio = verificação automática
    problema_obs TEXT,
    -- "impressão digital" do arquivo escolhido na tela (para a futura leitura/validação do conteúdo)
    nome_arquivo TEXT,
    tamanho INTEGER,
    arquivo_modificado_em TEXT,
    sha256 TEXT
);
CREATE INDEX IF NOT EXISTS idx_documentos_origem ON documentos(origem, origem_id);
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
        # versão 4.20: número da NF (Desembaraço) e liberação do pagamento após a HAWB pela Engenharia
        if "exigencia_status" not in {r[1] for r in db.execute("PRAGMA table_info(invoices)")}:
            db.execute("ALTER TABLE invoices ADD COLUMN exigencia_status TEXT")  # v4.65
        for col in ("exigencia_acao", "exigencia_prazo"):  # v4.66
            if col not in {r[1] for r in db.execute("PRAGMA table_info(invoices)")}:
                db.execute(f"ALTER TABLE invoices ADD COLUMN {col} TEXT")
        colunas_inv = {r[1] for r in db.execute("PRAGMA table_info(invoices)")}
        for col, tipo in (("numero_nf", "TEXT"), ("pagamento_liberado_em", "TEXT"), ("pagamento_liberado_por", "INTEGER"),
                          ("pagamento_liberado_obs", "TEXT"), ("historico", "INTEGER NOT NULL DEFAULT 0"),
                          ("nf_liberada", "INTEGER"), ("cadastro_concluido_em", "TEXT"),
                          ("duimp_banco_em", "TEXT"), ("duimp_banco_por", "INTEGER"),
                          ("duimp_dispensada", "INTEGER")):
            if col not in colunas_inv:
                db.execute(f"ALTER TABLE invoices ADD COLUMN {col} {tipo}")
        # versão 4.71: invoices com relatório de inspeção já enviado/registrado: fim da inspeção = última REPORT DATE
        if db.execute("SELECT 1 FROM configuracoes WHERE chave = 'v471_fim_inspecao'").fetchone() is None:
            db.execute("UPDATE invoices SET data_fim_inspecao = (SELECT MAX(substr(e.report_date, 1, 10)) FROM inspecao_eventos e"
                       " WHERE e.invoice_id = invoices.id) WHERE id IN (SELECT invoice_id FROM relatorios_versoes"
                       " WHERE aplicado_em IS NOT NULL) AND EXISTS (SELECT 1 FROM inspecao_eventos e WHERE e.invoice_id ="
                       " invoices.id AND e.report_date IS NOT NULL)")
            db.execute("INSERT INTO configuracoes (chave, valor) VALUES ('v471_fim_inspecao', '1')")
        # versão 4.30: invoices que já passaram do Transporte não voltam a ficar pendentes da nova "NF Liberada"
        if db.execute("SELECT 1 FROM configuracoes WHERE chave = 'v430_nf'").fetchone() is None:
            db.execute("UPDATE invoices SET nf_liberada = 1 WHERE data_chegada IS NOT NULL")
            db.execute("INSERT INTO configuracoes (chave, valor) VALUES ('v430_nf', '1')")
        # versão 4.33: "DUIMP enviada ao banco" (Financeiro). Invoices que já chegaram à fábrica não viram pendência.
        if db.execute("SELECT 1 FROM configuracoes WHERE chave = 'v433_duimp_banco'").fetchone() is None:
            db.execute("UPDATE invoices SET duimp_banco_em = 'antes da v4.33' WHERE data_chegada IS NOT NULL"
                       " AND duimp_banco_em IS NULL")
            db.execute("INSERT INTO configuracoes (chave, valor) VALUES ('v433_duimp_banco', '1')")
        # versão 4.43: DUIMP (arquivo) obrigatória no Desembaraço; os que já passaram do desembaraço ficam dispensados
        if db.execute("SELECT 1 FROM configuracoes WHERE chave = 'v443_duimp'").fetchone() is None:
            db.execute("UPDATE invoices SET duimp_dispensada = 1 WHERE data_desembaraco IS NOT NULL AND id NOT IN"
                       " (SELECT origem_id FROM arquivos WHERE origem = 'duimp')")
            db.execute("INSERT INTO configuracoes (chave, valor) VALUES ('v443_duimp', '1')")
        # v4.48: pagamentos anteriores a esta versão sem cotação não viram pendência
        if db.execute("SELECT 1 FROM configuracoes WHERE chave = 'v448_cotacao_desde'").fetchone() is None:
            db.execute("INSERT INTO configuracoes (chave, valor) VALUES ('v448_cotacao_desde', ?)", (agora(),))
        if "segurado_incluido" not in {r[1] for r in db.execute("PRAGMA table_info(pagamentos)")}:
            db.execute("ALTER TABLE pagamentos ADD COLUMN segurado_incluido REAL")  # v4.49: parte do segurado paga junto
        if "cotacao" not in {r[1] for r in db.execute("PRAGMA table_info(pagamentos)")}:
            db.execute("ALTER TABLE pagamentos ADD COLUMN cotacao REAL")  # v4.43: R$ por unidade da moeda no pagamento
        # versão 4.44: tipos livres dos lançamentos que correspondem a uma categoria de custo passam a usar o nome dela
        if db.execute("SELECT 1 FROM configuracoes WHERE chave = 'v444_categorias'").fetchone() is None:
            for antigo, novo in MAPA_TIPOS_ANTIGOS.items():
                db.execute("UPDATE lancamentos SET tipo = ? WHERE lower(trim(tipo)) = ?", (novo, antigo))
            db.execute("INSERT INTO configuracoes (chave, valor) VALUES ('v444_categorias', '1')")
        # versão 4.56: chave de origem do evento (para manter reclassificações e divisões entre importações)
        if "chave_origem" not in {r[1] for r in db.execute("PRAGMA table_info(inspecao_eventos)")}:
            db.execute("ALTER TABLE inspecao_eventos ADD COLUMN chave_origem TEXT")
        # versão 4.60: a falha encontrada (comentário) pode ser corrigida; guarda a original
        if "comentario_original" not in {r[1] for r in db.execute("PRAGMA table_info(inspecao_eventos)")}:
            db.execute("ALTER TABLE inspecao_eventos ADD COLUMN comentario_original TEXT")
        # versão 4.61: e-mails do fornecedor para os relatórios de inspeção; envio da versão do relatório
        if "emails_relatorio" not in {r[1] for r in db.execute("PRAGMA table_info(fornecedores)")}:
            db.execute("ALTER TABLE fornecedores ADD COLUMN emails_relatorio TEXT")
        if "email_id" not in {r[1] for r in db.execute("PRAGMA table_info(relatorios_versoes)")}:
            db.execute("ALTER TABLE relatorios_versoes ADD COLUMN email_id INTEGER")
            db.execute("ALTER TABLE relatorios_versoes ADD COLUMN arquivo_id INTEGER")
            db.execute("ALTER TABLE relatorios_versoes ADD COLUMN enviado_por INTEGER")
        if "comentario_novo" not in {r[1] for r in db.execute("PRAGMA table_info(inspecao_reclass)")}:
            db.execute("ALTER TABLE inspecao_reclass ADD COLUMN comentario_novo TEXT")
        if db.execute("SELECT 1 FROM politicas_desconto WHERE fornecedor_id = 0").fetchone() is None:
            db.execute("INSERT INTO politicas_desconto (fornecedor_id, tipo, pct_c, pct_d) VALUES (0, 'FIXA', 80, 100)")
            for f in db.execute("SELECT id FROM fornecedores WHERE lower(nome) LIKE '%orthogent%'").fetchall():
                db.execute("INSERT OR IGNORE INTO politicas_desconto (fornecedor_id, tipo, pct_d, faixas) VALUES (?, 'FAIXAS', 100, ?)",
                           (f[0], json.dumps(FAIXAS_ORTHOGENT)))
        # versão 4.59: relação inicial dos nomes de fornecedor MEGA × sistema
        if db.execute("SELECT 1 FROM configuracoes WHERE chave = 'v459_fornecedor_mega'").fetchone() is None:
            db.executemany("INSERT OR IGNORE INTO fornecedor_mega (nome_mega, nome_sistema) VALUES (?, ?)", FORNECEDORES_MEGA_INICIAL)
            db.execute("INSERT INTO configuracoes (chave, valor) VALUES ('v459_fornecedor_mega', '1')")
        # versão 4.22: invoices/pagamentos lançados pela carga de histórico (dados do passado)
        if "historico" not in {r[1] for r in db.execute("PRAGMA table_info(pagamentos)")}:
            db.execute("ALTER TABLE pagamentos ADD COLUMN historico INTEGER NOT NULL DEFAULT 0")
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
        # versão 4.10: anexos dos lançamentos (v4.9) passam para a tabela/pasta únicas de arquivos
        antigos = db.execute("SELECT * FROM lancamento_anexos").fetchall()
        if antigos:
            os.makedirs(ARQUIVOS_DIR, exist_ok=True)
            for a in antigos:  # (id, lancamento_id, nome, arquivo, tamanho, enviado_por, enviado_em)
                origem_arq = os.path.join(ANEXOS_DIR_V49, a[3])
                if os.path.exists(origem_arq):
                    os.replace(origem_arq, os.path.join(ARQUIVOS_DIR, a[3]))
                db.execute("INSERT INTO arquivos (origem, origem_id, nome, arquivo, tamanho, enviado_por, enviado_em)"
                           " VALUES ('lancamento', ?, ?, ?, ?, ?, ?)", (a[1], a[2], a[3], a[4], a[5], a[6]))
            db.execute("DELETE FROM lancamento_anexos")
        # versão 4.28: "impressão digital" do arquivo escolhido (base para a futura leitura/validação do arquivo)
        colunas_doc = {r[1] for r in db.execute("PRAGMA table_info(documentos)")}
        for col, tipo in (("nome_arquivo", "TEXT"), ("tamanho", "INTEGER"), ("arquivo_modificado_em", "TEXT"),
                          ("sha256", "TEXT")):
            if col not in colunas_doc:
                db.execute(f"ALTER TABLE documentos ADD COLUMN {col} {tipo}")
        # versão 4.9: e-mails podem levar arquivos anexados
        if "anexos" not in {r[1] for r in db.execute("PRAGMA table_info(emails)")}:
            db.execute("ALTER TABLE emails ADD COLUMN anexos TEXT")
        # versão 4.5: controle do lançamento no MEGA pelo Financeiro
        colunas_l = {r[1] for r in db.execute("PRAGMA table_info(lancamentos)")}
        for col, tipo in (("lancado_erp", "INTEGER NOT NULL DEFAULT 0"), ("lancado_erp_por", "INTEGER"),
                          ("lancado_erp_em", "TEXT"), ("alterado_apos_erp", "INTEGER NOT NULL DEFAULT 0")):
            if col not in colunas_l:
                db.execute(f"ALTER TABLE lancamentos ADD COLUMN {col} {tipo}")
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
        corrigir_horarios_utc(db)


def corrigir_horarios_utc(db):
    """Versão 4.21: até aqui os registros usavam o relógio do servidor. Num servidor em UTC (PythonAnywhere),
    converte uma única vez as datas/horas gravadas (colunas *_em) para o horário de Brasília."""
    if db.execute("SELECT 1 FROM configuracoes WHERE chave = 'fuso_brasilia'").fetchone():
        return
    servidor_em_utc = abs((datetime.now() - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()) < 60
    if servidor_em_utc:
        for (tabela,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                                    " AND name NOT LIKE 'legacy_%'").fetchall():
            for col in [c[1] for c in db.execute(f'PRAGMA table_info("{tabela}")') if c[1].endswith("_em")]:
                db.execute(f'UPDATE "{tabela}" SET "{col}" = strftime(\'%Y-%m-%d %H:%M:%S\', "{col}", \'-3 hours\')'
                           f' WHERE "{col}" GLOB \'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]\'')
        for chave in ("notif_inicio",):
            db.execute("UPDATE configuracoes SET valor = strftime('%Y-%m-%d %H:%M:%S', valor, '-3 hours')"
                       " WHERE chave = ? AND valor GLOB '????-??-?? ??:??:??'", (chave,))
    db.execute("INSERT INTO configuracoes (chave, valor) VALUES ('fuso_brasilia', ?)",
               ("convertido de UTC" if servidor_em_utc else "servidor já no horário local",))


# Todo o sistema usa o horário de Brasília (UTC−3, sem horário de verão), qualquer que seja o fuso do
# servidor (o PythonAnywhere roda em UTC/GMT 0).
FUSO_BRASIL = timedelta(hours=-3)


def agora_dt():
    """Data e hora atuais em Brasília (sem fuso no objeto)."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + FUSO_BRASIL


def agora():
    return agora_dt().strftime("%Y-%m-%d %H:%M:%S")


def hoje():
    return agora_dt().date().isoformat()


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
def fmt_num(valor, casas=2):
    if not isinstance(valor, (int, float)):
        return ""
    return f"{valor:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


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
    """Antecipado: conclusão do cadastro (etapa 1) + 5 dias úteis. A prazo: data da HAWB (registrada na Coleta) + dias da condição;
    antes da HAWB, a parcela a prazo fica sem vencimento (não há estimativa)."""
    if c["tipo"] == "ANTECIPADO":
        base = inv["cadastro_concluido_em"]
        return somar_dias_uteis(base[:10], PRAZO_ANTECIPADO_DIAS) if base else None
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
    # ordem dos pagamentos: primeiro as parcelas antecipadas, depois as após a HAWB por vencimento
    # (as após a HAWB dependem da liberação da Engenharia; o valor segurado sai delas, as últimas)
    ordem = sorted(range(len(parcelas)), key=lambda i: (parcelas[i]["tipo"] != "ANTECIPADO",
                                                        parcelas[i]["vencimento"] is None,
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
    # deduções de descontos avulsos (v4.38: sem invoice de origem)
    r["deducoes_avulsas"] = db.execute(
        "SELECT x.*, a.justificativa, a.valor AS valor_avulso FROM deducoes_avulsas x"
        " JOIN descontos_avulsos a ON a.id = x.avulso_id WHERE x.invoice_id = ? ORDER BY x.id", (iid,)).fetchall()
    r["deducoes_aplicadas"] = round(sum(x["valor"] for x in r["deducoes_recebidas"])
                                    + sum(x["valor"] for x in r["deducoes_avulsas"]), 2)
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
    # Swift: arquivo anexado ao pagamento (pagamentos antigos podem ter só o link do OneDrive, que continua valendo)
    r["swift_arquivos"] = arquivos_de("pagamento", [p["id"] for p in r["pagamentos"]])
    r["swift_docs"] = documentos_de("pagamento", [p["id"] for p in r["pagamentos"]])
    r["swifts_pendentes"] = sum(1 for p in r["pagamentos"] if not p["swift_url"] and not r["swift_arquivos"].get(p["id"])
                                and not r["swift_docs"].get(p["id"]) and not p["historico"])
    r["parcelas"] = calcular_parcelas(r)
    # parcelas após a HAWB ficam bloqueadas até a Engenharia de Processos liberar o pagamento ao Financeiro
    r["pagamento_liberado"] = bool(r["pagamento_liberado_em"])
    for p in r["parcelas"]:
        p["bloqueado"] = p["tipo"] == "PRAZO" and not r["pagamento_liberado"]
    r["tem_parcela_prazo"] = any(p["tipo"] == "PRAZO" for p in r["parcelas"])
    r["principal_bloqueado"] = round(sum(p["pendente"] for p in r["parcelas"] if p["bloqueado"]), 2)
    r["principal_liberado_pendente"] = max(round(r["principal_pendente"] - r["principal_bloqueado"], 2), 0) \
        if r["parcelas"] else r["principal_pendente"]
    # v4.49: caso excepcional — o Financeiro paga o invoice inteiro, inclusive o valor segurado (nada fica retido).
    # O desconto continua valendo para dedução no próximo invoice; só deixa de haver liberação do segurado.
    r["segurado_pagavel"] = max(round(r["segurado"] - r["liberado"], 2), 0) \
        if r["desconto"] and r["principal_bloqueado"] <= TOLERANCIA else 0
    r["duimp_arquivos"] = arquivos_de("duimp", [iid]).get(iid, [])
    r["duimp_docs"] = documentos_de("duimp", [iid]).get(iid, [])
    r["invoice_docs"] = documentos_de("invoice", [iid]).get(iid, [])
    r["invoice_arquivos"] = arquivos_de("invoice", [iid]).get(iid, [])
    # v4.31: os anexos voltaram a ser arquivos guardados no sistema; os caminhos na rede antigos (v4.27 a 4.30)
    # ficam só para consulta e não geram mais pendência de "arquivo não encontrado".
    r["docs_quebrados"] = []
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
    # descontos avulsos do fornecedor (id "a<número>" para não confundir com os descontos de invoices)
    for a in avulsos_com_saldo(fornecedor_id, moeda):
        lista.append({"id": f"a{a['id']}", "invoice": f"Avulso #{a['id']}", "valor_final": a["valor"], "saldo": a["saldo"],
                      "data_relatorio": a["criado_em"][:10], "avulso": True, "justificativa": a["justificativa"]})
    return lista


def avulsos_de(fornecedor_id=None, moeda=None):
    """Descontos avulsos com o total deduzido e o saldo."""
    sql = ("SELECT a.*, f.nome AS fornecedor, u.nome AS autor, e.nome AS encerrado_por_nome,"
           " COALESCE((SELECT SUM(valor) FROM deducoes_avulsas x WHERE x.avulso_id = a.id), 0) AS deduzido"
           " FROM descontos_avulsos a JOIN fornecedores f ON f.id = a.fornecedor_id"
           " LEFT JOIN usuarios u ON u.id = a.criado_por LEFT JOIN usuarios e ON e.id = a.encerrado_por WHERE 1 = 1")
    params = []
    if fornecedor_id is not None:
        sql += " AND a.fornecedor_id = ?"
        params.append(fornecedor_id)
    if moeda is not None:
        sql += " AND a.moeda = ?"
        params.append(moeda)
    lista = []
    for a in get_db().execute(sql + " ORDER BY a.id DESC", params).fetchall():
        a = dict(a)
        a["saldo"] = 0 if a["encerrado_em"] else round(a["valor"] - a["deduzido"], 2)
        lista.append(a)
    return lista


def avulsos_com_saldo(fornecedor_id, moeda):
    return sorted([a for a in avulsos_de(fornecedor_id, moeda) if a["saldo"] > TOLERANCIA], key=lambda a: a["id"])


def id_desconto(txt):
    """'12' -> 12 (desconto de invoice); 'a3' -> 'a3' (desconto avulso); inválido -> None."""
    txt = (txt or "").strip()
    if txt.isdigit():
        return int(txt)
    if re.fullmatch(r"a\d+", txt):
        return txt
    return None


def gravar_deducao(did, iid, numero_destino, valor, moeda, origem_txt):
    """Grava a dedução (de invoice ou avulsa) e os eventos. Não faz commit."""
    db = get_db()
    if isinstance(did, str):
        aid = int(did[1:])
        db.execute("INSERT INTO deducoes_avulsas (avulso_id, invoice_id, valor, criado_por, criado_em) VALUES (?, ?, ?, ?, ?)",
                   (aid, iid, valor, g.usuario["id"], agora()))
        registrar_evento(iid, f"Dedução de {fmt_moeda(valor, moeda)} do desconto avulso #{aid}", origem_txt)
        return
    db.execute("INSERT INTO deducoes (desconto_id, invoice_id, valor, criado_por, criado_em) VALUES (?, ?, ?, ?, ?)",
               (did, iid, valor, g.usuario["id"], agora()))
    registrar_evento(iid, f"Dedução de {fmt_moeda(valor, moeda)} do desconto do invoice {origem_txt}")
    origem_id = db.execute("SELECT invoice_id FROM descontos WHERE id = ?", (did,)).fetchone()[0]
    registrar_evento(origem_id, f"Desconto deduzido no invoice {numero_destino}: {fmt_moeda(valor, moeda)}")


app.jinja_env.globals.update(
    ANEXO_MAX_MB=ANEXO_MAX_MB,
    csrf_token=lambda: csrf_token(), PERFIS=PERFIS, SETORES=SETORES, MOEDAS=MOEDAS, CONDICOES=CONDICOES, SITUACOES=SITUACOES,
    CAMPOS_ETAPA=CAMPOS_ETAPA, TITULO_ETAPA=TITULO_ETAPA, SITUACOES_EXIGENCIA=SITUACOES_EXIGENCIA, ORDEM_LOGISTICA=ORDEM_LOGISTICA, CORES_CANAL=CORES_CANAL,
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
        limite = (agora_dt() - timedelta(minutes=BLOQUEIO_MINUTOS)).strftime("%Y-%m-%d %H:%M:%S")
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

    def add(r, acao, quem, data=None, valor=None, alerta=False, atencao=False):
        # alerta = vencido/atrasado (vermelho); atencao = vence em até 7 dias ou problema a corrigir (amarelo)
        pend.append({"r": r, "acao": acao, "quem": quem, "data": data, "valor": valor, "alerta": alerta,
                     "atencao": atencao and not alerta})

    lim_7 = somar_dias(hoje(), 7)
    for r in invoices:
        sit = r["situacao"]
        if sit == "CANCELADO":
            continue
        if r["historico"]:  # v4.51: invoices da carga de histórico não geram pendências
            continue
        if imp and sit == "CRIACAO":
            add(r, "Completar o cadastro: falta " + ", ".join(criacao_faltando(r)), "importacao")
        if imp and sit in ORDEM_LOGISTICA:
            # o que a Importação precisa preencher para concluir a etapa (o banco de débito é pendência do Financeiro)
            falta = [f for f in etapa_faltando(r, sit) if not f.startswith("Banco de débito")]
            if falta:
                add(r, f"{TITULO_ETAPA[sit]}: falta " + ", ".join(falta), "importacao",
                    r["data_prog_chegada"] if sit == "CHEGADA" else None)
        opcionais = [] if r["historico"] else opcionais_faltando(r)
        if imp and opcionais:
            add(r, "Informações não obrigatórias a preencher: " + ", ".join(opcionais), "importacao")
        if adm and sit == "INSPECAO":
            if not r["data_fim_inspecao"]:
                add(r, "Registrar o fim da inspeção", "admin", r["data_chegada"])
            elif r["tem_desconto"] is None:  # v4.45: data registrada, resultado ainda não definido
                add(r, "Relatório de Inspeção Pendente", "admin", r["data_fim_inspecao"], atencao=True)
            else:
                add(r, "Registrar a inspeção", "admin", r["data_chegada"])
        d = r["desconto"]
        if adm and d and d["status"] == "PENDENTE":
            add(r, "Enviar o relatório de inspeção (desconto) ao fornecedor", "admin", d["data_relatorio"], d["valor_final"])
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
                if p["bloqueado"]:  # aprovação da Engenharia de Processos antes do pagamento
                    add(r, "Liberar o pagamento ao Financeiro" + rotulo + (" (já vencido)" if vencido else ""),
                        "admin", p["vencimento"], p["pendente"], alerta=vencido, atencao=p["vencimento"] <= lim_7)
                    continue
                add(r, ("Pagamento vencido" if vencido else "Pagar invoice") + rotulo, "financeiro", p["vencimento"],
                    p["pendente"], alerta=vencido, atencao=p["vencimento"] <= lim_7)
        if fin:
            for p in r["parcelas_liberacao"]:
                if p["status"] != "DISPONIVEL" or p["pendente"] <= TOLERANCIA:
                    continue
                vencido = p["vencimento"] < hoje()
                add(r, ("Liberação do segurado vencida" if vencido else "Liberar valor segurado")
                    + (f" (invoice {p['invoice']} chegou)" if p["invoice"] else " (revalidação)"), "financeiro",
                    p["vencimento"], p["pendente"], alerta=vencido, atencao=p["vencimento"] <= lim_7)
        if fin and r["swifts_pendentes"]:
            add(r, f"Anexar o Swift ({r['swifts_pendentes']} pagamento(s))", "financeiro")
        # v4.48: cotação não é obrigatória ao registrar, mas fica pendente até ser informada (em "Corrigir")
        if fin and r["moeda"] != "BRL" and not r["historico"]:
            desde = config("v448_cotacao_desde") or ""
            sem_cot = [p for p in r["pagamentos"] if not p["cotacao"] and not p["historico"]
                       and (p["registrado_em"] or "") >= desde]
            if sem_cot:
                add(r, "Informar a cotação do pagamento de " + ", ".join(fmt_data(p["data_pagamento"]) for p in sem_cot)
                    + " (em Corrigir)", "financeiro", sem_cot[0]["data_pagamento"])
        for doc in r["docs_quebrados"]:
            add(r, f"Arquivo não encontrado na pasta da rede ({NOME_DOCUMENTO[doc['origem']]}): corrigir o caminho "
                   f"{doc['caminho']}", SETOR_DOCUMENTO[doc["origem"]], atencao=True)
        if fin and r["data_registro_br"] and not r["duimp_banco_em"] and not r["historico"]:
            add(r, "Enviar a DUIMP ao banco e marcar \"DUIMP enviada ao banco\"", "financeiro", r["data_registro_br"])
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
    for r in lista:
        r["progresso"] = progresso_fluxo(r)
        r["fator"], r["fator_final"] = fator_resumido(r["id"], r) if r["situacao"] != "CANCELADO" else (None, False)
    alertas_imp = rimp.alertas(processos_importacao(invoices), hj)  # v4.66
    return render_template("painel.html", invoices=lista, contagem=contagem, fornecedores=fornecedores, alertas_imp=alertas_imp,
                           filtro_sit=filtro_sit, filtro_forn=filtro_forn, filtro_moeda=filtro_moeda, busca=busca,
                           hoje=hj, ETAPAS_PAINEL=ETAPAS_PAINEL)


@app.route("/pendencias")
@login_obrigatorio
def pendencias():
    """Pendências de todos os setores (as do setor de quem está logado no topo)."""
    pend = listar_pendencias(todos_invoices(incluir_cancelados=True))
    por_setor = {k: sum(1 for p in pend if p["quem"] == k) for k in SETORES}
    return render_template("pendencias.html", pendencias=pend, por_setor=por_setor,
                           vencidas=sum(1 for p in pend if p["alerta"]), atencao=sum(1 for p in pend if p["atencao"]))


# Etapas do fluxo completo mostradas no Painel (uma coluna por etapa)
ETAPAS_PAINEL = [("Cadastro", "Cadastro"), ("Coleta", "Coleta"), ("Embarque", "Embarque"), ("Desembaraço", "Desemb."),
                 ("Transporte Terrestre", "Transp. terrestre"), ("Inspeção", "Inspeção"), ("Pagamento", "Pagto."),
                 ("Dedução e liberação", "Dedução / liberação"), ("Concluído", "Concluído")]


def progresso_fluxo(r):
    """[(etapa, estado, data)] do fluxo completo do invoice. estado: feito | atual | pendente | na (não se aplica)."""
    sit = r["situacao"]
    pagos = [p["data_pagamento"] for p in r["pagamentos"] if p["tipo"] == "PRINCIPAL"]
    antes_pagamento = ("CRIACAO", "COLETA", "EMBARQUE", "DESEMBARACO", "CHEGADA", "INSPECAO", "PAGAMENTO")
    passos = [("Cadastro", etapa_concluida(r, "CRIACAO"), (r["cadastro_concluido_em"] or "")[:10] or None)]
    datas = {"COLETA": r["data_coleta"], "EMBARQUE": r["eta_real"], "DESEMBARACO": r["data_desembaraco"],
             "CHEGADA": r["data_chegada"]}
    for e in ORDEM_LOGISTICA:
        passos.append((TITULO_ETAPA[e], etapa_concluida(r, e), datas[e]))  # histórico: etapas puladas (v4.51)
    passos.append(("Inspeção", r["tem_desconto"] is not None and bool(r["data_fim_inspecao"]), r["data_fim_inspecao"]))
    passos.append(("Pagamento", sit not in antes_pagamento + ("CANCELADO",), max(pagos) if pagos else None))
    # sem desconto confirmado na inspeção (ou histórico sem desconto), a etapa de dedução/liberação não se aplica
    sem_desconto = not r["desconto"] and (r["tem_desconto"] == 0 or (r["historico"] and sit in ("SWIFT", "CONCLUIDO")))
    passos.append(("Dedução e liberação", None if sem_desconto else (bool(r["desconto"]) and sit in ("SWIFT", "CONCLUIDO")), None))
    passos.append(("Concluído", sit == "CONCLUIDO", None))
    res, achou_atual = [], sit == "CANCELADO"
    for nome, feito, data in passos:
        if feito is None:
            res.append((nome, "na", None))
        elif feito:
            res.append((nome, "feito", data))
        elif not achou_atual:
            achou_atual = True
            res.append((nome, "atual", None))
        else:
            res.append((nome, "pendente", None))
    return res


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
            if p["bloqueado"]:
                tipo += " · bloqueado: aguardando liberação da Engenharia de Processos"
            itens.append(dict(base, categoria="PAGAMENTO", tipo=tipo, data=p["vencimento"], estimado=False,
                              valor=p["pendente"], bloqueado=p["bloqueado"]))
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
        if it.get("bloqueado"):
            it["situacao"] = "Bloqueado (Engenharia)"
        elif it["data"] and it["data"] < hj and it["categoria"] != "CUSTO":
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
        "moeda": f.get("moeda", "") if f.get("moeda", "") in MOEDAS else "",
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
    if not dados["moeda"]:
        erros.append("Selecione a moeda do invoice.")
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
    pcts, tipos, dias_l = f.getlist("cond_pct"), f.getlist("cond_tipo"), f.getlist("cond_dias")
    dias_l += [""] * (len(pcts) - len(dias_l))  # proteção: cada linha deve ter os três campos
    for pct, tipo, dias in zip(pcts, tipos, dias_l):
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
SEM_PENDENCIA = {"exigencias_anvisa", "exigencia_status", "exigencia_acao", "exigencia_prazo"}



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
    # v4.30: o invoice assinado é obrigatório para concluir o cadastro (v4.31: arquivo anexado no sistema;
    # um caminho na rede cadastrado na v4.30 também vale). Não vale para o histórico nem para cadastros já
    # concluídos antes desta regra.
    if r.get("id") and not r.get("historico") and not r.get("cadastro_concluido_em"):
        tem = r.get("invoice_arquivos") or r.get("invoice_docs")
        if r.get("invoice_arquivos") is None:
            db = get_db()
            tem = db.execute("SELECT 1 FROM arquivos WHERE origem = 'invoice' AND origem_id = ?", (r["id"],)).fetchone() \
                or db.execute("SELECT 1 FROM documentos WHERE origem = 'invoice' AND origem_id = ?", (r["id"],)).fetchone()
        if not tem:
            falta.append("Invoice assinado (arquivo anexado)")
    return falta


@app.before_request
def ajuste_v430_cadastro():
    """Uma única vez: cadastros que já estavam completos antes da v4.30 ganham como data de conclusão a data de
    assinatura (o vencimento da parcela antecipada não muda e nenhum e-mail de alteração é gerado)."""
    if config("v430_cadastro") is not None:
        return
    db = get_db()
    for inv in db.execute(SQL_INVOICE + " WHERE i.cadastro_concluido_em IS NULL").fetchall():
        r = calcular(inv)
        if not [f for f in criacao_faltando(r) if not f.startswith("Invoice assinado")]:
            db.execute("UPDATE invoices SET cadastro_concluido_em = ? WHERE id = ?",
                       (r["data_assinatura"] or r["criado_em"][:10], r["id"]))
    salvar_config("v430_cadastro", agora())
    db.commit()


def salvar_invoice_assinado_form(iid):
    """Campo 'Invoice assinado' da tela de cadastro: guarda o arquivo enviado no sistema."""
    # v4.42: até dois arquivos na criação (o primeiro obrigatório para concluir o cadastro, o segundo opcional)
    arquivos = [f for campo in ("invoice_arquivo", "invoice_arquivo2") for f in request.files.getlist(campo)
                if f and f.filename]
    if not arquivos:
        return
    salvos, erros = salvar_arquivos("invoice", iid, arquivos)
    if salvos:
        registrar_evento(iid, "Invoice assinado anexado", ", ".join(salvos))
        get_db().commit()
    for e in erros:
        flash("Invoice assinado: " + e + " Anexe o arquivo na tela do invoice.", "erro")


def registrar_conclusao_cadastro(iid):
    """Guarda a data em que a etapa 1 (cadastro) ficou completa: base do vencimento da parcela antecipada."""
    r = carregar_invoice(iid)
    if not r["cadastro_concluido_em"] and not criacao_faltando(r):
        get_db().execute("UPDATE invoices SET cadastro_concluido_em = ? WHERE id = ?", (agora(), iid))
        registrar_evento(iid, "Cadastro concluído (etapa 1)",
                         "Base do vencimento da parcela antecipada: + " f"{PRAZO_ANTECIPADO_DIAS} dias úteis")
        get_db().commit()
        return True
    return False


def opcionais_faltando(r):
    """Informações não obrigatórias ainda vazias (ficam como pendência no painel)."""
    falta = [rot for campo, rot in OPCIONAIS_CADASTRO if not r[campo]]
    for etapa in ORDEM_LOGISTICA:
        falta += [rot for campo, rot, _t, obrig in CAMPOS_ETAPA[etapa]
                  if not obrig and campo not in SEM_PENDENCIA and not r[campo]]
    return falta


def etapa_faltando(r, etapa):
    """Informações obrigatórias de uma etapa logística que ainda faltam."""
    if r["historico"]:  # processo do passado (carga de histórico): a logística não é controlada
        return []
    falta = [rot for campo, rot, _t, obrig in CAMPOS_ETAPA[etapa] if obrig and not r[campo]]
    if etapa == "EMBARQUE" and not r["banco_debito"]:
        falta.append("Banco de débito (Financeiro)")
    if etapa == "DESEMBARACO" and not tem_duimp(r):
        falta.insert(1, "DUIMP (arquivo anexado)")
    return falta


def tem_duimp(r):
    """v4.43: o Desembaraço só é concluído com a DUIMP anexada (ou caminho cadastrado na v4.27–4.30).
    Invoices que já tinham passado do desembaraço antes desta regra ficam dispensados."""
    if r["duimp_dispensada"]:
        return True
    chaves = r.keys()
    if "duimp_arquivos" in chaves:
        return bool(r["duimp_arquivos"] or r["duimp_docs"])
    db = get_db()
    return bool(db.execute("SELECT 1 FROM arquivos WHERE origem = 'duimp' AND origem_id = ?", (r["id"],)).fetchone()
                or db.execute("SELECT 1 FROM documentos WHERE origem = 'duimp' AND origem_id = ?", (r["id"],)).fetchone())


def avisar_cadastro_pendente(iid):
    registrar_conclusao_cadastro(iid)
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
    for x in r["deducoes_avulsas"]:
        cabecalho.append((f"Desconto avulso #{x['avulso_id']} deduzido", x["valor"]))
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
                did, v = id_desconto(chave[4:]), parse_num(valor)
                if did is None or v is None or v <= 0:
                    continue
                if did not in disponiveis:
                    erros.append("Um dos descontos escolhidos não está mais disponível.")
                elif v > disponiveis[did]["saldo"] + TOLERANCIA:
                    erros.append(f"A dedução do desconto {disponiveis[did]['invoice']} é maior que o saldo"
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
                origem = disponiveis[did].get("justificativa") if disponiveis[did].get("avulso") else disponiveis[did]["invoice"]
                gravar_deducao(did, iid, dados["numero"], v, dados["moeda"], origem)
            db.commit()
            flash("Invoice criado." + (f" {len(deducoes)} desconto(s) deduzido(s)." if deducoes else ""), "ok")
            salvar_invoice_assinado_form(iid)
            avisar_cadastro_pendente(iid)
            return redirect(url_for("invoice_detalhe", id=iid))
        for e in erros:
            flash(e, "erro")
        return render_template("invoice_form.html", d=dados, itens=itens, fornecedores=fornecedores, editando=False,
                               deducoes_escolhidas={str(k): v for k, v in deducoes})
    return render_template("invoice_form.html", d={"moeda": "", "condicao": [{"percentual": 100, "tipo": "PRAZO"}]},
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
    chave_travada = bool(r["desconto"] or r["deducoes_recebidas"] or r["deducoes_avulsas"])
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
            salvar_invoice_assinado_form(id)
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
    r["relatorios"] = relatorios_desconto(r["desconto"]["id"]) if r["desconto"] else []
    r["insp_resumo"] = db.execute(
        "SELECT grupo, COUNT(*) AS eventos, SUM(quantidade) AS pecas,"
        " SUM(CASE WHEN categoria IN ('C', 'D') THEN quantidade ELSE 0 END) AS cd, MAX(substr(report_date, 1, 10))"
        " AS report_date FROM inspecao_eventos"
        " WHERE invoice_id = ? GROUP BY grupo", (id,)).fetchone()
    fator = fator_importacao(r)
    r["relatorios_revisao"] = arquivos_de("revisao", [v["id"] for v in revisoes])
    disponiveis = descontos_disponiveis(r["fornecedor_id"], r["moeda"], excluir_invoice_id=id)
    nomes = {u["id"]: u["nome"] for u in db.execute("SELECT id, nome FROM usuarios")}
    return render_template("invoice_detalhe.html", r=r, eventos=eventos, revisoes=revisoes, disponiveis=disponiveis, fator=fator,
                           nomes=nomes, hoje=hoje())


# ---------------------------------------------------------------------------
# Etapas logísticas e banco de débito
# ---------------------------------------------------------------------------
def aplicar_etapa(r, etapa, form):
    """Grava os campos de uma etapa logística vindos do formulário. Retorna (erro, novos valores).
    Usado pela tela do invoice e pela aba Importação. Faz commit quando há mudança."""
    id = r["id"]
    novos, mudancas = {}, []
    for campo, rotulo, tipo, _obrig in CAMPOS_ETAPA[etapa]:
        valor = form.get(campo, "").strip()
        if tipo == "data":
            if valor and not data_valida(valor):
                return f"{TITULO_ETAPA[etapa]} · {rotulo}: data inválida.", {}
            valor = data_valida(valor)
        elif tipo == "canal":
            valor = valor if valor in CORES_CANAL else None
        elif tipo == "exigencia":
            valor = valor if valor in SITUACOES_EXIGENCIA else None
        elif tipo == "flag":
            valor = 1 if valor else None
        else:
            valor = valor or None
        novos[campo] = valor
        if valor != r[campo]:
            mudancas.append(f"{rotulo}: " + (fmt_data(valor) if tipo == 'data' else
                                             ('sim' if valor else 'não') if tipo == 'flag' else (valor or '—')))
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
    return None, novos


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
    erro, novos = aplicar_etapa(r, etapa, request.form)
    if erro:
        flash(erro, "erro")
        return redirect(url_for("invoice_detalhe", id=id) + f"#etapa-{etapa}")
    # salvar sempre funciona; a etapa só é concluída com todas as informações obrigatórias
    falta = etapa_faltando(dict(r, **novos), etapa)
    if falta:
        flash(f"Etapa {TITULO_ETAPA[etapa]} salva. Para concluir a etapa, falta: " + ", ".join(falta) + ".", "aviso-box")
    else:
        flash(f"Etapa {TITULO_ETAPA[etapa]} salva e concluída.", "ok")
    return redirect(url_for("invoice_detalhe", id=id) + f"#etapa-{etapa}")


@app.route("/invoices/<int:id>/duimp-banco", methods=["POST"])
@login_obrigatorio
def duimp_banco(id):
    """O Financeiro marca (ou desmarca) que a DUIMP foi enviada ao banco. Não trava o fluxo; sem a marcação
    fica uma pendência do Financeiro."""
    if not pode("financeiro"):
        abort(403)
    r = carregar_invoice(id)
    marcar = bool(request.form.get("duimp_banco"))
    db = get_db()
    if marcar and not r["duimp_banco_em"]:
        db.execute("UPDATE invoices SET duimp_banco_em = ?, duimp_banco_por = ? WHERE id = ?", (agora(), g.usuario["id"], id))
        registrar_evento(id, "DUIMP enviada ao banco (Financeiro)")
        flash("Marcado: DUIMP enviada ao banco.", "ok")
    elif not marcar and r["duimp_banco_em"]:
        db.execute("UPDATE invoices SET duimp_banco_em = NULL, duimp_banco_por = NULL WHERE id = ?", (id,))
        registrar_evento(id, "Marcação \"DUIMP enviada ao banco\" desfeita (Financeiro)")
        flash("Marcação desfeita: DUIMP não enviada ao banco.", "ok")
    db.commit()
    return redirect(voltar_seguro(url_for("invoice_detalhe", id=id) + "#etapa-DESEMBARACO"))


# ---------------------------------------------------------------------------
# Aba Importação: invoices nas etapas 1 a 5 (cadastro até o transporte terrestre), com edição rápida
# ---------------------------------------------------------------------------
ETAPAS_IMPORTACAO = ["CRIACAO"] + ORDEM_LOGISTICA


def no_radar_importacao(r):
    """Invoices da aba Importação: etapas 1 a 5, fora do histórico, ainda sem a NF liberada (v4.40) e sem a chegada na fábrica (v4.50)."""
    # v4.50: também sai quando a data real de chegada na fábrica já foi preenchida
    return (r["situacao"] in ETAPAS_IMPORTACAO and not r["historico"] and not r["nf_liberada"]
            and not r["data_chegada"])


@app.route("/importacao")
@login_obrigatorio
def importacao():
    lista = [r for r in todos_invoices() if no_radar_importacao(r)]
    for r in lista:
        r["falta"] = criacao_faltando(r) if r["situacao"] == "CRIACAO" else etapa_faltando(r, r["situacao"])
        r["ultima_info"] = ultima_informacao(r)
        r["resumo_imp"] = resumo_importacao(r)
    ordem = {e: i for i, e in enumerate(ETAPAS_IMPORTACAO)}
    lista.sort(key=lambda r: (ordem[r["situacao"]], r["data_prog_chegada"] or r["eta_planejado"] or "9999", r["id"]))
    contagem = {e: sum(1 for r in lista if r["situacao"] == e) for e in ETAPAS_IMPORTACAO}
    total = len(lista)
    grupos_info = info_grupos(lista)
    etapa = request.args.get("etapa")
    if etapa in ETAPAS_IMPORTACAO:
        lista = [r for r in lista if r["situacao"] == etapa]
    busca = (request.args.get("q") or "").strip().lower()
    if busca:
        lista = [r for r in lista if busca in r["numero"].lower() or busca in (r["fornecedor"] or "").lower()
                 or busca == (r["grupo_consolidacao"] or "").strip().lower()]
    return render_template("importacao.html", lista=lista, blocos=agrupar_consolidacao(lista), grupos_info=grupos_info,
                           CAMPOS_GRUPO=CAMPOS_GRUPO, chave_grupo=chave_grupo, contagem=contagem, total=total, etapa=etapa, busca=busca,
                           ETAPAS_IMPORTACAO=ETAPAS_IMPORTACAO, aberto=request.args.get("aberto", type=int),
                           editar=pode("importacao"))


def resumo_importacao(r):
    """v4.65: status do processo (aba Importação) → (título, detalhe, cor). v4.66: mesmas regras dos relatórios."""
    if r["situacao"] == "CRIACAO":
        falta = criacao_faltando(r)
        return ("Cadastro incompleto", "falta " + ", ".join(falta[:2]).lower() + ("…" if len(falta) > 2 else ""), "neutro")
    _k, titulo, detalhe, cor = rimp.status(processo_de(r))
    return titulo, detalhe, cor


def processo_de(r, extra=None):
    """Invoice → processo no formato dos relatórios da importação (relatorios_imp)."""
    e = extra or {}
    return {
        "id": r.get("id"), "fornecedor": r.get("fornecedor"), "pais": e.get("pais"), "invoice": r.get("numero"),
        "valor": r.get("valor_invoice"), "moeda": r.get("moeda"), "agente": r.get("agente_carga"), "despachante": r.get("despachante"),
        "consolidacao": r.get("grupo_consolidacao"), "duimp": e.get("duimp"),
        "coleta_prevista": r.get("data_prog_coleta"), "data_coleta": r.get("data_coleta"), "etd_previsto": r.get("etd_planejado"),
        "etd_real": r.get("etd_real"), "hawb": r.get("data_hawb"), "eta_prevista": r.get("eta_planejado"), "eta_real": r.get("eta_real"),
        "data_duimp": r.get("data_registro_br"), "data_parametrizacao": r.get("data_parametrizacao"), "canal": r.get("cor_canal"),
        "exig_descricao": r.get("exigencias_anvisa"), "exig_acao": r.get("exigencia_acao"), "exig_status": r.get("exigencia_status"),
        "exig_prazo": r.get("exigencia_prazo"), "data_desembaraco": r.get("data_desembaraco"),
        "entrega_prevista": r.get("data_prog_chegada"), "entrega_real": r.get("data_chegada"), "obs": r.get("obs"),
        "criado_em": r.get("criado_em"), "ultima_atualizacao": e.get("ultima"),
        "itens": [{"codigo": x["codigo"], "descricao": x["item"], "quantidade": x["quantidade"]} for x in r.get("itens", [])],
    }


def processos_importacao(invoices=None):
    """Processos dos relatórios da importação: invoices não cancelados e fora da carga de histórico, com o status
    recalculado agora (tela, Excel e PDF usam sempre esta mesma lista)."""
    db = get_db()
    invoices = invoices if invoices is not None else todos_invoices()
    paises = {r["id"]: r["cidade"] for r in db.execute("SELECT id, cidade FROM fornecedores")}
    duimps = {r["invoice_id"]: r["numero"] for r in db.execute("SELECT invoice_id, numero FROM duimp_dados WHERE numero IS NOT NULL")}
    ultimas = {r[0]: r[1] for r in db.execute("SELECT invoice_id, MAX(criado_em) FROM eventos GROUP BY invoice_id")}
    res = [processo_de(r, {"pais": paises.get(r["fornecedor_id"]), "duimp": duimps.get(r["id"]),
                           "ultima": ultimas.get(r["id"])})
           for r in invoices if not r["cancelado_em"] and not r["historico"]]
    return rimp.com_status(res)


def ultima_informacao(r):
    """Última informação preenchida no cronograma do fluxo (cadastro → coleta → embarque → desembaraço →
    transporte terrestre), em texto curto para a lista da aba Importação."""
    ultima = None
    if r["cadastro_concluido_em"]:
        ultima = f"Cadastro concluído {fmt_data(r['cadastro_concluido_em'][:10])}"
    elif r["criado_em"]:
        ultima = f"Cadastro iniciado {fmt_data(r['criado_em'][:10])}"
    for etapa in ORDEM_LOGISTICA:
        for campo, rotulo, tipo, _obrig in CAMPOS_ETAPA[etapa]:
            v = r[campo]
            if not v or tipo == "texto" or campo == "exigencia_prazo":
                continue
            if tipo == "data":
                ultima = f"{rotulo} {fmt_data(v)}"
            elif tipo == "flag":
                ultima = rotulo
            elif tipo == "canal":
                ultima = f"Canal {v}"
            elif tipo == "exigencia":
                ultima = f"Exigência {SITUACOES_EXIGENCIA.get(v, v).lower()}"
            else:
                ultima = f"{rotulo} {v}"
    return ultima


def chave_grupo(valor):
    return " ".join((valor or "").split()).lower()


def agrupar_consolidacao(lista):
    """Blocos [(grupo ou None, [invoices])]: invoices do mesmo grupo de consolidação ficam juntos, no lugar do
    primeiro deles na ordem da lista (a ordem original é mantida dentro do grupo)."""
    membros = {}
    for r in lista:
        k = chave_grupo(r["grupo_consolidacao"])
        if k:
            membros.setdefault(k, []).append(r)
    blocos, vistos = [], set()
    for r in lista:
        k = chave_grupo(r["grupo_consolidacao"])
        if not k:
            blocos.append((None, [r]))
        elif k not in vistos:
            vistos.add(k)
            blocos.append((r["grupo_consolidacao"].strip(), membros[k]))
    return blocos


def grupos_consolidacao():
    """Grupos de consolidação já usados, do maior para o menor (números em ordem numérica), com a quantidade de
    invoices e os fornecedores de cada um. Usado na lista do campo no cadastro do invoice."""
    grupos = {}
    for row in get_db().execute(
            "SELECT i.grupo_consolidacao AS g, f.nome AS fornecedor FROM invoices i"
            " LEFT JOIN fornecedores f ON f.id = i.fornecedor_id"
            " WHERE i.grupo_consolidacao IS NOT NULL AND trim(i.grupo_consolidacao) <> '' AND i.cancelado_em IS NULL"):
        k = chave_grupo(row["g"])
        item = grupos.setdefault(k, {"valor": " ".join(row["g"].split()), "qtd": 0, "fornecedores": []})
        item["qtd"] += 1
        if row["fornecedor"] and row["fornecedor"] not in item["fornecedores"]:
            item["fornecedores"].append(row["fornecedor"])

    def ordem(item):
        v = item["valor"].replace(",", ".")
        try:
            return (1, float(v), "")
        except ValueError:
            return (0, 0.0, item["valor"].lower())
    return sorted(grupos.values(), key=ordem, reverse=True)


app.jinja_env.globals["grupos_consolidacao"] = grupos_consolidacao


# Coleta e Embarque são iguais para todos os invoices de um grupo de consolidação (o banco de débito não entra)
ETAPAS_GRUPO = ["COLETA", "EMBARQUE"]
CAMPOS_GRUPO = [(e, campo, rotulo) for e in ETAPAS_GRUPO for campo, rotulo, _t, _o in CAMPOS_ETAPA[e]]


def membros_grupo(lista, chave):
    return [r for r in lista if chave and chave_grupo(r["grupo_consolidacao"]) == chave]


def info_grupos(lista):
    """{chave do grupo: {qtd, valores comuns de Coleta/Embarque (None se diferentes), diferentes}} considerando todos
    os invoices do grupo em andamento (etapas 1 a 5), mesmo os escondidos por filtro/busca."""
    info = {}
    for k in {chave_grupo(r["grupo_consolidacao"]) for r in lista} - {""}:
        ms = membros_grupo(lista, k)
        recebem = [m for m in ms if m["situacao"] != "CRIACAO"] or ms  # cadastro incompleto não recebe as datas
        valores, diferentes = {}, set()
        for _e, campo, _rot in CAMPOS_GRUPO:
            vs = {m[campo] for m in recebem}
            valores[campo] = next(iter(vs)) if len(vs) == 1 else None
            if len(vs) > 1:
                diferentes.add(campo)
        info[k] = {"qtd": len(ms), "valores": valores, "diferentes": diferentes,
                   "incompletos": [m["numero"] for m in ms if m["situacao"] == "CRIACAO"]}
    return info


@app.route("/importacao/grupo", methods=["POST"])
@login_obrigatorio
def importacao_grupo():
    """Preenche Coleta e Embarque de uma vez para todos os invoices do grupo de consolidação em andamento.
    Só os campos preenchidos no formulário são aplicados (os vazios mantêm o que cada invoice já tem)."""
    if not pode("importacao"):
        abort(403)
    chave = chave_grupo(request.form.get("grupo"))
    volta = redirect(url_for("importacao") + "#grupo-" + re.sub(r"[^\w-]", "_", chave))
    ativos = [r for r in todos_invoices() if no_radar_importacao(r)]
    membros = membros_grupo(ativos, chave)
    if not membros:
        flash("Nenhum invoice em andamento neste grupo de consolidação.", "erro")
        return volta
    informados = {}
    for _e, campo, rotulo in CAMPOS_GRUPO:
        v = request.form.get(campo, "").strip()
        if v:
            if not data_valida(v):
                flash(f"{rotulo}: data inválida. Nada foi salvo.", "erro")
                return volta
            informados[campo] = data_valida(v)
    if not informados:
        flash("Preencha ao menos uma data para aplicar ao grupo.", "erro")
        return volta
    atualizados, pulados = [], []
    for r in membros:
        if criacao_faltando(r):
            pulados.append(r["numero"])
            continue
        mudou = False
        for etapa in ETAPAS_GRUPO:
            campos = {c for c, _r, _t, _o in CAMPOS_ETAPA[etapa]}
            if not campos & informados.keys():
                continue
            form = {c: (informados.get(c) or r[c] or "") for c in campos}
            if any(form[c] != (r[c] or "") for c in campos):
                mudou = True
            erro, _novos = aplicar_etapa(r, etapa, form)
            if erro:
                flash(f"{r['numero']}: {erro}", "erro")
        if mudou:
            registrar_evento(r["id"], "Coleta/Embarque preenchidos pelo grupo de consolidação",
                             f"Grupo {r['grupo_consolidacao'].strip()}")
            get_db().commit()
            atualizados.append(r["numero"])
    grupo = membros[0]["grupo_consolidacao"].strip()
    if atualizados:
        flash(f"Grupo {grupo}: Coleta/Embarque atualizados em {len(atualizados)} invoice(s): {', '.join(atualizados)}.", "ok")
    else:
        flash(f"Grupo {grupo}: nenhuma alteração (os invoices já tinham essas datas).", "ok")
    if pulados:
        flash(f"Não atualizados por estarem com o cadastro incompleto: {', '.join(pulados)}.", "erro")
    return volta


@app.route("/importacao/<int:id>", methods=["POST"])
@login_obrigatorio
def importacao_salvar(id):
    """Salva de uma vez os campos das etapas 2 a 5 de um invoice (mesmas regras da tela do invoice)."""
    if not pode("importacao"):
        abort(403)
    r = carregar_invoice(id)
    volta = redirect(url_for("importacao", aberto=id) + f"#inv-{id}")
    if r["cancelado_em"]:
        abort(403)
    falta_cadastro = criacao_faltando(r)
    if falta_cadastro:
        flash(f"{r['numero']}: complete o cadastro antes da logística. Falta: " + ", ".join(falta_cadastro) + ".", "erro")
        return volta
    for etapa in ORDEM_LOGISTICA:
        erro, _novos = aplicar_etapa(r, etapa, request.form)
        if erro:
            flash(f"{r['numero']}: {erro} Nada desta etapa foi salvo.", "erro")
    r = carregar_invoice(id)
    if r["data_chegada"] and not r["nf_liberada"] and r["situacao"] in ORDEM_LOGISTICA:
        falta = etapa_faltando(r, r["situacao"])
        flash(f"{r['numero']} salvo com a chegada na fábrica: saiu desta lista. Etapa atual: {TITULO_ETAPA[r['situacao']]}"
              + (f" (falta: {', '.join(falta)} — complete na tela do invoice)." if falta else "."), "ok")
    elif r["nf_liberada"] and r["situacao"] in ORDEM_LOGISTICA:
        falta = etapa_faltando(r, r["situacao"])
        flash(f"{r['numero']} salvo com a NF Liberada: saiu desta lista. Etapa atual: {TITULO_ETAPA[r['situacao']]}"
              + (f" (falta: {', '.join(falta)} — complete na tela do invoice)." if falta else "."), "ok")
    elif r["situacao"] in ORDEM_LOGISTICA:
        falta = etapa_faltando(r, r["situacao"])
        flash(f"{r['numero']} salvo. Etapa atual: {TITULO_ETAPA[r['situacao']]}"
              + (f" (falta: {', '.join(falta)})." if falta else "."), "ok")
    else:
        flash(f"{r['numero']} salvo. Transporte Terrestre concluído: o invoice seguiu para "
              f"{SITUACOES.get(r['situacao'], (r['situacao'],))[0]} e saiu desta lista.", "ok")
    return volta


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
    did = id_desconto(request.form.get("desconto_id"))
    valor = parse_num(request.form.get("valor"))
    disponiveis = {d["id"]: d for d in descontos_disponiveis(r["fornecedor_id"], r["moeda"], excluir_invoice_id=id)}
    erro = None
    if r["cancelado_em"]:
        erro = "Invoice cancelado."
    elif any(p["tipo"] == "PRINCIPAL" for p in r["pagamentos"]):
        erro = "Este invoice já tem pagamento registrado: não é possível incluir deduções."
    elif did not in disponiveis:
        erro = "Selecione um desconto disponível."
    elif any(x["desconto_id"] == did for x in r["deducoes_recebidas"]) \
            or any(f"a{x['avulso_id']}" == did for x in r["deducoes_avulsas"]):
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
    origem = disponiveis[did].get("justificativa") if disponiveis[did].get("avulso") else disponiveis[did]["invoice"]
    gravar_deducao(did, id, r["numero"], valor, r["moeda"], origem)
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


@app.route("/deducoes-avulsas/<int:xid>/<acao>", methods=["POST"])
@login_obrigatorio
def deducao_avulsa_acao(xid, acao):
    """A Importação corrige ou remove a dedução de um desconto avulso enquanto o invoice não chegou nem foi pago."""
    if not pode("importacao"):
        abort(403)
    db = get_db()
    x = db.execute("SELECT x.*, a.valor AS valor_avulso, a.encerrado_em FROM deducoes_avulsas x"
                   " JOIN descontos_avulsos a ON a.id = x.avulso_id WHERE x.id = ?", (xid,)).fetchone()
    if x is None or acao not in ("editar", "remover"):
        abort(404)
    destino = carregar_invoice(x["invoice_id"])
    volta = redirect(url_for("invoice_detalhe", id=destino["id"]) + "#deducoes")
    if destino["data_chegada"] or any(p["tipo"] == "PRINCIPAL" for p in destino["pagamentos"]):
        flash("O invoice já chegou ou tem pagamento: a dedução não pode mais ser alterada.", "erro")
        return volta
    if acao == "remover":
        db.execute("DELETE FROM deducoes_avulsas WHERE id = ?", (xid,))
        registrar_evento(destino["id"], f"Dedução removida: {fmt_moeda(x['valor'], destino['moeda'])} do desconto "
                                        f"avulso #{x['avulso_id']}")
        db.commit()
        flash("Dedução removida." + (" O desconto avulso está encerrado: o valor não volta a ficar disponível."
                                     if x["encerrado_em"] else ""), "ok")
        return volta
    valor = parse_num(request.form.get("valor"))
    outras = db.execute("SELECT COALESCE(SUM(valor), 0) FROM deducoes_avulsas WHERE avulso_id = ? AND id <> ?",
                        (x["avulso_id"], xid)).fetchone()[0]
    saldo = round(x["valor_avulso"] - outras, 2) if not x["encerrado_em"] else x["valor"]
    if valor is None or valor <= 0:
        flash("Informe o valor deduzido (para retirar a dedução, use Remover).", "erro")
    elif valor > saldo + TOLERANCIA:
        flash(f"O valor é maior que o saldo do desconto avulso ({fmt_moeda(saldo, destino['moeda'])}).", "erro")
    elif valor > destino["itens_total"] - (destino["deducoes_aplicadas"] - x["valor"]) + TOLERANCIA:
        flash("O valor é maior que o valor restante do invoice.", "erro")
    else:
        valor = round(valor, 2)
        db.execute("UPDATE deducoes_avulsas SET valor = ? WHERE id = ?", (valor, xid))
        registrar_evento(destino["id"], f"Dedução do desconto avulso #{x['avulso_id']} corrigida",
                         f"{fmt_moeda(x['valor'], destino['moeda'])} → {fmt_moeda(valor, destino['moeda'])}")
        db.commit()
        flash("Dedução corrigida.", "ok")
    return volta


@app.route("/descontos/avulsos", methods=["POST"])
@somente_admin
def desconto_avulso_novo():
    """A Engenharia de Processos cadastra um desconto avulso para um fornecedor (sem invoice), com justificativa."""
    f = request.form
    fid = f.get("fornecedor_id", type=int)
    moeda = f.get("moeda", "")
    valor = parse_num(f.get("valor"))
    justificativa = " ".join(f.get("justificativa", "").split())[:1000]
    db = get_db()
    erro = None
    if not fid or not db.execute("SELECT 1 FROM fornecedores WHERE id = ?", (fid,)).fetchone():
        erro = "Selecione o fornecedor."
    elif moeda not in MOEDAS:
        erro = "Selecione a moeda."
    elif valor is None or valor <= 0:
        erro = "Informe o valor do desconto."
    elif len(justificativa) < 10:
        erro = "Escreva a justificativa: do que se trata o desconto (pelo menos 10 caracteres)."
    if erro:
        flash(erro, "erro")
        return redirect(url_for("descontos", f="avulsos") + "#avulsos")
    db.execute("INSERT INTO descontos_avulsos (fornecedor_id, moeda, valor, justificativa, criado_por, criado_em)"
               " VALUES (?, ?, ?, ?, ?, ?)", (fid, moeda, round(valor, 2), justificativa, g.usuario["id"], agora()))
    db.commit()
    flash(f"Desconto avulso de {fmt_moeda(valor, moeda)} cadastrado. Ele aparecerá para a Importação deduzir no "
          "próximo invoice deste fornecedor (mesma moeda).", "ok")
    return redirect(url_for("descontos", f="avulsos") + "#avulsos")


@app.route("/descontos/avulsos/<int:aid>/encerrar", methods=["POST"])
@somente_admin
def desconto_avulso_encerrar(aid):
    """Cancela o saldo ainda não deduzido (as deduções já feitas continuam no histórico)."""
    db = get_db()
    a = db.execute("SELECT * FROM descontos_avulsos WHERE id = ?", (aid,)).fetchone()
    if a is None:
        abort(404)
    motivo = " ".join(request.form.get("motivo", "").split())[:300]
    if a["encerrado_em"]:
        flash("Este desconto avulso já está encerrado.", "erro")
    elif not motivo:
        flash("Informe o motivo para encerrar o desconto avulso.", "erro")
    else:
        db.execute("UPDATE descontos_avulsos SET encerrado_em = ?, encerrado_por = ?, encerrado_motivo = ? WHERE id = ?",
                   (agora(), g.usuario["id"], motivo, aid))
        db.commit()
        flash(f"Desconto avulso #{aid} encerrado: o saldo restante não aparece mais para dedução.", "ok")
    return redirect(url_for("descontos", f="avulsos") + "#avulsos")


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
    if not r["data_chegada"] and not r["historico"]:  # histórico: etapas até a chegada são puladas (v4.51)
        flash("A inspeção é registrada depois da chegada do invoice na fábrica.", "erro")
        return volta
    if not data_insp:
        flash("Informe a data do fim da inspeção.", "erro")
        return volta
    db = get_db()
    if tem not in ("0", "1"):  # v4.45: só a data; o resultado (com ou sem desconto) vem depois, com o relatório
        if d:
            flash("Este invoice já tem desconto cadastrado: a inspeção não pode voltar para 'a definir'. "
                  "Para alterar o valor, use a revalidação.", "erro")
            return volta
        db.execute("UPDATE invoices SET data_fim_inspecao = ?, tem_desconto = NULL WHERE id = ?", (data_insp, id))
        registrar_evento(id, "Fim da inspeção registrado: relatório de inspeção pendente",
                         f"Fim da inspeção: {fmt_data(data_insp)}")
        db.commit()
        flash("Fim da inspeção registrado. Falta definir se há desconto (relatório de inspeção pendente).", "ok")
        return volta
    if tem == "0":
        if d:
            if r["deducoes_do_desconto"] or r["liberado"] or db.execute(
                    "SELECT 1 FROM revisoes_desconto WHERE desconto_id = ?", (d["id"],)).fetchone():
                flash("O desconto já tem deduções, liberações ou revalidações: para zerar, use a revalidação.", "erro")
                return volta
            rel_apagar = excluir_arquivos_de("relatorio", d["id"])
            db.execute("DELETE FROM descontos WHERE id = ?", (d["id"],))
            registrar_evento(id, "Desconto removido")
            g.remover_depois = rel_apagar
        db.execute("UPDATE invoices SET data_fim_inspecao = ?, tem_desconto = 0 WHERE id = ?", (data_insp, id))
        registrar_evento(id, "Inspeção registrada: sem desconto", f"Fim da inspeção: {fmt_data(data_insp)}")
        db.commit()
        remover_arquivos(g.pop("remover_depois", []))
        flash("Inspeção registrada sem desconto. O invoice segue para pagamento integral.", "ok")
        return volta
    data_rel = data_valida(f.get("data_relatorio"))
    # v4.46: sem escolha, o relatório ainda não foi enviado ao fornecedor
    status = f.get("status") if f.get("status") in STATUS_DESCONTO else "PENDENTE"
    # v4.72: o relatório é feito pela plataforma (versões do desconto), que guarda todas as informações: não se anexa
    # mais o Excel do relatório no invoice
    obs = f.get("obs", "").strip() or None
    if not data_rel:
        flash("Informe a data do relatório.", "erro")
        return volta
    if d:  # desconto já existe: valor só muda pela revalidação
        db.execute("UPDATE descontos SET data_relatorio = ?, obs = ? WHERE id = ?", (data_rel, obs, d["id"]))
        db.execute("UPDATE invoices SET data_fim_inspecao = ?, tem_desconto = 1 WHERE id = ?", (data_insp, id))
        if status != d["status"] and status != "APROVADO" and r["deducoes_do_desconto"]:
            flash("O desconto já foi deduzido em outro invoice: a aprovação do fornecedor não pode ser desfeita.", "erro")
            return volta
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
    did = db.execute("INSERT INTO descontos (invoice_id, valor_inicial, valor_final, valor_segurado, data_relatorio, status,"
                     " aprovado_em, aprovado_por, obs, criado_por, criado_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (id, valor, valor, segurado, data_rel, status, agora() if status == "APROVADO" else None,
                      g.usuario["id"] if status == "APROVADO" else None, obs, g.usuario["id"], agora())).lastrowid
    db.execute("UPDATE invoices SET data_fim_inspecao = ?, tem_desconto = 1 WHERE id = ?", (data_insp, id))
    detalhe = f"Valor: {fmt_moeda(valor, r['moeda'])} · segurado: {fmt_moeda(segurado, r['moeda'])}"
    if segurado + TOLERANCIA < valor:
        detalhe += " (o invoice já estava pago em parte: o restante será apenas deduzido no próximo invoice)"
    registrar_evento(id, f"Inspeção registrada com desconto · {STATUS_DESCONTO[status]}",
                     detalhe)
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
    if status != "APROVADO" and r["deducoes_do_desconto"]:
        flash("O desconto já foi deduzido em outro invoice: a aprovação do fornecedor não pode ser desfeita.", "erro")
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
    vid = db.execute("INSERT INTO revisoes_desconto (desconto_id, valor_anterior, valor_novo, motivo, usuario_id, criado_em)"
                     " VALUES (?, ?, ?, ?, ?, ?)", (did, d["valor_final"], novo, motivo, g.usuario["id"], agora())).lastrowid
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
    arquivos_swift = [a for a in request.files.getlist("swift_arquivo") if a and a.filename]
    caminho_swift = normalizar_caminho(f.get("swift_caminho"))
    banco = f.get("banco", "").strip() or r["banco_debito"]
    cotacao = parse_num(f.get("cotacao")) if r["moeda"] != "BRL" else None
    limite = {"PRINCIPAL": r["principal_liberado_pendente"] + r["segurado_pagavel"],
              "LIBERACAO": r["liberacao_disponivel"]}.get(tipo)
    erro = None
    if r["cancelado_em"]:
        erro = "Invoice cancelado."
    elif limite is None:
        erro = "Selecione o tipo de pagamento."
    elif not data_pg:
        erro = "Informe a data do pagamento."
    elif valor is None or valor <= 0:
        erro = "Informe o valor pago."
    elif cotacao is not None and cotacao <= 0:
        erro = "Cotação inválida: informe o valor em R$ por 1 unidade da moeda (ou deixe em branco e informe depois)."
    elif valor > limite + TOLERANCIA:
        erro = (f"O valor é maior que o disponível para {TIPOS_PAGAMENTO[tipo].lower()} "
                f"({fmt_moeda(limite, r['moeda'])}).")
        if tipo == "PRINCIPAL" and r["principal_bloqueado"] > TOLERANCIA:
            erro += (f" As parcelas após a HAWB ({fmt_moeda(r['principal_bloqueado'], r['moeda'])}) aguardam a "
                     "liberação do pagamento pela Engenharia de Processos.")
    elif caminho_swift and not RE_CAMINHO_REDE.match(caminho_swift):
        erro = ("Caminho do Swift inválido: use o caminho completo na pasta da rede, por exemplo "
                "\\\\servidor\\Financeiro\\Swift\\arquivo.pdf (ou deixe em branco e cadastre depois).")
    if erro:
        flash(erro, "erro")
        return volta
    db = get_db()
    valor = round(valor, 2)
    cotacao = round(cotacao, 6) if cotacao else None
    # parte do valor segurado paga junto (o desconto continua para dedução; nada fica retido)
    seg_incl = round(valor - r["principal_liberado_pendente"], 2) if tipo == "PRINCIPAL" else 0
    seg_incl = seg_incl if seg_incl > TOLERANCIA else 0
    if seg_incl:
        db.execute("UPDATE descontos SET valor_segurado = ? WHERE id = ?",
                   (round(r["segurado"] - seg_incl, 2), r["desconto"]["id"]))
    pid = db.execute("INSERT INTO pagamentos (invoice_id, tipo, valor, data_pagamento, banco, swift_url, obs,"
                     " registrado_por, registrado_em, cotacao) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                     (id, tipo, valor, data_pg, banco, f.get("obs", "").strip() or None, g.usuario["id"],
                      agora(), cotacao)).lastrowid
    if seg_incl:
        db.execute("UPDATE pagamentos SET segurado_incluido = ? WHERE id = ?", (seg_incl, pid))
        registrar_evento(id, f"Valor segurado pago junto: {fmt_moeda(seg_incl, r['moeda'])} (não ficou retido)",
                         f"O desconto de {fmt_moeda(r['desconto']['valor_final'], r['moeda'])} continua para dedução "
                         "no próximo invoice; não haverá liberação deste valor.")
    salvos, erros = salvar_arquivos("pagamento", pid, arquivos_swift)
    if caminho_swift:
        db.execute("INSERT INTO documentos (origem, origem_id, caminho, status, cadastrado_por, cadastrado_em)"
                   " VALUES ('pagamento', ?, ?, 'NAO_VERIFICADO', ?, ?)", (pid, caminho_swift, g.usuario["id"], agora()))
        salvos.append(caminho_swift)
    registrar_evento(id, f"{TIPOS_PAGAMENTO[tipo]}: {fmt_moeda(valor, r['moeda'])} em {fmt_data(data_pg)}",
                     (f"Swift: {', '.join(salvos)}" if salvos else "Swift pendente")
                     + (f" · banco: {banco}" if banco else "")
                     + (f" · cotação: R$ {fmt_num(cotacao, 4)} ({fmt_moeda(round(valor * cotacao, 2), 'BRL')})"
                        if cotacao else ""))
    db.commit()
    flash("Pagamento registrado." + ("" if salvos else " O Swift ficou pendente: anexe o arquivo na tabela de pagamentos."), "ok")
    for e in erros:
        flash(e, "erro")
    return volta


@app.route("/pagamentos/<int:pid>/swift", methods=["POST"])
@login_obrigatorio
def pagamento_swift(pid):
    """O Financeiro anexa o arquivo do Swift a um pagamento."""
    if not pode("financeiro"):
        abort(403)
    db = get_db()
    p = db.execute("SELECT * FROM pagamentos WHERE id = ?", (pid,)).fetchone()
    if p is None:
        abort(404)
    arquivos = [a for a in request.files.getlist("arquivo") if a and a.filename]
    if not arquivos:
        flash("Escolha o arquivo do Swift.", "erro")
    else:
        salvos, erros = salvar_arquivos("pagamento", pid, arquivos)
        if salvos:
            registrar_evento(p["invoice_id"], f"Swift anexado ao pagamento de {fmt_data(p['data_pagamento'])}",
                             ", ".join(salvos))
            db.commit()
            flash("Swift anexado: " + ", ".join(salvos) + ".", "ok")
        for e in erros:
            flash(e, "erro")
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
    banco = f.get("banco", "").strip() or None
    obs = f.get("obs", "").strip() or None
    cotacao = parse_num(f.get("cotacao")) if r["moeda"] != "BRL" else None
    cotacao = round(cotacao, 6) if cotacao and cotacao > 0 else None
    # limite: o que ainda pode ser pago desse tipo, devolvendo o valor atual deste pagamento
    si = p["segurado_incluido"] or 0
    base_pend = r["principal_liberado_pendente"] + p["valor"] - si  # pendente sem este pagamento (segurado devolvido)
    if p["tipo"] == "PRINCIPAL":
        limite = base_pend + (r["segurado"] + si - r["liberado"] if r["desconto"] and r["principal_bloqueado"] <= TOLERANCIA
                              else si)
    else:
        limite = r["liberacao_disponivel"] + p["valor"]
    erro = None
    if not data_pg:
        erro = "Informe a data do pagamento."
    elif valor is None or valor <= 0:
        erro = "Informe o valor pago."
    elif valor > limite + TOLERANCIA:
        erro = f"O valor é maior que o permitido para este pagamento ({fmt_moeda(limite, r['moeda'])})."
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
    if obs != p["obs"]:
        mudancas.append("observação")
    if r["moeda"] != "BRL" and (cotacao or None) != (p["cotacao"] or None):
        mudancas.append(f"cotação {fmt_num(p['cotacao'], 4) if p['cotacao'] else '—'} → {fmt_num(cotacao, 4) if cotacao else '—'}")
    if mudancas:
        db.execute("UPDATE pagamentos SET data_pagamento = ?, valor = ?, banco = ?, obs = ?, cotacao = ? WHERE id = ?",
                   (data_pg, valor, banco, obs, cotacao if r["moeda"] != "BRL" else None, pid))
        if p["tipo"] == "PRINCIPAL" and r["desconto"]:  # v4.49: reajusta a parte do segurado paga junto
            novo_si = round(valor - base_pend, 2)
            novo_si = novo_si if novo_si > TOLERANCIA else 0
            if abs(novo_si - si) > TOLERANCIA:
                db.execute("UPDATE descontos SET valor_segurado = ? WHERE id = ?",
                           (round(r["segurado"] + si - novo_si, 2), r["desconto"]["id"]))
                db.execute("UPDATE pagamentos SET segurado_incluido = ? WHERE id = ?", (novo_si or None, pid))
                mudancas.append(f"segurado pago junto {fmt_moeda(si, r['moeda'])} → {fmt_moeda(novo_si, r['moeda'])}")
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
        arquivos_swift = excluir_arquivos_de("pagamento", pid)
        if p["segurado_incluido"] and r["desconto"]:  # v4.49: o valor volta a ficar segurado
            db.execute("UPDATE descontos SET valor_segurado = valor_segurado + ? WHERE id = ?",
                       (p["segurado_incluido"], r["desconto"]["id"]))
        db.execute("DELETE FROM pagamentos WHERE id = ?", (pid,))
        registrar_evento(p["invoice_id"], f"Pagamento excluído: {TIPOS_PAGAMENTO[p['tipo']]} de "
                         f"{fmt_moeda(p['valor'], r['moeda'])} em {fmt_data(p['data_pagamento'])}", motivo)
        db.commit()
        remover_arquivos(arquivos_swift)
        flash("Pagamento excluído.", "ok")
    return redirect(url_for("invoice_detalhe", id=p["invoice_id"]) + "#pagamentos")


def deducoes_ligadas(iid):
    """Deduções que ligam este invoice a outros (recebidas por ele ou feitas com o desconto dele)."""
    return get_db().execute(
        "SELECT x.*, d.invoice_id AS origem_id, o.numero AS origem_numero, i.numero AS destino_numero"
        " FROM deducoes x JOIN descontos d ON d.id = x.desconto_id JOIN invoices o ON o.id = d.invoice_id"
        " JOIN invoices i ON i.id = x.invoice_id WHERE x.invoice_id = ? OR d.invoice_id = ?", (iid, iid)).fetchall()


def excluir_invoice(iid):
    """Exclui o invoice e tudo o que depende dele (itens, condição, desconto, deduções, pagamentos, lançamentos,
    arquivos, histórico e e-mails). Deduções ligadas a outros invoices também são removidas, com registro no
    histórico desses invoices. Faz commit; os arquivos são apagados do disco depois."""
    db = get_db()
    inv = db.execute("SELECT * FROM invoices WHERE id = ?", (iid,)).fetchone()
    ligadas = deducoes_ligadas(iid)
    for x in ligadas:
        outro = x["origem_id"] if x["invoice_id"] == iid else x["invoice_id"]
        if outro != iid:
            registrar_evento(outro, f"Dedução removida pela exclusão do invoice {inv['numero']}",
                             f"{fmt_moeda(x['valor'], inv['moeda'])} (desconto do invoice {x['origem_numero']} "
                             f"deduzido no invoice {x['destino_numero']})")
    filtro_arq = ("(origem IN ('duimp', 'invoice') AND origem_id = ?) OR (origem = 'pagamento' AND origem_id IN"
                  " (SELECT id FROM pagamentos WHERE invoice_id = ?)) OR (origem = 'lancamento' AND origem_id IN"
                  " (SELECT id FROM lancamentos WHERE invoice_id = ?)) OR (origem = 'relatorio' AND origem_id IN"
                  " (SELECT id FROM descontos WHERE invoice_id = ?)) OR (origem = 'revisao' AND origem_id IN"
                  " (SELECT v.id FROM revisoes_desconto v JOIN descontos d ON d.id = v.desconto_id WHERE d.invoice_id = ?))"
                  " OR (origem = 'relatorio_versao' AND origem_id IN (SELECT id FROM relatorios_versoes WHERE invoice_id = ?))")
    arquivos = db.execute("SELECT * FROM arquivos WHERE " + filtro_arq, (iid,) * 6).fetchall()
    db.execute("DELETE FROM arquivos WHERE " + filtro_arq, (iid,) * 6)
    db.execute("DELETE FROM documentos WHERE (origem IN ('duimp', 'invoice') AND origem_id = ?) OR (origem = 'pagamento' AND origem_id IN"
               " (SELECT id FROM pagamentos WHERE invoice_id = ?)) OR (origem = 'lancamento' AND origem_id IN"
               " (SELECT id FROM lancamentos WHERE invoice_id = ?))", (iid, iid, iid))
    db.execute("DELETE FROM deducoes_avulsas WHERE invoice_id = ?", (iid,))
    db.execute("DELETE FROM duimp_dados WHERE invoice_id = ?", (iid,))
    db.execute("DELETE FROM inspecao_eventos WHERE invoice_id = ?", (iid,))
    for t in ("inspecao_reclass", "desconto_ajustes", "relatorios_versoes"):
        db.execute(f"DELETE FROM {t} WHERE invoice_id = ?", (iid,))
    db.execute("DELETE FROM deducoes WHERE invoice_id = ? OR desconto_id IN (SELECT id FROM descontos WHERE invoice_id = ?)",
               (iid, iid))
    db.execute("DELETE FROM revisoes_desconto WHERE desconto_id IN (SELECT id FROM descontos WHERE invoice_id = ?)", (iid,))
    db.execute("DELETE FROM lancamento_anexos WHERE lancamento_id IN (SELECT id FROM lancamentos WHERE invoice_id = ?)",
               (iid,))
    for t in ("descontos", "pagamentos", "invoice_itens", "condicao_parcelas", "lancamentos", "eventos",
              "notificacoes_estado", "anexos", "emails"):
        db.execute(f"DELETE FROM {t} WHERE invoice_id = ?", (iid,))
    db.execute("DELETE FROM invoices WHERE id = ?", (iid,))
    db.commit()
    remover_arquivos(arquivos)
    return inv, ligadas, len(arquivos)


@app.route("/invoices/<int:id>/excluir", methods=["POST"])
@somente_admin
def invoice_excluir(id):
    """Exclusão definitiva de um invoice pelo Administrador (backup do banco antes)."""
    r = carregar_invoice(id)
    if request.form.get("confirmacao", "").strip().lower() != r["numero"].strip().lower():
        flash(f"Para excluir, digite exatamente o número do invoice ({r['numero']}).", "erro")
        return redirect(url_for("invoice_detalhe", id=id) + "#admin")
    backup = backup_banco("-antes-de-excluir-invoice")
    inv, ligadas, n_arq = excluir_invoice(id)
    flash(f"Invoice {inv['numero']} ({r['fornecedor']}) excluído definitivamente"
          + (f", com {len(ligadas)} dedução(ões) ligada(s) a outros invoices" if ligadas else "")
          + (f" e {n_arq} arquivo(s)" if n_arq else "")
          + f". Backup do banco feito antes: {os.path.basename(backup) if backup else '—'}.", "ok")
    return redirect(url_for("painel"))


def marcar_historico(iid, marcar):
    """v4.52: trata um invoice como os da carga de histórico (sem pendências; etapas até a chegada puladas;
    inspeção e desconto em diante preenchidos normalmente) — ou volta ao fluxo normal. Não faz commit."""
    db = get_db()
    db.execute("UPDATE invoices SET historico = ? WHERE id = ?", (1 if marcar else 0, iid))
    registrar_evento(iid, "Invoice passou a ser tratado como histórico (sem pendências; etapas até a chegada puladas)"
                     if marcar else "Invoice voltou ao fluxo normal (deixou de ser tratado como histórico)")


@app.route("/invoices/<int:id>/historico", methods=["POST"])
@somente_admin
def invoice_historico(id):
    r = carregar_invoice(id)
    marcar = request.form.get("acao") == "marcar"
    if bool(r["historico"]) != marcar:
        marcar_historico(id, marcar)
        get_db().commit()
        flash("Invoice tratado como histórico: sem pendências e com as etapas até a chegada puladas." if marcar
              else "Invoice voltou ao fluxo normal.", "ok")
    return redirect(url_for("invoice_detalhe", id=id) + "#admin")


@app.route("/sistema/tratar-historico", methods=["POST"])
@somente_admin
def sistema_tratar_historico():
    """Marca vários invoices como histórico de uma vez, pelo número (um por linha, ou separados por vírgula)."""
    numeros = [n.strip() for n in re.split(r"[\n,;]+", request.form.get("numeros", "")) if n.strip()]
    db = get_db()
    feitos, ja, nao = [], [], []
    for n in numeros:
        achados = db.execute("SELECT i.id, i.numero, i.historico, f.nome FROM invoices i JOIN fornecedores f"
                             " ON f.id = i.fornecedor_id WHERE lower(trim(i.numero)) = lower(?) AND i.cancelado_em IS NULL",
                             (n,)).fetchall()
        if not achados:
            nao.append(n)
        for a in achados:
            if a["historico"]:
                ja.append(f"{a['numero']} ({a['nome']})")
            else:
                marcar_historico(a["id"], True)
                feitos.append(f"{a['numero']} ({a['nome']})")
    db.commit()
    if feitos:
        flash(f"Tratados como histórico: {', '.join(feitos)}.", "ok")
    if ja:
        flash(f"Já eram histórico: {', '.join(ja)}.", "ok")
    if nao:
        flash(f"Não encontrados (confira o número exato): {', '.join(nao)}.", "erro")
    if not numeros:
        flash("Informe os números dos invoices.", "erro")
    return redirect(url_for("sistema") + "#tratar-historico")


@app.route("/invoices/<int:id>/cancelar", methods=["POST"])
@somente_admin
def invoice_cancelar(id):
    r = carregar_invoice(id)
    motivo = request.form.get("motivo", "").strip()
    if r["cancelado_em"]:
        flash("Este invoice já está cancelado.", "erro")
    elif r["pagamentos"] or r["deducoes_recebidas"] or r["deducoes_avulsas"] or r["desconto"]:
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
# ---------------------------------------------------------------------------
# Inspeção de recebimento (v4.53): base de eventos do Agregador MEGA, importada toda semana
# - Vínculo com o invoice pela NF de entrada (NUMERO_NF = número da NF do Desembaraço); sem NF cadastrada,
#   pelo número do invoice da coluna INVOICE (sem o nome do fornecedor do fim); sem vínculo, fica o texto do Excel.
# - Não há identificador único do evento: a importação compara invoice a invoice. Se o relatório ainda não foi
#   enviado ao fornecedor, os eventos do invoice são trocados pelos da extração mais recente (as reclassificações
#   já feitas são reaproveitadas para o mesmo evento); com relatório enviado, nada muda (só avisa).
# ---------------------------------------------------------------------------
PASTA_IMPORTACOES = os.path.join(os.path.dirname(DB_PATH), "importacoes")
CAMPOS_GRAVADOS = ["invoice_excel", "numero_nf", "fornecedor_excel", "produto", "seq", "descricao", "codigo_mega",
                   "quantidade", "criterio", "comentario", "data_inclusao", "data_inspecao", "inspetor", "cod_inspetor",
                   "report_date", "lote", "area", "familia", "family", "qtde_nf", "vlr_unit_nf"]


# v4.54: a padronização dos termos começou pouco antes de 01/07/2026; eventos com REPORT DATE anterior não geram
# pendência de tradução (eventos sem REPORT DATE contam como novos)
TRADUCAO_DESDE = "2026-07-01"


def exige_traducao(e):
    return (e["report_date"] or "9999")[:10] >= TRADUCAO_DESDE


def glossario():
    return {r["chave"]: r["texto_en"] for r in get_db().execute("SELECT chave, texto_en FROM traducao_termos")}


def assinatura_evento(e):
    """Identifica o mesmo evento entre duas extrações (não há código único no MEGA)."""
    def v(x):
        if x is None:
            return ""
        if isinstance(x, (int, float)):
            return f"{float(x):g}"
        t = str(x).strip()
        try:  # número gravado como texto (ex.: critério "017", código do inspetor)
            return f"{float(t):g}"
        except ValueError:
            return t
    return "|".join(v(e[c]) for c in
                    ("produto", "seq", "quantidade", "criterio", "comentario", "data_inspecao", "cod_inspetor", "lote"))


# v4.59: o MEGA usa outro nome para o fornecedor (ex.: "DR. FRIGZ INTERNATIONAL PVT LTD" = "Dr. Frigz")
FORNECEDORES_MEGA_INICIAL = [
    ("AR INSTRUMED", "AR"), ("ARTIFOL ENTERPRISES", "Artifol"), ("BONADENT MANUFACTURING CO.", "Bonadent"),
    ("DENTACRAFT INSTRUMENTS", "Dentacraft"), ("DR. FRIGZ INTERNATIONAL PVT LTD", "Dr. Frigz"),
    ("FIZZA SURGICAL INTERNACIONAL", "Fizza"), ("GEMKIND", "Gemkind"), ("HAWK INSTRUMENTS", "Hawk"),
    ("Kummas Corporation", "Kummas"), ("LIBRA SURGICAL INSTRUMENTS", "Libra"), ("M.A.ARAIN BROTHERS PVT. LTD", "M.A. Arain"),
    ("MEDISPOREX PRIVATE LIMITED", "Medisporex"), ("ORTHOGENT ENGINEERING", "Orthogent"),
    ("PANORAMA INTERNATIONAL", "Panorama"), ("Rocky Surgimed Manufacturing INC", "Rocky"), ("SAMICO INDUSTRIES", "Samico"),
    ("SHEFFIELD SURGICAL INSTRUMENTS", "Sheffield"), ("TECHNIMEN AGHA (PVT) LTD.", "Technimen"),
    ("TONTARRA MEDIZINTECHNIK GMBH", "Tontarra"), ("TRIMED PVT LTD", "Trimed"), ("WELDON INSTRUMENTS", "Weldon"),
    ("ZEPF MEDICAL INSTRUMENTS GMBH ®", "Zepf"),
]


def mapa_fornecedor_mega():
    return {" ".join(r["nome_mega"].split()).lower(): r["nome_sistema"]
            for r in get_db().execute("SELECT nome_mega, nome_sistema FROM fornecedor_mega")}


def fornecedor_sistema(nome_mega, mapa=None):
    """Nome do fornecedor no sistema para o nome que vem do MEGA (sem relação cadastrada, o próprio nome do MEGA)."""
    if not nome_mega:
        return nome_mega
    mapa = mapa if mapa is not None else mapa_fornecedor_mega()
    return mapa.get(" ".join(str(nome_mega).split()).lower(), nome_mega)


app.jinja_env.globals["fornecedor_sistema"] = lambda n: fornecedor_sistema(n)


def numeros_candidatos(invoice_excel):
    """O Excel escreve "26000069 - DR.FRIGZ" ou "ORTHOGENT - 92-786-OGE-342": cada parte pode ser o número."""
    t = str(invoice_excel or "").strip()
    partes = [p.strip() for p in re.split(r"\s+-\s+", t) if p.strip()]
    return list(dict.fromkeys([t] + partes))


def vincular_eventos(eventos):
    """Preenche invoice_id e grupo de cada evento. Devolve avisos (NF vazia etc.).
    1º pela NF de entrada; sem NF cadastrada, pelo número do invoice dentro do fornecedor (relação MEGA × sistema);
    por fim, pelo número sozinho, se for único."""
    db = get_db()
    por_nf, por_numero, por_forn_numero = {}, {}, {}
    for i in db.execute("SELECT i.id, i.numero, i.numero_nf, lower(f.nome) AS forn FROM invoices i"
                        " JOIN fornecedores f ON f.id = i.fornecedor_id WHERE i.cancelado_em IS NULL"):
        if i["numero_nf"]:
            por_nf.setdefault(str(i["numero_nf"]).strip().lstrip("0"), []).append(i["id"])
        por_numero.setdefault(i["numero"].strip().lower(), []).append(i["id"])
        por_forn_numero.setdefault((i["forn"], i["numero"].strip().lower()), []).append(i["id"])
    mapa = mapa_fornecedor_mega()
    sem_nf = 0
    for e in eventos:
        nf = str(e.get("numero_nf") or "").strip()
        e["numero_nf"] = nf or None
        if not nf:
            sem_nf += 1
        iid = None
        ids = por_nf.get(nf.lstrip("0")) if nf else None
        if ids and len(ids) == 1:
            iid = ids[0]
        else:
            forn = str(fornecedor_sistema(e.get("fornecedor_excel"), mapa) or "").strip().lower()
            for num in numeros_candidatos(e.get("invoice_excel")):
                ids = por_forn_numero.get((forn, num.lower())) or por_numero.get(num.lower())
                if ids and len(ids) == 1:
                    iid = ids[0]
                    break
        e["invoice_id"] = iid
        e["grupo"] = f"inv:{iid}" if iid else "xls:" + (str(e.get("invoice_excel") or "").strip() or f"NF {nf or '?'}")
        crit = insp.categoria_do_criterio(e.get("criterio"))
        cat = str(e.get("categoria") or "").strip().upper() or None
        e["categoria_original"] = crit or cat
        e["categoria"] = cat if cat in insp.CATEGORIAS else crit
    avisos = []
    if sem_nf:
        avisos.append(f"{sem_nf} evento(s) sem NF de entrada (NUMERO_NF) na planilha: confira a extração.")
    return avisos


def religar_grupos():
    """Grupos sem vínculo ("Excel") que agora encontram o invoice (NF cadastrada ou relação de fornecedor nova)
    passam a ser do invoice. Só quando o invoice ainda não tem eventos. Devolve quantos foram ligados."""
    db = get_db()
    ligados = 0
    for (grupo,) in db.execute("SELECT DISTINCT grupo FROM inspecao_eventos WHERE grupo LIKE 'xls:%'").fetchall():
        e = dict(db.execute("SELECT numero_nf, invoice_excel, fornecedor_excel, criterio, categoria FROM inspecao_eventos"
                            " WHERE grupo = ? LIMIT 1", (grupo,)).fetchone())
        vincular_eventos([e])
        iid = e["invoice_id"]
        if iid and not db.execute("SELECT 1 FROM inspecao_eventos WHERE grupo = ?", (f"inv:{iid}",)).fetchone():
            db.execute("UPDATE inspecao_eventos SET grupo = ?, invoice_id = ? WHERE grupo = ?", (f"inv:{iid}", iid, grupo))
            db.execute("UPDATE inspecao_reclass SET grupo = ?, invoice_id = ? WHERE grupo = ?", (f"inv:{iid}", iid, grupo))
            registrar_evento(iid, "Inspeção de recebimento ligada ao invoice", f"Eventos do Excel \"{grupo[4:]}\"")
            ligados += 1
    return ligados


@app.route("/inspecoes/fornecedores", methods=["GET", "POST"])
@login_obrigatorio
def fornecedores_mega():
    """Relação entre o nome do fornecedor no MEGA e no sistema."""
    db = get_db()
    admin = g.usuario["perfil"] == "admin"
    if request.method == "POST":
        if not admin:
            abort(403)
        n = 0
        for mega, sistema in zip(request.form.getlist("nome_mega"), request.form.getlist("nome_sistema")):
            mega, sistema = " ".join(mega.split()), " ".join(sistema.split())
            if not mega:
                continue
            if sistema:
                db.execute("INSERT INTO fornecedor_mega (nome_mega, nome_sistema, atualizado_por, atualizado_em) VALUES (?, ?, ?, ?)"
                           " ON CONFLICT(nome_mega) DO UPDATE SET nome_sistema = excluded.nome_sistema,"
                           " atualizado_por = excluded.atualizado_por, atualizado_em = excluded.atualizado_em",
                           (mega, sistema, g.usuario["id"], agora()))
            else:
                db.execute("DELETE FROM fornecedor_mega WHERE nome_mega = ?", (mega,))
            n += 1
        ligados = religar_grupos()
        db.commit()
        flash(f"Relação salva.{f' {ligados} invoice(s) de inspeção passaram a estar ligados ao sistema.' if ligados else ''}", "ok")
        return redirect(url_for("fornecedores_mega"))
    rel = {" ".join(r["nome_mega"].split()).lower(): r for r in db.execute("SELECT * FROM fornecedor_mega")}
    nomes_mega = {}
    for r in db.execute("SELECT fornecedor_excel AS n, COUNT(*) AS q FROM inspecao_eventos WHERE fornecedor_excel IS NOT NULL"
                        " GROUP BY fornecedor_excel"):
        nomes_mega[" ".join(r["n"].split()).lower()] = (r["n"], r["q"])
    linhas = []
    for k in sorted(set(rel) | set(nomes_mega)):
        nome = nomes_mega.get(k, (rel[k]["nome_mega"] if k in rel else k, 0))
        linhas.append({"mega": nome[0], "eventos": nome[1], "sistema": rel[k]["nome_sistema"] if k in rel else ""})
    sistema = [f["nome"] for f in db.execute("SELECT nome FROM fornecedores ORDER BY nome")]
    return render_template("fornecedores_mega.html", linhas=linhas, sistema=sistema, admin=admin)


def janela_laudos(eventos):
    """v4.75: período de laudos (REPORT DATE / DATA_DO_LAUDO) que a planilha cobre: (menor, maior) ou None.
    A extração nova é semanal (só os eventos novos): a importação troca só os eventos desse período e mantém os
    anteriores. Uma extração completa cobre todo o histórico, então continua trocando tudo."""
    datas = [str(e.get("report_date"))[:10] for e in eventos if e.get("report_date")]
    return (min(datas), max(datas)) if datas else None


def na_janela(report_date, janela):
    """Evento da base que a planilha substitui: laudo dentro do período dela (ou ainda sem laudo)."""
    if janela is None or not report_date:
        return True
    return janela[0] <= str(report_date)[:10] <= janela[1]


def grupos_atuais(janela=None):
    """{grupo: {qtd (no período da planilha), total, assinaturas (multiconjunto), invoice_id, bloqueado}}"""
    from collections import Counter
    db = get_db()
    res = {}
    for e in db.execute("SELECT * FROM inspecao_eventos"):
        g_ = res.setdefault(e["grupo"], {"qtd": 0, "total": 0, "assin": Counter(), "invoice_id": e["invoice_id"]})
        g_["total"] += 1
        if not na_janela(e["report_date"], janela):
            continue
        g_["qtd"] += 1
        g_["assin"][assinatura_evento(e)] += 1
    for k, g_ in res.items():
        g_["bloqueado"] = relatorio_enviado(g_["invoice_id"])
    return res


def relatorio_enviado(invoice_id):
    """Depois de enviado o relatório (desconto enviado/aprovado), a extração não muda mais os eventos do invoice."""
    if not invoice_id:
        return False
    db = get_db()
    d = db.execute("SELECT status FROM descontos WHERE invoice_id = ?", (invoice_id,)).fetchone()
    if d and d["status"] in ("ENVIADO", "APROVADO"):
        return True
    return bool(db.execute("SELECT 1 FROM relatorios_versoes WHERE invoice_id = ? AND aplicado_em IS NOT NULL",
                           (invoice_id,)).fetchone())


def plano_importacao(eventos):
    """Compara a planilha com a base, invoice a invoice (só os eventos do período de laudos da planilha)."""
    from collections import Counter
    atuais = grupos_atuais(janela_laudos(eventos))
    novos = {}
    for e in eventos:
        novos.setdefault(e["grupo"], []).append(e)
    db = get_db()
    nomes = {i["id"]: i for i in db.execute(
        "SELECT i.id, i.numero, i.numero_nf, f.nome AS fornecedor FROM invoices i JOIN fornecedores f ON f.id = i.fornecedor_id")}
    linhas = []
    for gk, evs in novos.items():
        atual = atuais.get(gk)
        assin = Counter(assinatura_evento(e) for e in evs)
        if not atual:
            acao = "novo"
        elif atual["assin"] == assin:
            acao = "igual"
        elif atual["bloqueado"]:
            acao = "bloqueado"
        else:
            acao = "atualizar"
        iid = evs[0]["invoice_id"]
        inv = nomes.get(iid)
        itens_inv = {x["codigo"].strip().upper() for x in db.execute(
            "SELECT codigo FROM invoice_itens WHERE invoice_id = ? AND codigo IS NOT NULL", (iid,))} if iid else set()
        fora = sorted({str(e["produto"]).strip() for e in evs if iid and itens_inv
                       and str(e["produto"] or "").strip().upper() not in itens_inv})
        linhas.append({"grupo": gk, "invoice_id": iid, "invoice": inv["numero"] if inv else evs[0].get("invoice_excel"),
                       "fornecedor": inv["fornecedor"] if inv else fornecedor_sistema(evs[0].get("fornecedor_excel")),
                       "nf": evs[0].get("numero_nf"), "eventos": len(evs), "atuais": atual["qtd"] if atual else 0,
                       "mantidos": (atual["total"] - atual["qtd"]) if atual else 0,
                       "acao": acao, "itens_fora": fora,
                       "data": max((str(e.get("data_inclusao") or "") for e in evs), default="")})
    linhas.sort(key=lambda x: (x["acao"] == "igual", x["data"]), reverse=False)
    linhas.sort(key=lambda x: ({"atualizar": 0, "novo": 1, "bloqueado": 2, "igual": 3}[x["acao"]], x["data"]), reverse=False)
    return linhas


def gravar_eventos(grupo, eventos, importacao_id, janela=None):
    """Troca os eventos do grupo pelos da extração, reaproveitando as reclassificações do mesmo evento.
    v4.75: só troca os eventos com laudo no período da planilha (janela); os anteriores ficam como estão."""
    from collections import defaultdict
    db = get_db()
    # partes de cada evento original (um evento dividido numa reclassificação parcial tem várias partes)
    antigos = defaultdict(list)
    apagar, maior_ordem = [], -1
    for e in db.execute("SELECT * FROM inspecao_eventos WHERE grupo = ? ORDER BY ordem, id", (grupo,)):
        if na_janela(e["report_date"], janela):
            antigos[e["chave_origem"] or assinatura_evento(e) + "#0"].append(e)
            apagar.append(e["id"])
        else:
            maior_ordem = max(maior_ordem, e["ordem"] or 0)
    db.executemany("DELETE FROM inspecao_eventos WHERE id = ?", [(i,) for i in apagar])
    inicio_ordem = maior_ordem + 1
    linhas = []
    vistos = defaultdict(int)
    colunas = (f"grupo, invoice_id, {', '.join(CAMPOS_GRAVADOS)}, categoria_original, categoria, outros, reclass_por,"
               " reclass_em, reclass_motivo, importacao_id, ordem, chave_origem, comentario_original")
    for ordem, e in enumerate(eventos):
        assin = assinatura_evento(e)
        chave = f"{assin}#{vistos[assin]}"
        vistos[assin] += 1
        outros = json.dumps({k: e.get(k) for k in ("bu", "grupo_estoque", "qtde_pedida", "usuario_inclusao",
                                                   "item_inspecionado", "status_lote", "vlr_item_nf", "data_nf")},
                            default=str)
        base = [None if e.get(c) is None else e.get(c) for c in CAMPOS_GRAVADOS]
        partes = antigos.get(chave) or []
        mudou = [a for a in partes if a["reclass_em"] or a["categoria"] != a["categoria_original"] or a["comentario_original"]]
        if mudou and abs(sum(a["quantidade"] or 0 for a in partes) - (e.get("quantidade") or 0)) < 0.001:
            # mesmo evento: recria as partes com as reclassificações (categoria e falha) já feitas
            for a in partes:
                b = list(base)
                b[CAMPOS_GRAVADOS.index("quantidade")] = a["quantidade"]
                if a["comentario_original"]:
                    b[CAMPOS_GRAVADOS.index("comentario")] = a["comentario"]
                linhas.append((grupo, e["invoice_id"], *b, e["categoria_original"], a["categoria"], outros,
                               a["reclass_por"], a["reclass_em"], a["reclass_motivo"], importacao_id, inicio_ordem + ordem, chave,
                               a["comentario_original"]))
            continue
        cat, rm = e["categoria"], None
        if cat and e["categoria_original"] and cat != e["categoria_original"]:
            rm = "Reclassificado na planilha (antes do sistema)"
        linhas.append((grupo, e["invoice_id"], *base, e["categoria_original"], cat, outros, None, None, rm,
                       importacao_id, inicio_ordem + ordem, chave, None))
    db.executemany(f"INSERT INTO inspecao_eventos ({colunas}) VALUES ({', '.join('?' * (len(CAMPOS_GRAVADOS) + 12))})",
                   linhas)


@app.route("/inspecoes")
@login_obrigatorio
def inspecoes():
    from collections import Counter
    db = get_db()
    glos = glossario()
    resumo = {}
    for e in db.execute("SELECT grupo, invoice_id, invoice_excel, numero_nf, fornecedor_excel, quantidade, categoria,"
                        " comentario, data_inclusao, reclass_em, categoria_original, report_date FROM inspecao_eventos"):
        r = resumo.setdefault(e["grupo"], {"grupo": e["grupo"], "invoice_id": e["invoice_id"], "invoice": e["invoice_excel"],
                                          "nf": e["numero_nf"], "fornecedor": e["fornecedor_excel"], "eventos": 0,
                                          "qtd": Counter(), "pendentes": set(), "data": "", "reclass": 0})
        r["eventos"] += 1
        r["qtd"][e["categoria"] or "?"] += e["quantidade"] or 0
        r["data"] = max(r["data"], (e["report_date"] or "")[:10])  # v4.55: "última inclusão" = maior REPORT DATE
        if e["categoria"] != e["categoria_original"]:
            r["reclass"] += 1
        if exige_traducao(e):
            _en, falta = insp.traduzir(e["comentario"], glos)
            r["pendentes"].update(falta)
    invs = {i["id"]: i for i in db.execute(
        "SELECT i.id, i.numero, f.nome AS fornecedor, d.status AS desconto_status FROM invoices i"
        " JOIN fornecedores f ON f.id = i.fornecedor_id LEFT JOIN descontos d ON d.invoice_id = i.id")}
    lista = []
    mapa = mapa_fornecedor_mega()
    for r in resumo.values():
        r["fornecedor"] = fornecedor_sistema(r["fornecedor"], mapa)
        inv = invs.get(r["invoice_id"])
        if inv:
            r["invoice"], r["fornecedor"], r["desconto_status"] = inv["numero"], inv["fornecedor"], inv["desconto_status"]
        total = sum(r["qtd"].values())
        r["total"] = total
        r["pct_cd"] = (r["qtd"]["C"] + r["qtd"]["D"]) / total if total else 0
        lista.append(r)
    lista.sort(key=lambda r: r["data"], reverse=True)
    importacoes = db.execute("SELECT m.*, u.nome AS usuario FROM inspecao_importacoes m LEFT JOIN usuarios u"
                             " ON u.id = m.criado_por ORDER BY m.id DESC LIMIT 5").fetchall()
    return render_template("inspecoes.html", lista=lista, importacoes=importacoes, admin=g.usuario["perfil"] == "admin",
                           pendentes_total=len(set().union(*[r["pendentes"] for r in lista])) if lista else 0)


def preencher_family(eventos):
    """v4.75: o formato novo do MEGA só traz a família em português (FAMÍLIA). A família em inglês (usada no Excel ao
    fornecedor) vem da base: a tradução mais usada para a mesma família ou, sem ela, a do mesmo produto."""
    db = get_db()
    por_familia, por_produto = {}, {}
    for x in db.execute("SELECT lower(trim(familia)) AS k, family, COUNT(*) AS n FROM inspecao_eventos"
                        " WHERE familia IS NOT NULL AND family IS NOT NULL AND trim(family) <> '' GROUP BY 1, 2 ORDER BY n"):
        por_familia[x["k"]] = x["family"]
    for x in db.execute("SELECT upper(trim(produto)) AS k, family, COUNT(*) AS n FROM inspecao_eventos"
                        " WHERE family IS NOT NULL AND trim(family) <> '' GROUP BY 1, 2 ORDER BY n"):
        por_produto[x["k"]] = x["family"]
    for e in eventos:
        if not e.get("family"):
            e["family"] = (por_familia.get(str(e.get("familia") or "").strip().lower())
                           or por_produto.get(str(e.get("produto") or "").strip().upper()))


@app.route("/inspecoes/importar", methods=["POST"])
@somente_admin
def inspecoes_importar():
    arq = request.files.get("arquivo")
    if not arq or not arq.filename.lower().endswith((".xlsx", ".xlsm")):
        flash("Escolha a planilha (.xlsx ou .xlsm) extraída do Agregador MEGA.", "erro")
        return redirect(url_for("inspecoes"))
    try:
        eventos, traducoes, avisos, aba = insp.ler_planilha(io.BytesIO(arq.read()))
    except Exception as e:
        app.logger.warning("Falha ao ler a planilha de inspeções: %s", e)
        flash("Não foi possível ler a planilha. Confira se é um arquivo .xlsx/.xlsm válido.", "erro")
        return redirect(url_for("inspecoes"))
    if not eventos:
        for a in avisos or ["A planilha não tem eventos."]:
            flash(a, "erro")
        return redirect(url_for("inspecoes"))
    preencher_family(eventos)
    os.makedirs(PASTA_IMPORTACOES, exist_ok=True)
    for antigo in glob.glob(os.path.join(PASTA_IMPORTACOES, "*.json")):  # prévias abandonadas
        try:
            if datetime.now().timestamp() - os.path.getmtime(antigo) > 86400:
                os.remove(antigo)
        except OSError:
            pass
    token = secrets.token_hex(8)
    with open(os.path.join(PASTA_IMPORTACOES, f"{token}.json"), "w", encoding="utf-8") as fh:
        json.dump({"arquivo": arq.filename, "aba": aba, "eventos": eventos, "traducoes": traducoes, "avisos": avisos},
                  fh, default=str)
    return redirect(url_for("inspecoes_previa", token=token))


def carregar_previa(token):
    if not re.fullmatch(r"[0-9a-f]{16}", token or ""):
        abort(404)
    caminho = os.path.join(PASTA_IMPORTACOES, f"{token}.json")
    if not os.path.exists(caminho):
        flash("A prévia expirou: envie a planilha de novo.", "erro")
        return None, caminho
    with open(caminho, encoding="utf-8") as fh:
        return json.load(fh), caminho


@app.route("/inspecoes/importar/<token>", methods=["GET", "POST"])
@somente_admin
def inspecoes_previa(token):
    dados, caminho = carregar_previa(token)
    if dados is None:
        return redirect(url_for("inspecoes"))
    eventos = dados["eventos"]
    avisos = dados["avisos"] + vincular_eventos(eventos)
    plano = plano_importacao(eventos)
    termos_planilha = insp.termos_das_traducoes(dados["traducoes"]) if dados["traducoes"] else {}
    existentes = set(glossario())
    termos_novos = {k: v for k, v in termos_planilha.items() if k not in existentes}
    if request.method == "POST":
        db = get_db()
        escolhidos = set(request.form.getlist("grupo"))
        por_grupo = {}
        for e in eventos:
            por_grupo.setdefault(e["grupo"], []).append(e)
        aplicar = [p for p in plano if p["grupo"] in escolhidos and p["acao"] in ("novo", "atualizar")]
        mid = db.execute("INSERT INTO inspecao_importacoes (arquivo, eventos, grupos, resumo, criado_por, criado_em)"
                         " VALUES (?, ?, ?, ?, ?, ?)", (dados["arquivo"], sum(p["eventos"] for p in aplicar), len(aplicar),
                                                        None, g.usuario["id"], agora())).lastrowid
        janela = janela_laudos(eventos)
        for p in aplicar:
            gravar_eventos(p["grupo"], por_grupo[p["grupo"]], mid, janela)
            if p["invoice_id"]:
                registrar_evento(p["invoice_id"], "Inspeção de recebimento importada do Agregador MEGA",
                                 f"{p['eventos']} evento(s)" + (" (atualizados)" if p["acao"] == "atualizar" else ""))
        n_termos = 0
        if request.form.get("termos") and termos_novos:
            db.executemany("INSERT OR IGNORE INTO traducao_termos (chave, texto_pt, texto_en, origem, atualizado_por,"
                           " atualizado_em) VALUES (?, ?, ?, 'planilha', ?, ?)",
                           [(k, k.capitalize(), v, g.usuario["id"], agora()) for k, v in termos_novos.items()])
            n_termos = len(termos_novos)
        religar_grupos()
        resumo = {"novos": sum(1 for p in aplicar if p["acao"] == "novo"),
                  "atualizados": sum(1 for p in aplicar if p["acao"] == "atualizar"), "termos": n_termos}
        db.execute("UPDATE inspecao_importacoes SET resumo = ? WHERE id = ?", (json.dumps(resumo), mid))
        db.commit()
        try:
            os.remove(caminho)
        except OSError:
            pass
        flash(f"Importação concluída: {resumo['novos']} invoice(s) novo(s), {resumo['atualizados']} atualizado(s), "
              f"{sum(p['eventos'] for p in aplicar)} evento(s)." + (f" {n_termos} termo(s) de tradução carregado(s)."
                                                                   if n_termos else ""), "ok")
        return redirect(url_for("inspecoes"))
    return render_template("inspecoes_previa.html", plano=plano, avisos=avisos, arquivo=dados["arquivo"], aba=dados["aba"],
                           total=len(eventos), termos_novos=len(termos_novos), token=token, janela=janela_laudos(eventos))


@app.route("/inspecoes/<path:grupo>")
@login_obrigatorio
def inspecao_grupo(grupo):
    from collections import Counter, OrderedDict
    db = get_db()
    eventos = db.execute("SELECT * FROM inspecao_eventos WHERE grupo = ? ORDER BY produto, ordem", (grupo,)).fetchall()
    if not eventos:
        abort(404)
    glos = glossario()
    r = carregar_invoice(eventos[0]["invoice_id"]) if eventos[0]["invoice_id"] else None
    linhas, pendentes = [], Counter()
    por_item = OrderedDict()
    for e in eventos:
        en, falta = insp.traduzir(e["comentario"], glos)
        antigo = not exige_traducao(e)
        if not antigo:
            for k in falta:
                pendentes[k] += 1
        linhas.append({"e": e, "en": en, "falta": falta, "antigo": antigo})
        it = por_item.setdefault(e["produto"], {"descricao": e["descricao"], "qtd": Counter(), "total": 0})
        it["qtd"][e["categoria"]] += e["quantidade"] or 0
        it["total"] += e["quantidade"] or 0
    itens_inv = {x["codigo"].strip().upper(): x["quantidade"] for x in r["itens"] if x["codigo"]} if r else {}
    for cod, it in por_item.items():
        it["qtd_invoice"] = itens_inv.get(str(cod or "").strip().upper())
    totais = Counter()
    for it in por_item.values():
        totais.update(it["qtd"])
    # visão agrupada item → defeito (para selecionar e reclassificar em lote)
    grupos_def = OrderedDict()
    for x in linhas:
        e = x["e"]
        k = (e["produto"], insp.chave_termo(e["comentario"] or "") or "OK")
        gd = grupos_def.setdefault(k, {"produto": e["produto"], "comentario": e["comentario"], "en": x["en"], "ids": [],
                                       "qtd": Counter()})
        gd["ids"].append(e["id"])
        gd["qtd"][e["categoria"]] += e["quantidade"] or 0
    calc = calcular_desconto_inspecao(r) if r else None
    versoes = db.execute("SELECT v.*, u.nome AS usuario FROM relatorios_versoes v LEFT JOIN usuarios u ON u.id = v.criado_por"
                         " WHERE v.invoice_id = ? ORDER BY v.versao DESC", (r["id"],)).fetchall() if r else []
    reclass = db.execute("SELECT x.*, u.nome AS usuario FROM inspecao_reclass x LEFT JOIN usuarios u ON u.id = x.criado_por"
                         " WHERE x.grupo = ? ORDER BY x.id DESC", (grupo,)).fetchall()
    ajustes_hist = db.execute("SELECT a.*, u.nome AS usuario FROM desconto_ajustes a LEFT JOIN usuarios u ON u.id = a.criado_por"
                              " WHERE a.invoice_id = ? ORDER BY a.id DESC", (r["id"],)).fetchall() if r else []
    return render_template("inspecao_grupo.html", grupo=grupo, eventos=linhas, r=r, por_item=por_item, totais=totais,
                           total=sum(totais.values()), pendentes=pendentes, e0=eventos[0], grupos_def=list(grupos_def.values()),
                           calc=calc, versoes=versoes, reclass=reclass, ajustes_hist=ajustes_hist, falhas_conhecidas=falhas_conhecidas(grupo),
                           versao_json=lambda v: json.loads(v["linhas"]), admin=g.usuario["perfil"] == "admin")


# ---------------------------------------------------------------------------
# Fechamento do relatório de desconto (v4.56 — fase 2)
# Desconto por item = preço FOB (item do invoice) × (qtd. C × % de C + qtd. D × % de D)
# Política padrão: C 80%, D 100%. Por fornecedor (ex.: Orthogent): % de C conforme o % de A+B do item (faixas).
# Ajustes: % por item ou geral do invoice (com motivo, histórico). Item > geral > política.
# Reclassificação (só Engenharia de Processos): em lote ou parcial, com motivo; o fornecedor só vê a categoria final.
# ---------------------------------------------------------------------------
FAIXAS_ORTHOGENT = [[90, 0], [80, 50], [70, 60], [0, 80]]


def politica_desconto(fornecedor_id):
    db = get_db()
    p = db.execute("SELECT * FROM politicas_desconto WHERE fornecedor_id = ?", (fornecedor_id,)).fetchone() \
        or db.execute("SELECT * FROM politicas_desconto WHERE fornecedor_id = 0").fetchone()
    d = dict(p)
    d["faixas"] = json.loads(p["faixas"]) if p["faixas"] else []
    d["propria"] = p["fornecedor_id"] != 0
    return d


def descrever_politica(p):
    if p["tipo"] == "FAIXAS":
        partes = []
        for i, (minimo, pct) in enumerate(sorted(p["faixas"], key=lambda f: -f[0])):
            partes.append(f"A+B ≥ {fmt_num(minimo, 0)}%: C {fmt_num(pct, 0)}%" if minimo > 0 else f"abaixo: C {fmt_num(pct, 0)}%")
        return "C conforme o % de A+B do item (" + "; ".join(partes) + f"); D {fmt_num(p['pct_d'], 0)}%"
    return f"C {fmt_num(p['pct_c'], 0)}% e D {fmt_num(p['pct_d'], 0)}% do preço FOB"


def pct_c_politica(p, pct_ab):
    if p["tipo"] == "FAIXAS":
        for minimo, pct in sorted(p["faixas"], key=lambda f: -f[0]):
            if pct_ab * 100 + 1e-9 >= minimo:
                return pct
        return p["faixas"][-1][1] if p["faixas"] else 0
    return p["pct_c"]


def ajustes_atuais(invoice_id):
    """{produto ou None (geral): ajuste mais recente}"""
    atuais = {}
    for a in get_db().execute("SELECT a.*, u.nome AS usuario FROM desconto_ajustes a LEFT JOIN usuarios u ON u.id = a.criado_por"
                              " WHERE a.invoice_id = ? ORDER BY a.id", (invoice_id,)):
        atuais[a["produto"]] = a
    return {k: v for k, v in atuais.items() if v["pct_c"] is not None or v["pct_d"] is not None}


def calcular_desconto_inspecao(r):
    """Cálculo do desconto do invoice a partir dos eventos (categoria final) e dos itens do invoice (preço FOB)."""
    from collections import Counter
    db = get_db()
    pol = politica_desconto(r["fornecedor_id"])
    ajustes = ajustes_atuais(r["id"])
    geral = ajustes.get(None)
    itens = {}
    for x in r["itens"]:
        if x["codigo"]:
            k = x["codigo"].strip().upper()
            it = itens.setdefault(k, {"codigo": x["codigo"].strip(), "qtd_invoice": 0, "preco": x["valor_unit"], "descricao": x["item"]})
            it["qtd_invoice"] += x["quantidade"] or 0
    qtd = {}
    descr = {}
    for e in db.execute("SELECT produto, descricao, categoria, quantidade FROM inspecao_eventos WHERE invoice_id = ?", (r["id"],)):
        k = str(e["produto"] or "").strip().upper()
        qtd.setdefault(k, Counter())[e["categoria"]] += e["quantidade"] or 0
        descr.setdefault(k, e["descricao"])
    linhas, divergencias = [], []
    for k in sorted(set(itens) | set(qtd)):
        c = qtd.get(k, Counter())
        total = sum(c.values())
        it = itens.get(k)
        pct_ab = (c["A"] + c["B"]) / total if total else 0
        aj = ajustes.get(k)
        pc = pct_c_politica(pol, pct_ab)
        pd = pol["pct_d"]
        origem = "política"
        if geral:
            pc = geral["pct_c"] if geral["pct_c"] is not None else pc
            pd = geral["pct_d"] if geral["pct_d"] is not None else pd
            origem = "ajuste geral"
        if aj:
            pc = aj["pct_c"] if aj["pct_c"] is not None else pc
            pd = aj["pct_d"] if aj["pct_d"] is not None else pd
            origem = "ajuste do item"
        preco = it["preco"] if it else None
        desconto = round((preco or 0) * (c["C"] * pc + c["D"] * pd) / 100, 2)
        linha = {"codigo": it["codigo"] if it else (k or "?"), "chave": k, "descricao": descr.get(k) or (it or {}).get("descricao"),
                 "A": c["A"], "B": c["B"], "C": c["C"], "D": c["D"], "inspecionado": total,
                 "qtd_invoice": it["qtd_invoice"] if it else None, "pct_ab": pct_ab, "preco": preco, "pct_c": pc,
                 "pct_d": pd, "origem": origem, "ajuste": aj, "desconto": desconto}
        if not it:
            divergencias.append(f"{linha['codigo']}: inspecionado ({fmt_qtd(total)}) mas não está no invoice (sem preço FOB)")
        elif abs(total - it["qtd_invoice"]) > 0.001:
            divergencias.append(f"{linha['codigo']}: inspecionado {fmt_qtd(total)} × invoice {fmt_qtd(it['qtd_invoice'])}")
        linhas.append(linha)
    total_cat = Counter()
    for l in linhas:
        for cat in "ABCD":
            total_cat[cat] += l[cat]
    return {"linhas": linhas, "total": round(sum(l["desconto"] for l in linhas), 2), "politica": pol,
            "politica_txt": descrever_politica(pol), "geral": geral, "divergencias": divergencias,
            "valor_produtos": round(sum((it["preco"] or 0) * it["qtd_invoice"] for it in itens.values()), 2),
            "total_cat": total_cat, "inspecionado": sum(total_cat.values())}


def original_da_falha(e, novo):
    """Falha original guardada depois da correção (vazia se a falha voltou ao texto original do MEGA)."""
    original = e["comentario_original"] or (e["comentario"] if novo != e["comentario"] else None)
    return None if original is not None and novo == original else original


def reclassificar_eventos(eventos, para, motivo, qtd_parcial=None, comentario=None):
    """Reclassifica eventos (de um ou vários invoices) ou parte de um evento: muda a categoria e/ou corrige a falha
    encontrada (comentário; a original fica guardada). Devolve (eventos, peças, erro). Não faz commit."""
    from collections import defaultdict
    db = get_db()

    def alvo(e):
        return (para or e["categoria"]), (comentario if comentario is not None else e["comentario"])

    def descricao(n, q):
        partes = []
        if para:
            partes.append(f"para {para}")
        if comentario is not None:
            partes.append(f"falha: \"{comentario}\"")
        return f"Inspeção: {fmt_qtd(q)} peça(s) reclassificada(s) ({', '.join(partes)})"

    if qtd_parcial is not None:  # parcial: um único evento, dividido
        e = eventos[0]
        if len(eventos) != 1 or qtd_parcial <= 0 or qtd_parcial >= (e["quantidade"] or 0):
            return 0, 0, "Reclassificação parcial: marque um único evento e informe uma quantidade menor que a dele."
        cat, com = alvo(e)
        if cat == e["categoria"] and com == e["comentario"]:
            return 0, 0, "Nada muda: o evento já está nessa categoria e com essa falha."
        db.execute("UPDATE inspecao_eventos SET quantidade = ? WHERE id = ?", (round(e["quantidade"] - qtd_parcial, 4), e["id"]))
        cols = [c for c in e.keys() if c != "id"]
        novo = dict(e)
        novo.update(quantidade=qtd_parcial, categoria=cat, reclass_por=g.usuario["id"], reclass_em=agora(), reclass_motivo=motivo,
                    chave_origem=e["chave_origem"] or assinatura_evento(e) + "#0")
        if com != e["comentario"]:
            novo.update(comentario=com, comentario_original=original_da_falha(e, com))
        db.execute(f"INSERT INTO inspecao_eventos ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", [novo[c] for c in cols])
        if not e["chave_origem"]:
            db.execute("UPDATE inspecao_eventos SET chave_origem = ? WHERE id = ?", (novo["chave_origem"], e["id"]))
        db.execute("INSERT INTO inspecao_reclass (grupo, invoice_id, produto, comentario, quantidade, de, para, eventos, parcial,"
                   " motivo, criado_por, criado_em, comentario_novo) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?, ?)",
                   (e["grupo"], e["invoice_id"], e["produto"], e["comentario"], qtd_parcial, e["categoria"], cat, motivo,
                    g.usuario["id"], agora(), com if com != e["comentario"] else None))
        if e["invoice_id"]:
            registrar_evento(e["invoice_id"], descricao(1, qtd_parcial), f"1 evento (parcial) · {motivo}")
        return 1, qtd_parcial, None
    resumo = defaultdict(lambda: [0, 0])
    por_invoice = defaultdict(lambda: [0, 0])
    feitos, pecas = 0, 0
    for e in eventos:
        cat, com = alvo(e)
        if cat == e["categoria"] and com == e["comentario"]:
            continue
        db.execute("UPDATE inspecao_eventos SET categoria = ?, comentario = ?, comentario_original = ?, reclass_por = ?,"
                   " reclass_em = ?, reclass_motivo = ?, chave_origem = COALESCE(chave_origem, ?) WHERE id = ?",
                   (cat, com, original_da_falha(e, com),
                    g.usuario["id"], agora(), motivo, assinatura_evento(e) + "#0", e["id"]))
        k = (e["grupo"], e["invoice_id"], e["produto"], e["comentario"], e["categoria"], cat, com if com != e["comentario"] else None)
        resumo[k][0] += 1
        resumo[k][1] += e["quantidade"] or 0
        por_invoice[e["invoice_id"]][0] += 1
        por_invoice[e["invoice_id"]][1] += e["quantidade"] or 0
        feitos += 1
        pecas += e["quantidade"] or 0
    for (grupo, iid, produto, com_antigo, de, para_, com_novo), (n, q) in resumo.items():
        db.execute("INSERT INTO inspecao_reclass (grupo, invoice_id, produto, comentario, quantidade, de, para, eventos,"
                   " parcial, motivo, criado_por, criado_em, comentario_novo) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
                   (grupo, iid, produto, com_antigo, q, de, para_, n, motivo, g.usuario["id"], agora(), com_novo))
    for iid, (n, q) in por_invoice.items():
        if iid:
            registrar_evento(iid, descricao(n, q), f"{n} evento(s) · {motivo}")
    return feitos, pecas, None


def falhas_conhecidas(grupo=None):
    """Sugestões para o campo de falha: os comentários mais usados (do invoice, se informado, e da base toda)."""
    db = get_db()
    sql = ("SELECT comentario, COUNT(*) AS n FROM inspecao_eventos WHERE comentario IS NOT NULL AND trim(comentario) NOT IN"
           " ('', ';', 'OK', 'OK;') {} GROUP BY comentario ORDER BY n DESC LIMIT 300")
    lista = [r[0] for r in db.execute(sql.format("AND grupo = ?"), (grupo,))] if grupo else []
    lista += [r[0] for r in db.execute(sql.format(""))]
    return list(dict.fromkeys(lista))


def ler_reclassificacao(f):
    """(para, motivo, quantidade parcial, nova falha, erro) do formulário de reclassificação.
    Muda a categoria e/ou a falha encontrada (comentário); o motivo é sempre obrigatório."""
    para = f.get("para") if f.get("para") in insp.CATEGORIAS else None
    motivo = " ".join(f.get("motivo", "").split())[:300]
    comentario = " ".join((f.get("comentario") or "").split())[:300] or None
    if not (para or comentario) or not motivo:
        return None, None, None, None, "Escolha a nova categoria e/ou a nova falha encontrada e informe o motivo da reclassificação."
    qtd = parse_num(f.get("quantidade")) if (f.get("quantidade") or "").strip() else None
    return para, motivo, qtd, comentario, None


def mensagem_reclass(feitos, pecas, para, erro, comentario=None):
    if erro:
        flash(erro, "erro")
    elif feitos:
        oque = " e ".join(x for x in (f"categoria {para}" if para else "", f"falha \"{comentario}\"" if comentario else "") if x)
        flash(f"{feitos} evento(s) reclassificado(s): {oque} ({fmt_qtd(pecas)} peça(s)).", "ok")
    else:
        flash("Nada mudou: os eventos já estavam assim.", "erro")


@app.route("/inspecoes/<path:grupo>/reclassificar", methods=["POST"])
@somente_admin
def inspecao_reclassificar(grupo):
    """Reclassificação em lote (eventos marcados) ou parcial (parte da quantidade de um evento), sempre com motivo."""
    db = get_db()
    volta = redirect(url_for("inspecao_grupo", grupo=grupo) + "#eventos")
    para, motivo, qtd, comentario, erro = ler_reclassificacao(request.form)
    if erro:
        flash(erro, "erro")
        return volta
    ids = [int(x) for x in request.form.getlist("evento") if x.isdigit()]
    eventos = db.execute(f"SELECT * FROM inspecao_eventos WHERE grupo = ? AND id IN ({','.join('?' * len(ids))})",
                         (grupo, *ids)).fetchall() if ids else []
    if not eventos:
        flash("Marque os eventos a reclassificar.", "erro")
        return volta
    feitos, pecas, erro = reclassificar_eventos(eventos, para, motivo, qtd, comentario)
    if not erro:
        db.commit()
    mensagem_reclass(feitos, pecas, para, erro, comentario)
    return volta


# ---------- base de dados completa (v4.57): consulta e reclassificação de eventos de qualquer invoice ----------
FILTROS_BASE = ("fornecedor", "invoice", "item", "categoria", "defeito", "de", "ate", "reclass")


def consulta_base(args):
    """(SQL WHERE, parâmetros) a partir dos filtros da tela da base de dados."""
    where, params = ["1 = 1"], []
    if args.get("fornecedor"):  # v4.58: escolhido na lista (nome do sistema ou, sem vínculo, o do Excel)
        where.append("COALESCE(f.nome, fm.nome_sistema, e.fornecedor_excel, '') = ?")
        params.append(args["fornecedor"])
    if args.get("invoice"):  # o valor é o grupo do invoice
        where.append("e.grupo = ?")
        params.append(args["invoice"])
    if args.get("item"):
        where.append("lower(COALESCE(e.produto, '')) LIKE ?")
        params.append(f"%{args['item'].strip().lower()}%")
    if args.get("categoria") in insp.CATEGORIAS:
        where.append("e.categoria = ?")
        params.append(args["categoria"])
    if args.get("defeito"):
        where.append("lower(COALESCE(e.comentario, '')) LIKE ?")
        params.append(f"%{args['defeito'].strip().lower()}%")
    if data_valida(args.get("de")):
        where.append("substr(COALESCE(e.report_date, ''), 1, 10) >= ?")
        params.append(data_valida(args.get("de")))
    if data_valida(args.get("ate")):
        where.append("substr(COALESCE(e.report_date, ''), 1, 10) <= ?")
        params.append(data_valida(args.get("ate")))
    if args.get("reclass") == "1":
        where.append("(e.categoria IS NOT e.categoria_original OR e.reclass_em IS NOT NULL)")
    return " AND ".join(where), params


SQL_BASE = ("FROM inspecao_eventos e LEFT JOIN invoices i ON i.id = e.invoice_id"
            " LEFT JOIN fornecedores f ON f.id = i.fornecedor_id"
            " LEFT JOIN fornecedor_mega fm ON fm.nome_mega = trim(e.fornecedor_excel)")


@app.route("/inspecoes-base", methods=["GET", "POST"])
@login_obrigatorio
def inspecoes_base():
    """Base de dados de inspeção (todos os invoices), com filtros e reclassificação em lote."""
    db = get_db()
    admin = g.usuario["perfil"] == "admin"
    if request.method == "POST":
        if not admin:
            abort(403)
        f = request.form
        filtros = {k: f.get("f_" + k, "") for k in FILTROS_BASE}
        volta = redirect(url_for("inspecoes_base", **{k: v for k, v in filtros.items() if v}, pagina=f.get("pagina", 1)))
        para, motivo, qtd, comentario, erro = ler_reclassificacao(f)
        if erro:
            flash(erro, "erro")
            return volta
        if f.get("todos_do_filtro"):
            where, params = consulta_base(filtros)
            eventos = db.execute(f"SELECT e.* {SQL_BASE} WHERE {where}", params).fetchall()
            if qtd is not None:
                flash("A reclassificação parcial é feita num evento por vez (marque só ele).", "erro")
                return volta
        else:
            ids = [int(x) for x in f.getlist("evento") if x.isdigit()]
            eventos = db.execute(f"SELECT * FROM inspecao_eventos WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall() if ids else []
        if not eventos:
            flash("Marque os eventos a reclassificar.", "erro")
            return volta
        feitos, pecas, erro = reclassificar_eventos(eventos, para, motivo, qtd, comentario)
        if not erro:
            db.commit()
        mensagem_reclass(feitos, pecas, para, erro, comentario)
        return volta
    args = {k: request.args.get(k, "") for k in FILTROS_BASE}
    where, params = consulta_base(args)
    total, pecas = db.execute(f"SELECT COUNT(*), COALESCE(SUM(e.quantidade), 0) {SQL_BASE} WHERE {where}", params).fetchone()
    por_pagina = 300
    pagina = max(1, request.args.get("pagina", 1, type=int))
    paginas = max(1, -(-total // por_pagina))
    pagina = min(pagina, paginas)
    eventos = db.execute(f"SELECT e.*, i.numero AS invoice_sistema, COALESCE(f.nome, fm.nome_sistema) AS fornecedor_sistema {SQL_BASE} WHERE {where}"
                         " ORDER BY substr(COALESCE(e.report_date, ''), 1, 10) DESC, e.grupo, e.produto, e.ordem"
                         f" LIMIT {por_pagina} OFFSET {(pagina - 1) * por_pagina}", params).fetchall()
    glos = glossario()
    linhas = [{"e": e, "en": insp.traduzir(e["comentario"], glos)[0]} for e in eventos]
    opcoes = db.execute(f"SELECT e.grupo, COALESCE(i.numero, e.invoice_excel, e.grupo) AS invoice,"
                        f" COALESCE(f.nome, fm.nome_sistema, e.fornecedor_excel, '') AS fornecedor, MAX(substr(COALESCE(e.report_date, ''), 1, 10)) AS data"
                        f" {SQL_BASE} GROUP BY e.grupo ORDER BY fornecedor, data DESC").fetchall()
    fornecedores = sorted({o["fornecedor"] for o in opcoes if o["fornecedor"]}, key=str.lower)
    return render_template("inspecoes_base.html", linhas=linhas, total=total, pecas=pecas, args=args, pagina=pagina,
                           opcoes_invoice=opcoes, opcoes_fornecedor=fornecedores, falhas_conhecidas=falhas_conhecidas(args.get("invoice") or None),
                           paginas=paginas, admin=admin, filtrado=any(args.values()))


@app.route("/invoices/<int:id>/desconto-ajuste", methods=["POST"])
@somente_admin
def desconto_ajuste(id):
    """Ajuste do % de C/D de um item ou do invoice inteiro (vazio = volta à política), sempre com motivo."""
    r = carregar_invoice(id)
    f = request.form
    motivo = " ".join(f.get("motivo", "").split())[:300]
    produto = (f.get("produto") or "").strip().upper() or None
    pc = parse_num(f.get("pct_c")) if (f.get("pct_c") or "").strip() else None
    pd = parse_num(f.get("pct_d")) if (f.get("pct_d") or "").strip() else None
    volta = redirect(url_for("inspecao_grupo", grupo=f"inv:{id}") + "#fechamento")
    if not motivo:
        flash("Informe o motivo do ajuste.", "erro")
        return volta
    for v in (pc, pd):
        if v is not None and not (0 <= v <= 100):
            flash("Os percentuais devem estar entre 0 e 100.", "erro")
            return volta
    get_db().execute("INSERT INTO desconto_ajustes (invoice_id, produto, pct_c, pct_d, motivo, criado_por, criado_em)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?)", (id, produto, pc, pd, motivo, g.usuario["id"], agora()))
    alvo = f"item {produto}" if produto else "todos os itens"
    txt = ("volta à política" if pc is None and pd is None else
           f"C {fmt_num(pc, 0) + '%' if pc is not None else 'política'} · D {fmt_num(pd, 0) + '%' if pd is not None else 'política'}")
    registrar_evento(id, f"Desconto: ajuste do % ({alvo})", f"{txt} · {motivo}")
    get_db().commit()
    flash(f"Ajuste salvo ({alvo}: {txt}).", "ok")
    return volta


@app.route("/invoices/<int:id>/desconto-versao", methods=["POST"])
@somente_admin
def desconto_versao(id):
    """Salva uma versão do cálculo (a 1ª vai para o fornecedor; as seguintes são revisões, com motivo).
    Se o inspecionado não bate com o invoice, pede confirmação antes."""
    r = carregar_invoice(id)
    calc = calcular_desconto_inspecao(r)
    db = get_db()
    ultima = db.execute("SELECT * FROM relatorios_versoes WHERE invoice_id = ? ORDER BY versao DESC LIMIT 1", (id,)).fetchone()
    motivo = " ".join(request.form.get("motivo", "").split())[:300]
    volta = redirect(url_for("inspecao_grupo", grupo=f"inv:{id}") + "#fechamento")
    if not calc["inspecionado"]:
        flash("Não há eventos de inspeção para calcular o desconto.", "erro")
        return volta
    if ultima and not motivo:
        flash("Informe o motivo da revisão (o que mudou em relação à versão anterior).", "erro")
        return volta
    if calc["divergencias"] and not request.form.get("confirmar"):
        return render_template("desconto_confirmar.html", r=r, calc=calc, motivo=motivo, versao=(ultima["versao"] + 1) if ultima else 1)
    linhas = [{k: l[k] for k in ("codigo", "descricao", "A", "B", "C", "D", "inspecionado", "qtd_invoice", "preco", "pct_c",
                                 "pct_d", "origem", "desconto")} for l in calc["linhas"]]
    versao = (ultima["versao"] + 1) if ultima else 1
    db.execute("INSERT INTO relatorios_versoes (invoice_id, versao, total, linhas, politica, motivo, divergencias, criado_por,"
               " criado_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (id, versao, calc["total"], json.dumps(linhas), calc["politica_txt"], motivo or None,
                json.dumps(calc["divergencias"]) if calc["divergencias"] else None, g.usuario["id"], agora()))
    registrar_evento(id, f"Relatório de desconto: versão {versao} calculada ({fmt_moeda(calc['total'], r['moeda'])})",
                     (motivo + " · " if motivo else "") + calc["politica_txt"]
                     + (f" · diferenças de quantidade confirmadas: {len(calc['divergencias'])}" if calc["divergencias"] else ""))
    db.commit()
    flash(f"Versão {versao} do desconto salva: {fmt_moeda(calc['total'], r['moeda'])}.", "ok")
    return volta


@app.route("/invoices/<int:id>/versoes/<int:versao>/excluir", methods=["POST"])
@somente_admin
def desconto_versao_excluir(id, versao):
    """v4.70: apaga a última versão do desconto (ex.: revisão feita como teste). Se ela já tinha sido registrada/enviada,
    desfaz o efeito no desconto do invoice: revisão → o desconto volta ao valor anterior; versão 1 → o desconto é apagado
    e o invoice volta a "Relatório de Inspeção Pendente". Não apaga se o desconto já foi usado em deduções ou liberações
    que o novo valor não comporta."""
    r = carregar_invoice(id)
    db = get_db()
    volta = redirect(url_for("inspecao_grupo", grupo=f"inv:{id}") + "#fechamento")
    v = db.execute("SELECT * FROM relatorios_versoes WHERE invoice_id = ? AND versao = ?", (id, versao)).fetchone()
    if v is None:
        abort(404)
    ultima = db.execute("SELECT MAX(versao) FROM relatorios_versoes WHERE invoice_id = ?", (id,)).fetchone()[0]
    if versao != ultima:
        flash(f"Só a última versão (v{ultima}) pode ser apagada.", "erro")
        return volta
    d = r["desconto"]
    apagar = excluir_arquivos_de("relatorio_versao", v["id"])
    detalhe = f"v{versao} · {fmt_moeda(v['total'], r['moeda'])}" + (f" · {v['motivo']}" if v["motivo"] else "")
    if v["aplicado_em"]:
        if versao == 1 or not d:
            if d:
                if r["deduzido"] > TOLERANCIA or r["liberado"] > TOLERANCIA:
                    flash("O desconto desta versão já foi deduzido em outro invoice ou teve valor segurado liberado: "
                          "desfaça isso antes de apagar a versão.", "erro")
                    return volta
                for x in db.execute("SELECT id FROM revisoes_desconto WHERE desconto_id = ?", (d["id"],)).fetchall():
                    apagar += excluir_arquivos_de("revisao", x["id"])
                db.execute("DELETE FROM revisoes_desconto WHERE desconto_id = ?", (d["id"],))
                apagar += excluir_arquivos_de("relatorio", d["id"])
                db.execute("DELETE FROM descontos WHERE id = ?", (d["id"],))
            db.execute("UPDATE invoices SET tem_desconto = NULL WHERE id = ?", (id,))
            detalhe += " · o desconto criado por ela foi apagado; o invoice volta a aguardar o relatório de inspeção"
        else:
            rev = db.execute("SELECT * FROM revisoes_desconto WHERE desconto_id = ? ORDER BY id DESC LIMIT 1",
                             (d["id"],)).fetchone()
            if rev and abs(rev["valor_novo"] - d["valor_final"]) <= TOLERANCIA and abs(rev["valor_novo"] - v["total"]) <= TOLERANCIA:
                anterior = rev["valor_anterior"]
                if anterior < r["deduzido"] - TOLERANCIA:
                    flash(f"O valor anterior ({fmt_moeda(anterior, r['moeda'])}) é menor que o já deduzido em outros invoices "
                          f"({fmt_moeda(r['deduzido'], r['moeda'])}): desfaça as deduções antes.", "erro")
                    return volta
                segurado = min(d["valor_segurado"], anterior)
                if r["principal_pago"] < r["principal_devido"] - TOLERANCIA or r["principal_pago"] <= TOLERANCIA:
                    segurado = max(segurado_possivel(r, anterior), r["liberado"])
                db.execute("UPDATE descontos SET valor_final = ?, valor_segurado = ? WHERE id = ?", (anterior, segurado, d["id"]))
                apagar += excluir_arquivos_de("revisao", rev["id"])
                db.execute("DELETE FROM revisoes_desconto WHERE id = ?", (rev["id"],))
                detalhe += f" · desconto voltou de {fmt_moeda(d['valor_final'], r['moeda'])} para {fmt_moeda(anterior, r['moeda'])}"
            elif v["arquivo_id"]:  # a versão não mudou o valor: só tinha anexado o relatório ao desconto
                nome_arq = db.execute("SELECT nome FROM arquivos WHERE id = ?", (v["arquivo_id"],)).fetchone()
                if nome_arq:
                    for a in db.execute("SELECT * FROM arquivos WHERE origem = 'relatorio' AND origem_id = ? AND nome = ?",
                                        (d["id"], nome_arq["nome"])).fetchall():
                        apagar.append(a)
                        db.execute("DELETE FROM arquivos WHERE id = ?", (a["id"],))
    db.execute("DELETE FROM relatorios_versoes WHERE id = ?", (v["id"],))
    registrar_evento(id, f"Versão {versao} do desconto apagada", detalhe)
    db.commit()
    remover_arquivos(apagar)
    flash(f"Versão {versao} apagada." + (" O desconto do invoice foi ajustado." if v["aplicado_em"] else ""), "ok")
    return volta


# ---------------------------------------------------------------------------
# Relatório ao fornecedor (v4.61 — fase 3): Excel em inglês + e-mail com prévia; ao enviar, a versão vira o desconto
# do invoice (versão 1: desconto com o relatório anexado; revisões: revalidação com o relatório revisado)
# ---------------------------------------------------------------------------
CARGO_ASSINATURA_PADRAO = "Suppliers Development Analyst"
CID_ASSINATURA = "assinatura-quinelato"


def imagem_assinatura():
    aid = config("assinatura_imagem")
    if not aid or not str(aid).isdigit():
        return None
    a = get_db().execute("SELECT * FROM arquivos WHERE id = ?", (int(aid),)).fetchone()
    return a if a is not None and os.path.exists(caminho_anexo(a)) else None


def salvar_arquivo_bytes(origem, origem_id, nome, conteudo):
    os.makedirs(ARQUIVOS_DIR, exist_ok=True)
    ext = nome.rsplit(".", 1)[-1].lower()
    gravado = f"{origem}-{origem_id}-{secrets.token_hex(8)}.{ext}"
    caminho = os.path.join(ARQUIVOS_DIR, gravado)
    with open(caminho, "wb") as saida:
        saida.write(conteudo)
    aid = get_db().execute("INSERT INTO arquivos (origem, origem_id, nome, arquivo, tamanho, enviado_por, enviado_em)"
                           " VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (origem, origem_id, nome, gravado, len(conteudo), g.usuario["id"], agora())).lastrowid
    return aid, caminho


def dados_relatorio(r, v):
    """Tudo o que o Excel e o e-mail precisam: eventos com o defeito em inglês (categoria final), itens e textos."""
    db = get_db()
    glos = glossario()
    eventos, pendentes = [], set()
    nome_mega = None
    for e in db.execute("SELECT * FROM inspecao_eventos WHERE invoice_id = ? ORDER BY produto, ordem, id", (r["id"],)):
        en, falta = insp.traduzir(e["comentario"], glos)
        if falta and exige_traducao(e):
            pendentes.update(falta)
        nome_mega = nome_mega or e["fornecedor_excel"]
        eventos.append({"produto": e["produto"], "descricao": e["descricao"], "quantidade": e["quantidade"] or 0,
                        "categoria": e["categoria"], "en": en if en else (e["comentario"] or "").strip(" ;"),
                        "report_date": e["report_date"], "lote": e["lote"], "family": e["family"], "numero_nf": e["numero_nf"]})
    itens = {}
    for x in r["itens"]:
        k = (x["codigo"] or "").strip().upper()
        it = itens.setdefault(k, {"codigo": (x["codigo"] or "").strip(), "descricao": x["item"], "quantidade": 0, "preco": x["valor_unit"]})
        it["quantidade"] += x["quantidade"] or 0
    # v4.63: em todo o relatório (e-mail, assunto, Excel e nome do arquivo) vale o nome do fornecedor cadastrado no sistema
    info = {"fornecedor": r["fornecedor"], "fornecedor_curto": r["fornecedor"], "invoice": r["numero"], "moeda": r["moeda"],
            "versao": v["versao"], "data": hoje()}
    return info, json.loads(v["linhas"]), eventos, list(itens.values()), sorted(pendentes)


def chave_natural(t):
    """'INV-10' depois de 'INV-9' (números comparados como números)."""
    return [(0, int(p), "") if p.isdigit() else (1, 0, p.lower()) for p in re.split(r"(\d+)", t) if p]


def historico_qualidade(r, n=5):
    """v4.67: desenvolvimento da qualidade do fornecedor nos últimos n invoices inspecionados (o atual é o último).
    Usa a base de inspeção inteira: invoices do sistema e os que só existem no Excel do MEGA (pela relação de nomes
    MEGA × sistema). Categoria final de cada evento. Devolve [{invoice, data, A, B, C, D, total, atual}]."""
    db = get_db()
    grupos = {}
    for e in db.execute(
            "SELECT e.grupo, e.invoice_id, e.invoice_excel, e.fornecedor_excel, e.numero_nf, e.categoria, e.quantidade,"
            " substr(e.report_date, 1, 10) AS data,"
            " i.numero FROM inspecao_eventos e LEFT JOIN invoices i ON i.id = e.invoice_id"
            " LEFT JOIN fornecedor_mega fm ON fm.nome_mega = trim(e.fornecedor_excel)"
            " WHERE (i.fornecedor_id = ? AND i.cancelado_em IS NULL) OR (e.invoice_id IS NULL"
            " AND lower(trim(COALESCE(fm.nome_sistema, e.fornecedor_excel))) = lower(trim(?)))",
            (r["fornecedor_id"], r["fornecedor"])):
        gr = grupos.setdefault(e["grupo"], {"invoice": e["numero"] or insp.rotulo_invoice_excel(
                                                e["invoice_excel"], e["fornecedor_excel"], r["fornecedor"]) or e["grupo"],
                                            "data": None, "A": 0, "B": 0, "C": 0, "D": 0, "atual": e["invoice_id"] == r["id"]})
        if e["categoria"] in ("A", "B", "C", "D"):
            gr[e["categoria"]] += e["quantidade"] or 0
        if e["data"] and (gr["data"] is None or e["data"] > gr["data"]):
            gr["data"] = e["data"]
        # o Excel às vezes traz só o nome do fornecedor no lugar do invoice: aí identifica pela NF de entrada
        if e["numero_nf"] and not e["numero"] and insp.chave_termo(gr["invoice"]).replace(" ", "") in {
                insp.chave_termo(e["fornecedor_excel"]).replace(" ", ""), insp.chave_termo(r["fornecedor"]).replace(" ", "")}:
            gr["invoice"] = f"NF {e['numero_nf']}"
    lista = [x for x in grupos.values() if x["A"] + x["B"] + x["C"] + x["D"] > 0]
    for x in lista:
        x["total"] = x["A"] + x["B"] + x["C"] + x["D"]
    atual = next((x for x in lista if x["atual"]), None)
    if atual is None:
        return []
    # o invoice do relatório é sempre o último: só entram os inspecionados antes dele (nunca os posteriores, nem
    # num relatório revisado depois). Mesma data de inspeção: vale a ordem do número do invoice (01 antes de 02).
    def ordem(x):
        return (x["data"] or "", chave_natural(str(x["invoice"])))
    anteriores = sorted([x for x in lista if not x["atual"] and ordem(x) < ordem(atual)], key=ordem)
    return anteriores[-(n - 1):] + [atual] if n > 1 else [atual]


def versao_confere(r, v):
    """A versão salva ainda corresponde aos eventos e ao cálculo atuais?"""
    atual = {l["codigo"]: (l["C"], l["D"], round(l["desconto"], 2)) for l in calcular_desconto_inspecao(r)["linhas"]}
    salva = {l["codigo"]: (l["C"], l["D"], round(l["desconto"], 2)) for l in json.loads(v["linhas"])}
    return atual == salva


def aplicar_versao_no_desconto(r, v, conteudo, nome):
    """Lança a versão enviada no desconto do invoice. Devolve uma mensagem de erro ou None."""
    db = get_db()
    total = round(v["total"], 2)
    d = r["desconto"]
    # v4.71: ao enviar/registrar o relatório, o fim da inspeção passa a ser a última REPORT DATE dos eventos do invoice
    data_insp = db.execute("SELECT MAX(substr(report_date, 1, 10)) FROM inspecao_eventos WHERE invoice_id = ?",
                           (r["id"],)).fetchone()[0] or r["data_fim_inspecao"] or hoje()
    if data_insp != r["data_fim_inspecao"]:
        db.execute("UPDATE invoices SET data_fim_inspecao = ? WHERE id = ?", (data_insp, r["id"]))
        if r["data_fim_inspecao"]:
            registrar_evento(r["id"], "Fim da inspeção ajustado para a última REPORT DATE",
                             f"{fmt_data(r['data_fim_inspecao'])} → {fmt_data(data_insp)}")
    if not d:
        if total <= TOLERANCIA:
            db.execute("UPDATE invoices SET data_fim_inspecao = ?, tem_desconto = 0 WHERE id = ?", (data_insp, r["id"]))
            registrar_evento(r["id"], "Inspeção registrada: sem desconto (relatório ao fornecedor)", f"Versão {v['versao']}")
            return None
        if total > r["valor_invoice"] + TOLERANCIA:
            return "O desconto calculado é maior que o valor do invoice."
        segurado = segurado_possivel(r, total)
        did = db.execute("INSERT INTO descontos (invoice_id, valor_inicial, valor_final, valor_segurado, data_relatorio, status,"
                         " obs, criado_por, criado_em) VALUES (?, ?, ?, ?, ?, 'ENVIADO', ?, ?, ?)",
                         (r["id"], total, total, segurado, hoje(), f"Relatório de inspeção · versão {v['versao']}",
                          g.usuario["id"], agora())).lastrowid
        db.execute("UPDATE invoices SET data_fim_inspecao = ?, tem_desconto = 1 WHERE id = ?", (data_insp, r["id"]))
        salvar_arquivo_bytes("relatorio", did, nome, conteudo)
        registrar_evento(r["id"], "Inspeção registrada com desconto · Enviado ao fornecedor",
                         f"Valor: {fmt_moeda(total, r['moeda'])} · segurado: {fmt_moeda(segurado, r['moeda'])} · relatório: {nome}")
        return None
    if abs(total - d["valor_final"]) > TOLERANCIA:  # revisão: revalidação do desconto com o relatório revisado
        if total < r["deduzido"] - TOLERANCIA:
            return f"O novo valor é menor que o já deduzido em outros invoices ({fmt_moeda(r['deduzido'], r['moeda'])})."
        if total > r["valor_invoice"] + TOLERANCIA:
            return "O desconto calculado é maior que o valor do invoice."
        segurado = d["valor_segurado"]
        if r["principal_pago"] < r["principal_devido"] - TOLERANCIA or r["principal_pago"] <= TOLERANCIA:
            segurado = max(segurado_possivel(r, total), r["liberado"])
        motivo = v["motivo"] or f"Relatório de inspeção · versão {v['versao']}"
        vid = db.execute("INSERT INTO revisoes_desconto (desconto_id, valor_anterior, valor_novo, motivo, usuario_id, criado_em)"
                         " VALUES (?, ?, ?, ?, ?, ?)", (d["id"], d["valor_final"], total, motivo, g.usuario["id"], agora())).lastrowid
        db.execute("UPDATE descontos SET valor_final = ?, valor_segurado = ? WHERE id = ?", (total, segurado, d["id"]))
        salvar_arquivo_bytes("revisao", vid, nome, conteudo)
        registrar_evento(r["id"], f"Desconto revalidado: {fmt_moeda(d['valor_final'], r['moeda'])} → {fmt_moeda(total, r['moeda'])}",
                         f"{motivo} · relatório revisado: {nome}")
    else:
        salvar_arquivo_bytes("relatorio", d["id"], nome, conteudo)
    if d["status"] == "PENDENTE":
        alterar_status_desconto(r, d, "ENVIADO")
    return None


@app.route("/invoices/<int:id>/relatorio/<int:versao>", methods=["GET", "POST"])
@somente_admin
def relatorio_fornecedor(id, versao):
    """Prévia do Excel e do e-mail ao fornecedor; o envio só acontece depois da validação na prévia."""
    r = carregar_invoice(id)
    db = get_db()
    v = db.execute("SELECT * FROM relatorios_versoes WHERE invoice_id = ? AND versao = ?", (id, versao)).fetchone()
    if v is None:
        abort(404)
    volta = redirect(url_for("inspecao_grupo", grupo=f"inv:{id}") + "#fechamento")
    ultima = db.execute("SELECT MAX(versao) FROM relatorios_versoes WHERE invoice_id = ?", (id,)).fetchone()[0]
    info, linhas, eventos, itens, pendentes = dados_relatorio(r, v)
    bloqueios = []
    if pendentes:
        bloqueios.append("Termos sem tradução: " + ", ".join(pendentes) + ". Traduza antes de enviar o relatório.")
    if versao != ultima:
        bloqueios.append(f"Esta não é a última versão (v{ultima}).")
    if v["aplicado_em"]:
        bloqueios.append(f"Esta versão já foi enviada em {fmt_data(v['aplicado_em'])}.")
    if not versao_confere(r, v):
        bloqueios.append("Os eventos ou o cálculo mudaram depois desta versão: salve uma nova versão antes de enviar.")
    forn = db.execute("SELECT * FROM fornecedores WHERE id = ?", (r["fornecedor_id"],)).fetchone()
    f = request.form if request.method == "POST" else {}
    total_txt = f"{r['moeda']} {rel.num_br(v['total'], 2)}"
    # invoice já pago inteiro: nada fica retido nele, o desconto vai todo para o próximo invoice
    pago_integral = r["principal_pago"] >= r["valor_invoice"] - TOLERANCIA and r["principal_pago"] > TOLERANCIA
    padrao = rel.textos_padrao(info, v["total"], total_txt, pago_integral)
    textos = {k: (f.get(k) if f.get(k) is not None else padrao[k]) for k in padrao}
    campos = {
        "para": f.get("para", forn["emails_relatorio"] or ""),
        "cc": f.get("cc", config("relatorio_cc", "") or ""),
        "assunto": f.get("assunto", f"Inspection Results {info['invoice']} - {info['fornecedor_curto']}"
                         + (f" (Revision {versao})" if versao > 1 else "")),
        "link": f.get("link", ""),
        "cargo": f.get("cargo", config("relatorio_cargo", CARGO_ASSINATURA_PADRAO)),
        "nome": f.get("nome", config(f"relatorio_nome_{g.usuario['id']}", g.usuario["nome"])),
    }
    # imagem da assinatura da empresa (enviada uma vez; vai embutida no e-mail)
    if request.method == "POST" and request.files.get("imagem_assinatura") and request.files["imagem_assinatura"].filename:
        arq = request.files["imagem_assinatura"]
        ext = arq.filename.rsplit(".", 1)[-1].lower()
        if ext not in ("png", "jpg", "jpeg", "gif"):
            flash("A imagem da assinatura deve ser PNG, JPG ou GIF.", "erro")
        else:
            aid_img, _c = salvar_arquivo_bytes("assinatura", 0, "assinatura." + ext, arq.read())
            salvar_config("assinatura_imagem", str(aid_img))
            db.commit()
            flash("Imagem da assinatura salva: vai em todos os relatórios.", "ok")
    img = imagem_assinatura()
    if request.method == "POST" and f.get("remover_imagem"):
        salvar_config("assinatura_imagem", None)
        db.commit()
        img = None
    historico = historico_qualidade(r)  # v4.67: desenvolvimento nos últimos 5 invoices
    html_previa = rel.corpo_email(info, eventos, textos, campos["link"].strip(), campos["nome"], campos["cargo"],
                                  url_for("arquivo_baixar", aid=img["id"]) if img else None, historico)
    html = rel.corpo_email(info, eventos, textos, campos["link"].strip(), campos["nome"], campos["cargo"],
                           "cid:" + CID_ASSINATURA if img else None, historico)
    nome = rel.nome_arquivo(info)
    acao = f.get("acao") if request.method == "POST" else None
    if acao == "baixar":
        conteudo = rel.gerar_excel(info, linhas, eventos, itens)
        return send_file(io.BytesIO(conteudo), download_name=nome, as_attachment=True,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if acao == "outlook":  # v4.69: abre no Outlook do computador, com a conta logada lá
        if bloqueios:
            for b in bloqueios:
                flash(b, "erro")
            return volta
        conteudo = rel.gerar_excel(info, linhas, eventos, itens)
        eml = rascunho_outlook(campos["assunto"], html, campos["para"], campos["cc"], [(nome, conteudo)])
        if lista_emails(campos["para"]):
            db.execute("UPDATE fornecedores SET emails_relatorio = ? WHERE id = ?", (campos["para"], r["fornecedor_id"]))
        salvar_config("relatorio_cc", campos["cc"] or None)
        salvar_config("relatorio_cargo", campos["cargo"] or None)
        salvar_config(f"relatorio_nome_{g.usuario['id']}", campos["nome"] or None)
        db.commit()
        arquivo = "".join(ch for ch in campos["assunto"] if ch not in '\\/:*?"<>|')[:120] + ".eml"
        return send_file(io.BytesIO(eml), download_name=arquivo, as_attachment=True, mimetype="message/rfc822")
    if acao in ("enviar", "registrar"):
        if bloqueios:
            for b in bloqueios:
                flash(b, "erro")
            return volta
        if acao == "enviar" and (not lista_emails(campos["para"]) or not conta_configurada()):
            flash("Informe o e-mail do fornecedor em 'Para'." if not lista_emails(campos["para"])
                  else "Conta de envio de e-mails não configurada (aba E-mails).", "erro")
        else:
            conteudo = rel.gerar_excel(info, linhas, eventos, itens)
            aid, caminho = salvar_arquivo_bytes("relatorio_versao", v["id"], nome, conteudo)
            erro = aplicar_versao_no_desconto(r, v, conteudo, nome)
            if erro:
                db.rollback()
                flash(erro, "erro")
                return volta
            email_id = None
            if acao == "enviar":
                email_id = db.execute("INSERT INTO emails (fluxo, invoice_id, assunto, corpo, para, cc, status, criado_em, anexos)"
                                      " VALUES ('relatorio_fornecedor', ?, ?, ?, ?, ?, 'PENDENTE', ?, ?)",
                                      (id, campos["assunto"], html, campos["para"], campos["cc"], agora(),
                                       json.dumps([[caminho, nome]], ensure_ascii=False))).lastrowid
                db.execute("UPDATE fornecedores SET emails_relatorio = ? WHERE id = ?", (campos["para"], r["fornecedor_id"]))
                salvar_config("relatorio_cc", campos["cc"] or None)
            salvar_config("relatorio_cargo", campos["cargo"] or None)
            salvar_config(f"relatorio_nome_{g.usuario['id']}", campos["nome"] or None)
            db.execute("UPDATE relatorios_versoes SET aplicado_em = ?, email_id = ?, arquivo_id = ?, enviado_por = ? WHERE id = ?",
                       (agora(), email_id, aid, g.usuario["id"], v["id"]))
            registrar_evento(id, f"Relatório de inspeção v{versao} " + ("enviado ao fornecedor" if acao == "enviar"
                                                                      else "registrado (enviado por fora do sistema)"),
                             (f"Para: {campos['para']}" + (f" · cc: {campos['cc']}" if campos["cc"] else "") + " · " if email_id else "")
                             + f"desconto {fmt_moeda(v['total'], r['moeda'])} · {nome}")
            db.commit()
            if email_id:
                enviar_fila()
                em = db.execute("SELECT status, erro FROM emails WHERE id = ?", (email_id,)).fetchone()
                if em["status"] == "ENVIADO":
                    flash(f"Relatório v{versao} enviado para {campos['para']}. O desconto do invoice foi atualizado.", "ok")
                else:
                    flash(f"Relatório registrado, mas o e-mail não saiu agora ({em['erro'] or 'falha'}); o sistema tenta de novo."
                          " Veja a aba E-mails.", "erro")
            else:
                flash(f"Relatório v{versao} registrado como enviado por fora do sistema. O desconto do invoice foi atualizado.", "ok")
            return volta
    return render_template("relatorio_previa.html", r=r, v=v, info=info, html=html_previa, campos=campos, textos=textos, img=img,
                           pago_integral=pago_integral,
                           bloqueios=bloqueios, nome=nome, conta_ok=conta_configurada(), eventos=len(eventos))


@app.route("/politicas-desconto", methods=["GET", "POST"])
@login_obrigatorio
def politicas_desconto():
    db = get_db()
    admin = g.usuario["perfil"] == "admin"
    if request.method == "POST":
        if not admin:
            abort(403)
        f = request.form
        fid = f.get("fornecedor_id", type=int)
        if f.get("acao") == "remover" and fid:
            db.execute("DELETE FROM politicas_desconto WHERE fornecedor_id = ?", (fid,))
            db.commit()
            flash("O fornecedor voltou a usar a política padrão.", "ok")
            return redirect(url_for("politicas_desconto"))
        tipo = f.get("tipo") if f.get("tipo") in ("FIXA", "FAIXAS") else "FIXA"
        pc, pd = parse_num(f.get("pct_c")), parse_num(f.get("pct_d"))
        faixas = []
        for minimo, pct in zip(f.getlist("faixa_min"), f.getlist("faixa_pct")):
            a, b = parse_num(minimo), parse_num(pct)
            if a is not None and b is not None:
                faixas.append([a, b])
        if pd is None or (tipo == "FIXA" and pc is None) or (tipo == "FAIXAS" and not faixas):
            flash("Preencha os percentuais da política.", "erro")
            return redirect(url_for("politicas_desconto"))
        if tipo == "FAIXAS" and not any(m == 0 for m, _p in faixas):
            faixas.append([0, faixas[-1][1]])
        db.execute("INSERT INTO politicas_desconto (fornecedor_id, tipo, pct_c, pct_d, faixas, atualizado_por, atualizado_em)"
                   " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(fornecedor_id) DO UPDATE SET tipo = excluded.tipo,"
                   " pct_c = excluded.pct_c, pct_d = excluded.pct_d, faixas = excluded.faixas,"
                   " atualizado_por = excluded.atualizado_por, atualizado_em = excluded.atualizado_em",
                   (fid or 0, tipo, pc if tipo == "FIXA" else None, pd, json.dumps(sorted(faixas, key=lambda x: -x[0])) if tipo == "FAIXAS" else None,
                    g.usuario["id"], agora()))
        db.commit()
        flash("Política de desconto salva.", "ok")
        return redirect(url_for("politicas_desconto"))
    pols = []
    for p in db.execute("SELECT p.*, f.nome FROM politicas_desconto p LEFT JOIN fornecedores f ON f.id = p.fornecedor_id"
                        " ORDER BY p.fornecedor_id <> 0, f.nome"):
        d = dict(p)
        d["faixas"] = json.loads(p["faixas"]) if p["faixas"] else []
        d["texto"] = descrever_politica(d)
        pols.append(d)
    fornecedores = db.execute("SELECT id, nome FROM fornecedores WHERE id NOT IN (SELECT fornecedor_id FROM politicas_desconto)"
                              " ORDER BY nome").fetchall()
    return render_template("politicas_desconto.html", pols=pols, fornecedores=fornecedores, admin=admin,
                           FAIXAS_ORTHOGENT=FAIXAS_ORTHOGENT)


@app.route("/traducoes", methods=["GET", "POST"])
@login_obrigatorio
def traducoes():
    """Glossário PT -> EN por termo. Os termos pendentes (usados nos eventos e ainda sem tradução) ficam no topo."""
    from collections import Counter
    db = get_db()
    admin = g.usuario["perfil"] == "admin"
    if request.method == "POST":
        if not admin:
            abort(403)
        f = request.form
        n = 0
        for chave, en in zip(f.getlist("chave"), f.getlist("texto_en")):
            chave, en = insp.chave_termo(chave), " ".join(en.split())[:200]
            if not chave:
                continue
            if f.get("acao") == "excluir":
                continue
            if en:
                db.execute("INSERT INTO traducao_termos (chave, texto_pt, texto_en, origem, atualizado_por, atualizado_em)"
                           " VALUES (?, ?, ?, 'sistema', ?, ?) ON CONFLICT(chave) DO UPDATE SET texto_en = excluded.texto_en,"
                           " origem = 'sistema', atualizado_por = excluded.atualizado_por, atualizado_em = excluded.atualizado_em",
                           (chave, chave.capitalize(), en, g.usuario["id"], agora()))
                n += 1
        if f.get("acao") == "excluir" and f.get("excluir"):
            db.execute("DELETE FROM traducao_termos WHERE chave = ?", (f.get("excluir"),))
            db.commit()
            flash("Termo excluído.", "ok")
            return redirect(url_for("traducoes", aba="cadastrados"))
        db.commit()
        flash(f"{n} tradução(ões) salva(s)." if n else "Nenhuma tradução preenchida.", "ok" if n else "erro")
        return redirect(url_for("traducoes", aba=request.args.get("aba", "pendentes"), q=request.args.get("q", "")))
    glos = glossario()
    uso, exemplos, ultimo, invoices_termo = Counter(), {}, {}, {}
    for e in db.execute("SELECT grupo, comentario, data_inclusao, report_date FROM inspecao_eventos"):
        if not exige_traducao(e):  # antes da padronização dos termos: não conta como pendência
            continue
        for original, k in insp.termos(e["comentario"]):
            if k in insp.SEM_DEFEITO:
                continue
            uso[k] += 1
            exemplos.setdefault(k, original)
            ultimo[k] = max(ultimo.get(k, ""), e["data_inclusao"] or "")
            invoices_termo.setdefault(k, set()).add(e["grupo"])
    busca = insp.chave_termo(request.args.get("q", ""))
    periodo = request.args.get("periodo", "180")
    desde = somar_dias(hoje(), -int(periodo)) if periodo.isdigit() else ""
    pendentes = [{"chave": k, "exemplo": exemplos[k], "uso": n, "ultimo": ultimo[k], "invoices": len(invoices_termo[k])}
                 for k, n in uso.items() if k not in glos and (not busca or busca in k) and ultimo[k][:10] >= desde]
    pendentes.sort(key=lambda x: (x["uso"], x["ultimo"]), reverse=True)  # os mais usados primeiro
    cadastrados = [dict(t) | {"uso": uso.get(t["chave"], 0)} for t in db.execute(
        "SELECT t.*, u.nome AS usuario FROM traducao_termos t LEFT JOIN usuarios u ON u.id = t.atualizado_por"
        " ORDER BY t.chave") if not busca or busca in t["chave"]]
    return render_template("traducoes.html", pendentes=pendentes[:300], total_pendentes=len(pendentes), periodo=periodo,
                           cadastrados=cadastrados, aba=request.args.get("aba", "pendentes"), admin=admin,
                           q=request.args.get("q", ""))


@app.route("/descontos")
@login_obrigatorio
def descontos():
    ids = [x[0] for x in get_db().execute(
        "SELECT d.invoice_id FROM descontos d JOIN invoices i ON i.id = d.invoice_id ORDER BY d.id DESC")]
    lista = [carregar_invoice(i) for i in ids]
    filtro = request.args.get("f", "abertos")
    avulsos = avulsos_de()
    for a in avulsos:
        a["deducoes"] = get_db().execute(
            "SELECT x.*, i.numero, i.cancelado_em FROM deducoes_avulsas x JOIN invoices i ON i.id = x.invoice_id"
            " WHERE x.avulso_id = ? ORDER BY x.id", (a["id"],)).fetchall()
    if filtro == "avulsos":
        return render_template("descontos.html", lista=[], filtro=filtro, avulsos=avulsos,
                               fornecedores_lista=get_db().execute("SELECT id, nome FROM fornecedores WHERE ativo = 1"
                                                                   " ORDER BY nome").fetchall())
    # v4.46: invoices com o fim da inspeção registrado e o resultado ainda não definido (relatório pendente)
    pendentes_rel = [carregar_invoice(x[0]) for x in get_db().execute(
        "SELECT id FROM invoices WHERE data_fim_inspecao IS NOT NULL AND tem_desconto IS NULL AND cancelado_em IS NULL"
        " ORDER BY data_fim_inspecao")]
    if filtro == "abertos":
        lista = pendentes_rel + [r for r in lista if r["situacao"] not in ("CONCLUIDO", "CANCELADO")]
    elif filtro == "RELATORIO":
        lista = pendentes_rel
    elif filtro in STATUS_DESCONTO:
        lista = [r for r in lista if r["desconto"]["status"] == filtro]
    else:
        lista = pendentes_rel + lista
    return render_template("descontos.html", lista=lista, filtro=filtro, avulsos=avulsos)


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
        "SELECT f.*, (SELECT COUNT(*) FROM invoices i WHERE i.fornecedor_id = f.id) AS qtd,"
        " (SELECT COUNT(*) FROM fornecedor_lembretes l WHERE l.fornecedor_id = f.id AND l.ok_em IS NULL) AS lembretes"
        " FROM fornecedores f ORDER BY f.ativo DESC, f.nome"
    ).fetchall()
    condicoes = {f["id"]: condicao_texto({"condicao": condicao_fornecedor(f["id"])}) for f in lista}
    return render_template("fornecedores.html", fornecedores=lista, pode_editar=pode_editar, condicoes=condicoes)


# ---------------------------------------------------------------------------
# Configuração do fornecedor: condição de pagamento padrão e lembretes para o próximo invoice
# ---------------------------------------------------------------------------
def condicao_fornecedor(fid):
    return [dict(c) for c in get_db().execute(
        "SELECT percentual, tipo, dias FROM fornecedor_condicao WHERE fornecedor_id = ? ORDER BY ordem", (fid,))]


def lembretes_ativos(fid):
    return get_db().execute(
        "SELECT l.*, u.nome AS autor FROM fornecedor_lembretes l LEFT JOIN usuarios u ON u.id = l.criado_por"
        " WHERE l.fornecedor_id = ? AND l.ok_em IS NULL ORDER BY l.id", (fid,)).fetchall()


@app.route("/fornecedores/<int:fid>", methods=["GET", "POST"])
@login_obrigatorio
def fornecedor_config(fid):
    db = get_db()
    f = db.execute("SELECT * FROM fornecedores WHERE id = ?", (fid,)).fetchone()
    if f is None:
        abort(404)
    pode_editar = pode("importacao")
    if request.method == "POST":
        if not pode_editar:
            abort(403)
        acao = request.form.get("acao")
        if acao == "condicao":
            cond, erros = [], []
            for pct, tipo, dias in zip(request.form.getlist("cond_pct"), request.form.getlist("cond_tipo"),
                                       request.form.getlist("cond_dias") + [""] * 20):
                p = parse_num(pct)
                if p is None:
                    continue
                if tipo not in CONDICOES or p <= 0 or p > 100:
                    erros.append("Percentual ou tipo inválido.")
                elif tipo == "PRAZO" and not dias.strip().isdigit():
                    erros.append(f"Informe os dias após a HAWB da parcela de {fmt_qtd(p)}%.")
                else:
                    cond.append((p, tipo, int(dias) if tipo == "PRAZO" else None))
            if cond and abs(sum(c[0] for c in cond) - 100) > 0.01:
                erros.append("A condição precisa somar 100%.")
            if erros:
                for e in erros:
                    flash(e, "erro")
            else:
                db.execute("DELETE FROM fornecedor_condicao WHERE fornecedor_id = ?", (fid,))
                for ordem, (p, tipo, dias) in enumerate(cond, 1):
                    db.execute("INSERT INTO fornecedor_condicao (fornecedor_id, ordem, percentual, tipo, dias)"
                               " VALUES (?, ?, ?, ?, ?)", (fid, ordem, p, tipo, dias))
                db.commit()
                flash("Condição de pagamento padrão salva." if cond else "Condição padrão removida.", "ok")
            return redirect(url_for("fornecedor_config", fid=fid) + "#condicao")
        if acao == "lembrete":
            texto = request.form.get("texto", "").strip()[:1000]
            if not texto:
                flash("Escreva o texto do lembrete.", "erro")
            else:
                db.execute("INSERT INTO fornecedor_lembretes (fornecedor_id, texto, criado_por, criado_em)"
                           " VALUES (?, ?, ?, ?)", (fid, texto, g.usuario["id"], agora()))
                db.commit()
                flash("Lembrete cadastrado: vai aparecer ao criar o próximo invoice deste fornecedor.", "ok")
            return redirect(url_for("fornecedor_config", fid=fid) + "#lembretes")
        if acao == "excluir_lembrete":
            db.execute("DELETE FROM fornecedor_lembretes WHERE id = ? AND fornecedor_id = ?",
                       (request.form.get("lid", type=int), fid))
            db.commit()
            flash("Lembrete excluído.", "ok")
            return redirect(url_for("fornecedor_config", fid=fid) + "#lembretes")
        abort(400)
    historico = db.execute(
        "SELECT l.*, u.nome AS autor, o.nome AS quem_ok FROM fornecedor_lembretes l"
        " LEFT JOIN usuarios u ON u.id = l.criado_por LEFT JOIN usuarios o ON o.id = l.ok_por"
        " WHERE l.fornecedor_id = ? AND l.ok_em IS NOT NULL ORDER BY l.ok_em DESC LIMIT 30", (fid,)).fetchall()
    return render_template("fornecedor_config.html", f=f, condicao=condicao_fornecedor(fid),
                           lembretes=lembretes_ativos(fid), historico=historico, pode_editar=pode_editar)


@app.route("/api/fornecedor-info")
@login_obrigatorio
def api_fornecedor_info():
    """Condição padrão e lembretes ativos do fornecedor escolhido na criação do invoice."""
    fid = obter_fornecedor(" ".join(request.args.get("nome", "").split()), criar=False)
    if not fid:
        return jsonify({"condicao": [], "lembretes": []})
    return jsonify({
        "condicao": condicao_fornecedor(fid),
        "lembretes": [{"id": l["id"], "texto": l["texto"], "autor": l["autor"] or "—",
                       "em": fmt_data(l["criado_em"])} for l in lembretes_ativos(fid)],
    })


@app.route("/api/lembretes/<int:lid>/ok", methods=["POST"])
@login_obrigatorio
def api_lembrete_ok(lid):
    """OK no lembrete: ele não aparece mais nos próximos invoices."""
    db = get_db()
    if db.execute("SELECT 1 FROM fornecedor_lembretes WHERE id = ? AND ok_em IS NULL", (lid,)).fetchone() is None:
        return jsonify({"ok": False}), 404
    db.execute("UPDATE fornecedor_lembretes SET ok_em = ?, ok_por = ? WHERE id = ?", (agora(), g.usuario["id"], lid))
    db.commit()
    return jsonify({"ok": True})


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
# ---------------------------------------------------------------------------
# v4.66: Relatórios da importação (indicadores, itens por situação, importações por canal, mensal e semanal).
# Tudo é calculado a partir das datas dos processos, na hora: a tela, o Excel e o PDF usam as mesmas funções
# (relatorios_imp) e cada download recalcula com os dados atuais.
# ---------------------------------------------------------------------------
def _baixar(conteudo, nome, mime):
    return send_file(io.BytesIO(conteudo), download_name=nome, as_attachment=True, mimetype=mime)


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _pdf_disponivel():
    try:
        import reportlab  # noqa: F401
        return True
    except ImportError:
        flash("A biblioteca de PDF (reportlab) ainda não está instalada no servidor. Em Sistema, use "
              "\"Verificar e instalar atualização\" ou rode: pip install --user reportlab", "erro")
        return False


def _gerado_em():
    return agora_dt().strftime("%d/%m/%Y %H:%M")


def _periodo_args(padrao="mes_anterior"):
    """(de, ate) do formulário; sem datas, o mês anterior completo."""
    de, ate = request.args.get("de") or "", request.args.get("ate") or ""
    try:
        date.fromisoformat(de)
        date.fromisoformat(ate)
    except ValueError:
        h = date.fromisoformat(hoje())
        fim = h.replace(day=1) - timedelta(days=1)
        de, ate = fim.replace(day=1).isoformat(), fim.isoformat()
    if de > ate:
        de, ate = ate, de
    return de, ate


def _txt_periodo(de, ate):
    return f"{rimp.dma(de)} a {rimp.dma(ate)}"


def _valor_ind(chave, par):
    """Valor de indicador para o Excel: número (1 casa; % em 0 a 100) ou "—"."""
    v = par[0]
    if v is None:
        return "—"
    return round(v * 100, 1) if rimp.FORMATO_IND[chave] == "pct" else round(v, 1)


@app.route("/relatorios")
@app.route("/relatorios/indicadores.<fmt>")
@login_obrigatorio
def relatorios(fmt=None):
    dim = request.args.get("dim") if request.args.get("dim") in rimp.DIMENSOES else "fornecedor"
    procs = processos_importacao()
    ind = rimp.indicadores(procs, dim)
    cartoes = rimp.cartoes_indicadores(ind["estat"])
    if fmt == "xlsx":
        abas = [{"nome": "Resumo", "titulo": f"Indicadores da importação · gerado em {_gerado_em()}",
                 "cab": ["Indicador", "Valor", "Processos considerados", "Como é calculado"],
                 "linhas": [["Processos no total", ind["estat"]["total"], "", ""],
                            ["Entregues na Quinelato", ind["estat"]["entregues"], "", ""],
                            ["Em andamento", ind["estat"]["andamento"], "", ""]]
                 + [[r + (" (%)" if f == "pct" else " (dias)"), _valor_ind(k, ind["estat"][k]), ind["estat"][k][1], e]
                    for k, r, f, e in rimp.INDICADORES]}]
        for d, titulo in rimp.DIMENSOES.items():
            cab = [{"fornecedor": "Fornecedor", "agente": "Agente de carga", "despachante": "Despachante"}[d],
                   "Processos", "Entregues"] + [r + (" (%)" if f == "pct" else " (dias)") for _k, r, f, _e in rimp.INDICADORES]
            linhas = [[nome, e["total"], e["entregues"]] + [_valor_ind(k, e[k]) for k, *_r in rimp.INDICADORES]
                      for nome, e in rimp.por_grupo(procs, d)]
            abas.append({"nome": titulo, "cab": cab, "linhas": linhas})
        return _baixar(exp_imp.xlsx(abas), f"indicadores-importacao-{hoje()}.xlsx", XLSX_MIME)
    if fmt == "pdf":
        if not _pdf_disponivel():
            return redirect(url_for("relatorios", dim=dim))
        pdf = exp_imp.Pdf("Indicadores da importação", rimp.DIMENSOES[dim], paisagem=True)
        pdf.cartoes(cartoes, 5)
        pdf.h2("Visão geral")
        pdf.graficos_lado_a_lado([(t, s, f, None) for t, s, f in ind["graficos"]])
        pdf.grafico(ind["largo"][0], ind["largo"][1], "n", altura=150, cores=False)
        pdf.quebra()
        pdf.h2(rimp.DIMENSOES[dim])
        pdf.graficos_lado_a_lado([(t, s, f, False) for t, s, f in ind["graf_dim"]])
        pdf.h2(f"Tabela {rimp.DIMENSOES[dim].lower()}")
        pdf.tabela([ind["titulo_dim"].capitalize(), "Proc.", "Entr."] + [r for _k, r, _f, _e in rimp.INDICADORES],
                   [[n, e["total"], e["entregues"]] + [rimp.fmt_ind(k, e[k]) for k, *_r in rimp.INDICADORES]
                    for n, e in ind["grupos"]], [16, 5, 5] + [8] * len(rimp.INDICADORES))
        pdf.espaco()
        pdf.p("Cada média considera só os processos que têm as duas datas envolvidas; o número entre parênteses é a "
              "quantidade de processos usados no cálculo. \"—\" = sem dados suficientes.")
        return _baixar(pdf.bytes(_gerado_em()), f"indicadores-importacao-{hoje()}.pdf", "application/pdf")
    return render_template("rel_indicadores.html", ind=ind, cartoes=cartoes, dim=dim, rimp=rimp, aba="indicadores")


@app.route("/relatorios/itens")
@app.route("/relatorios/itens.<fmt>")
@login_obrigatorio
def relatorio_itens(fmt=None):
    hj = hoje()
    procs = processos_importacao()
    rel_ = rimp.itens_por_situacao(procs, hj)
    if fmt == "xlsx":
        abas = [{"nome": "Visao Geral Processos", "cab": rimp.CAB_VISAO_GERAL, "linhas": rimp.linhas_visao_geral(procs, hj)},
                {"nome": "Visao Item a Item", "cab": rimp.CAB_ITENS, "linhas": rimp.linhas_itens(procs)}]
        return _baixar(exp_imp.xlsx(abas), f"itens-por-situacao-{hj}.xlsx", XLSX_MIME)
    if fmt == "csv":
        return _baixar(exp_imp.csv_bytes(rimp.CAB_ITENS, rimp.linhas_itens(procs)), f"itens-por-situacao-{hj}.csv",
                       "text/csv")
    if fmt == "pdf":
        if not _pdf_disponivel():
            return redirect(url_for("relatorio_itens"))
        pdf = exp_imp.Pdf("Itens por situação", "processos ainda não entregues na Quinelato", paisagem=True)
        pdf.cartoes(_cartoes_itens(rel_), 5)
        pdf.graficos_lado_a_lado([("Processos por situação", rel_["graf_status"], "n", False),
                                  ("Processos por canal", rel_["graf_canal"], "n", None)])
        pdf.h2("Processos")
        pdf.tabela(rimp.CAB_VISAO_GERAL, rimp.linhas_visao_geral(procs, hj), [10, 10, 12, 8, 22, 7, 8, 6, 8])
        for t in rel_["tabelas"]:
            pdf.h2(f"{t['titulo']} · {rimp.plural(t['processos'], 'processo', 'processos')}")
            pdf.tabela(["Código", "Descrição", "Quantidade", "Invoices"],
                       [[l["codigo"], l["descricao"], l["quantidade"], ", ".join(l["invoices"])] for l in t["linhas"]],
                       [10, 40, 8, 20], vazio="Sem itens cadastrados nestes processos.")
        if rel_["retidos"]:
            pdf.h2("Itens retidos")
            linhas = []
            for x in rel_["retidos"]:
                p = x["processo"]
                for it in x["itens"] or [{"codigo": "", "descricao": "(sem itens cadastrados)", "quantidade": None}]:
                    linhas.append([p["fornecedor"], p["invoice"], p["status_titulo"], it["codigo"], it["descricao"],
                                   it["quantidade"], p.get("exig_descricao") or "Sem exigência formal"])
            pdf.tabela(["Fornecedor", "Invoice", "Status", "Código", "Descrição", "Qtd.", "Exigência"], linhas,
                       [10, 9, 16, 8, 22, 5, 20])
        return _baixar(pdf.bytes(_gerado_em()), f"itens-por-situacao-{hj}.pdf", "application/pdf")
    return render_template("rel_itens.html", rel=rel_, cartoes=_cartoes_itens(rel_), rimp=rimp, aba="itens",
                           hoje=hj)


def _cartoes_itens(rel_):
    valor = " · ".join(fmt_moeda(v, m) for m, v in sorted(rel_["valor_transito"].items())) or "—"
    return [("Processos ativos", str(len(rel_["ativos"])), "neutro", "ainda não entregues"),
            ("Em trânsito", str(rel_["em_transito"]), "neutro", ""),
            ("Atrasados", str(rel_["atrasados"]), "vermelho" if rel_["atrasados"] else "neutro", "ETA prevista vencida"),
            ("No prazo", str(rel_["no_prazo"]), "verde" if rel_["no_prazo"] else "neutro", ""),
            ("Valor em trânsito", valor, "neutro", "processos com status Em trânsito")]


@app.route("/relatorios/canal")
@app.route("/relatorios/canal.<fmt>")
@login_obrigatorio
def relatorio_canal(fmt=None):
    tipo = request.args.get("tipo") if request.args.get("tipo") in rimp.TIPOS_CANAL else "amarelo"
    de, ate = _periodo_args()
    rel_ = rimp.importacoes_por_canal(processos_importacao(), tipo, de, ate)
    periodo = f"Período: {_txt_periodo(de, ate)}"
    nome = f"importacoes-canal-{tipo.replace('_', '-')}-{de}-a-{ate}"
    if fmt == "xlsx":
        return _baixar(exp_imp.xlsx_canal(rel_["titulo"], periodo, rel_["cab"], rel_["linhas"]), nome + ".xlsx", XLSX_MIME)
    if fmt == "pdf":
        if not _pdf_disponivel():
            return redirect(url_for("relatorio_canal", tipo=tipo, de=de, ate=ate))
        pdf = exp_imp.Pdf(rel_["titulo"], periodo)
        pdf.tabela(rel_["cab"], rel_["linhas"], vazio="Nenhuma importação parametrizada neste período.")
        return _baixar(pdf.bytes(_gerado_em()), nome + ".pdf", "application/pdf")
    return render_template("rel_canal.html", rel=rel_, tipo=tipo, de=de, ate=ate, periodo=periodo, rimp=rimp, aba="canal")


@app.route("/relatorios/mensal")
@app.route("/relatorios/mensal.<fmt>")
@login_obrigatorio
def relatorio_mensal(fmt=None):
    de, ate = _periodo_args()
    m = rimp.relatorio_mensal(processos_importacao(), de, ate)
    periodo = _txt_periodo(de, ate)
    nome = f"relatorio-mensal-importacao-{de}-a-{ate}"

    def abertos(lista):
        return [[q["fornecedor"], q["invoice"], q["status_titulo"], q["ultima_mov_txt"], q["dias_parado"],
                 rimp.dma(rimp.inicio(q))] for q in lista]
    cab_abertos = ["Fornecedor", "Invoice", "Status no fim do período", "Última movimentação", "Dias parado", "Início"]
    cab_cheg = ["Fornecedor", "Invoice", "ETA prevista", "ETA real", "Diferença (dias)", "Peças"]

    def chegadas():
        return [[p["fornecedor"], p["invoice"], rimp.dma(p.get("eta_prevista")) if p.get("eta_prevista") else "—",
                 rimp.dma(p["eta_real"]), _dif(p.get("eta_prevista"), p["eta_real"]),
                 rimp.qtd_itens(p) if p["itens"] else "sem itens"] for p in m["chegadas"]]
    cab_ent = ["Fornecedor", "Invoice", "Entrega prevista", "Entrega real", "Diferença (dias)", "Peças"]

    def entregas():
        return [[p["fornecedor"], p["invoice"], rimp.dma(p.get("entrega_prevista")) if p.get("entrega_prevista") else "—",
                 rimp.dma(p["entrega_real"]), _dif(p.get("entrega_prevista"), p["entrega_real"]),
                 rimp.qtd_itens(p) if p["itens"] else "sem itens"] for p in m["entregas"]]
    resumo = ([["Eventos: " + r, n] for r, n in m["contagem_eventos"]]
              + [["Parametrizações em canal " + c.lower() if c != "Sem canal" else "Parametrizações sem canal", n]
                 for c, n in m["param_canal"]]
              + [["Chegadas: " + r.lower(), n] for r, n in m["pontualidade"]]
              + [[f"Peças — {k.lower()}", v["pecas"] if v["processos"] else "—"] for k, v in m["pecas"].items()]
              + [[f"Processos sem itens — {k.lower()}", v["sem_itens"]] for k, v in m["pecas"].items()]
              + [["Processos em aberto no fim do período", len(m["abertos"])],
                 ["Pendências herdadas (começaram antes do período)", len(m["herdadas"])]])
    if fmt == "xlsx":
        abas = [{"nome": "Resumo", "titulo": f"Relatório mensal da importação · {periodo}", "cab": ["Indicador", "Quantidade"],
                 "linhas": resumo},
                {"nome": "Linha do tempo", "cab": ["Data", "Evento", "Fornecedor", "Invoice", "Detalhe"],
                 "linhas": [[rimp.dma(e["data"]), e["evento"], e["processo"]["fornecedor"], e["processo"]["invoice"],
                             e["detalhe"]] for e in m["eventos"]]},
                {"nome": "Chegadas", "cab": cab_cheg, "linhas": chegadas()},
                {"nome": "Entregas", "cab": cab_ent, "linhas": entregas()},
                {"nome": "Parametrizações", "cab": ["Fornecedor", "Invoice", "Data", "Canal"],
                 "linhas": [[p["fornecedor"], p["invoice"], rimp.dma(p["data_parametrizacao"]), p.get("canal") or "—"]
                            for p in m["parametrizacoes"]]},
                {"nome": "Em aberto", "cab": cab_abertos, "linhas": abertos(m["abertos"])},
                {"nome": "Pendências herdadas", "cab": cab_abertos, "linhas": abertos(m["herdadas"])}]
        return _baixar(exp_imp.xlsx(abas), nome + ".xlsx", XLSX_MIME)
    if fmt == "pdf":
        if not _pdf_disponivel():
            return redirect(url_for("relatorio_mensal", de=de, ate=ate))
        pdf = exp_imp.Pdf("Relatório mensal da importação", periodo, paisagem=True)
        pc = m["pecas"]
        pdf.cartoes([("Eventos no período", str(len(m["eventos"])), "neutro", ""),
                     ("Chegadas ao Brasil", str(len(m["chegadas"])), "neutro", ""),
                     ("Entregas na Quinelato", str(len(m["entregas"])), "neutro", ""),
                     ("Em aberto no fim do período", str(len(m["abertos"])), "neutro", ""),
                     ("Pendências herdadas", str(len(m["herdadas"])), "vermelho" if m["herdadas"] else "neutro",
                      "começaram antes do período")]
                    + [(f"Peças — {k.lower()}", rimp.num(v["pecas"], 0) if v["processos"] else "—", "neutro",
                        f"{v['sem_itens']} sem itens cadastrados" if v["sem_itens"] else "") for k, v in pc.items()], 4)
        pdf.graficos_lado_a_lado([("Eventos do período", m["contagem_eventos"], "n", False),
                                  ("Parametrizações por canal", m["param_canal"], "n", None),
                                  ("Pontualidade das chegadas", m["pontualidade"], "n", False),
                                  ("Processos em aberto por situação", m["status_abertos"], "n", False)], 140)
        pdf.h2("Pendências herdadas (começaram antes do período e continuam pendentes)")
        pdf.tabela(cab_abertos, abertos(m["herdadas"]), [14, 10, 24, 22, 7, 8], vazio="Nenhuma pendência herdada.")
        pdf.h2("Processos em aberto no fim do período")
        pdf.tabela(cab_abertos, abertos(m["abertos"]), [14, 10, 24, 22, 7, 8], vazio="Nenhum processo em aberto.")
        pdf.h2("Chegadas no Brasil no período")
        pdf.tabela(cab_cheg, chegadas(), [16, 12, 10, 10, 8, 8], vazio="Nenhuma chegada no período.")
        pdf.h2("Entregas na Quinelato no período")
        pdf.tabela(cab_ent, entregas(), [16, 12, 10, 10, 8, 8], vazio="Nenhuma entrega no período.")
        pdf.h2("Linha do tempo")
        pdf.tabela(["Data", "Evento", "Fornecedor", "Invoice", "Detalhe"],
                   [[rimp.dma(e["data"]), e["evento"], e["processo"]["fornecedor"], e["processo"]["invoice"], e["detalhe"]]
                    for e in m["eventos"]], [8, 14, 16, 12, 10], vazio="Nenhum evento no período.")
        return _baixar(pdf.bytes(_gerado_em()), nome + ".pdf", "application/pdf")
    return render_template("rel_mensal.html", m=m, de=de, ate=ate, periodo=periodo, rimp=rimp, aba="mensal",
                           dif=_dif)


def _dif(previsto, real):
    d = rimp.dias_entre(previsto, real)
    return "—" if d is None else (f"+{d}" if d > 0 else str(d))


# ---------------------------------------------------------------------------
# v4.73: relatório de descontos (inspeção de recebimento) — poucos números e bem visual
# ---------------------------------------------------------------------------
SITUACAO_DESCONTO_CURTA = {"PENDENTE": "Aguardando envio", "ENVIADO": "Aguardando aprovação", "APROVADO": "Aprovado"}


def dados_descontos(de, ate, fornecedor_id=None):
    """Invoices inspecionados no período (fim da inspeção entre de e ate), descontos, qualidade A/B/C/D por fornecedor
    (eventos com REPORT DATE no período) e o que ainda está em aberto (saldo a deduzir, a qualquer data)."""
    db = get_db()
    todos = [r for r in todos_invoices() if fornecedor_id is None or r["fornecedor_id"] == fornecedor_id]
    insp_ = [r for r in todos if r["tem_desconto"] is not None and r["data_fim_inspecao"]
             and de <= r["data_fim_inspecao"][:10] <= ate]
    moedas = sorted({r["moeda"] for r in insp_}, key=lambda m: (m != "USD", m))
    resumo = {}
    for m in moedas:
        rs = [r for r in insp_ if r["moeda"] == m]
        valor = round(sum(r["valor_invoice"] for r in rs), 2)
        desc = round(sum(r["desconto"]["valor_final"] for r in rs if r["desconto"]), 2)
        resumo[m] = {"invoices": len(rs), "com_desconto": sum(1 for r in rs if r["desconto"]), "valor": valor,
                     "desconto": desc, "pct": (desc / valor) if valor else None}
    # por mês e por fornecedor (cada moeda separada: nunca se misturam)
    por_mes, por_forn = {}, {}
    for r in insp_:
        v = r["desconto"]["valor_final"] if r["desconto"] else 0
        mes = rimp.mes_ano(r["data_fim_inspecao"])
        chave_mes = r["data_fim_inspecao"][:7]
        por_mes.setdefault(r["moeda"], {}).setdefault(chave_mes, [mes, 0])[1] += v
        f = por_forn.setdefault((r["moeda"], r["fornecedor"]), {"valor": 0, "desconto": 0, "n": 0})
        f["valor"] += r["valor_invoice"]
        f["desconto"] += v
        f["n"] += 1
    graf_mes = {m: [(x[0], round(x[1], 2)) for _k, x in sorted(d.items())] for m, d in por_mes.items()}
    graf_forn = {}
    for (m, nome), f in sorted(por_forn.items(), key=lambda x: -(x[1]["desconto"] / x[1]["valor"] if x[1]["valor"] else 0)):
        if f["valor"]:
            graf_forn.setdefault(m, []).append((f"{nome} ({f['n']})", round(f["desconto"] / f["valor"] * 100, 1)))
    tabela_forn = sorted([{"fornecedor": nome, "moeda": m, "invoices": f["n"], "valor": round(f["valor"], 2),
                           "desconto": round(f["desconto"], 2), "pct": f["desconto"] / f["valor"] if f["valor"] else None}
                          for (m, nome), f in por_forn.items()], key=lambda x: (x["moeda"] != "USD", -(x["pct"] or 0)))
    # qualidade A/B/C/D por fornecedor (base de inspeção inteira, inclusive invoices só do MEGA)
    sql = ("SELECT COALESCE(f.nome, fm.nome_sistema, trim(e.fornecedor_excel)) AS forn, e.categoria, SUM(e.quantidade) AS q"
           " " + SQL_BASE + " WHERE substr(COALESCE(e.report_date, ''), 1, 10) BETWEEN ? AND ?"
           " AND e.categoria IN ('A', 'B', 'C', 'D')")
    params = [de, ate]
    if fornecedor_id is not None:
        nome_f = db.execute("SELECT nome FROM fornecedores WHERE id = ?", (fornecedor_id,)).fetchone()
        sql += " AND lower(COALESCE(f.nome, fm.nome_sistema, trim(e.fornecedor_excel))) = lower(?)"
        params.append(nome_f["nome"] if nome_f else "")
    qual = {}
    for x in db.execute(sql + " GROUP BY 1, 2", params):
        if x["forn"]:
            qual.setdefault(x["forn"], {"A": 0, "B": 0, "C": 0, "D": 0})[x["categoria"]] += x["q"] or 0
    qualidade = sorted(({"fornecedor": k, **v, "total": sum(v.values())} for k, v in qual.items() if sum(v.values())),
                       key=lambda x: -((x["C"] + x["D"]) / x["total"]))[:12]
    tot_q = {k: sum(x[k] for x in qual.values()) for k in "ABCD"}
    tot_q["total"] = sum(tot_q.values())
    # em aberto: descontos com saldo a deduzir ou ainda sem aprovação (qualquer data)
    abertos = []
    for r in todos:
        d = r["desconto"]
        if d and (r["saldo_a_deduzir"] > TOLERANCIA or d["status"] != "APROVADO"):
            abertos.append({"id": r["id"], "fornecedor": r["fornecedor"], "invoice": r["numero"], "moeda": r["moeda"],
                            "valor": d["valor_final"], "deduzido": r["deduzido"], "saldo": r["saldo_a_deduzir"],
                            "situacao": SITUACAO_DESCONTO_CURTA.get(d["status"], d["status"]), "status": d["status"],
                            "data": d["data_relatorio"]})
    for a in avulsos_de(fornecedor_id):
        if a["saldo"] > TOLERANCIA:
            abertos.append({"id": None, "fornecedor": a["fornecedor"], "invoice": "Desconto avulso", "moeda": a["moeda"],
                            "valor": a["valor"], "deduzido": a["deduzido"], "saldo": a["saldo"], "situacao": "Aprovado",
                            "status": "APROVADO", "data": a["criado_em"][:10]})
    abertos.sort(key=lambda x: ({"PENDENTE": 0, "ENVIADO": 1}.get(x["status"], 2), x["fornecedor"].lower(), x["data"] or ""))
    saldo = {}
    aguardando = {}
    for x in abertos:
        saldo[x["moeda"]] = round(saldo.get(x["moeda"], 0) + x["saldo"], 2)
        if x["status"] != "APROVADO":
            aguardando.setdefault(x["moeda"], [0, 0])
            aguardando[x["moeda"]][0] += 1
            aguardando[x["moeda"]][1] += x["valor"]
    return {"insp": insp_, "moedas": moedas, "resumo": resumo, "graf_mes": graf_mes, "graf_forn": graf_forn,
            "tabela_forn": tabela_forn, "qualidade": qualidade, "tot_q": tot_q, "abertos": abertos, "saldo": saldo,
            "aguardando": aguardando}


def _cartoes_descontos(x):
    def por_moeda(dic, fmt):
        return " · ".join(fmt(m, v) for m, v in dic.items()) or "—"
    n_insp = sum(v["invoices"] for v in x["resumo"].values())
    n_desc = sum(v["com_desconto"] for v in x["resumo"].values())
    tq = x["tot_q"]
    return [
        ("Invoices inspecionados", str(n_insp), "neutro", f"{n_desc} com desconto" if n_insp else ""),
        ("Total de descontos", por_moeda({m: v["desconto"] for m, v in x["resumo"].items()}, lambda m, v: fmt_moeda(v, m)),
         "neutro", "no período"),
        ("% sobre o valor inspecionado", por_moeda({m: v["pct"] for m, v in x["resumo"].items()},
                                                   lambda m, v: ("—" if v is None else rimp.num(v * 100, 1, "%"))
                                                   + (f" ({m})" if len(x["resumo"]) > 1 else "")),
         "neutro", "desconto ÷ valor dos invoices"),
        ("Peças C + D", rimp.num((tq["C"] + tq["D"]) / tq["total"] * 100, 1, "%") if tq["total"] else "—",
         "vermelho" if tq["total"] and (tq["C"] + tq["D"]) / tq["total"] > 0.05 else "neutro",
         f"{rimp.num(tq['total'], 0)} peças inspecionadas" if tq["total"] else ""),
        ("Saldo a deduzir", por_moeda(x["saldo"], lambda m, v: fmt_moeda(v, m)), "amarelo" if x["saldo"] else "neutro",
         "em aberto, qualquer data" + (" · " + ", ".join(f"{n} aguardando aprovação" for n, _v in x["aguardando"].values())
                                       if x["aguardando"] else "")),
    ]


def _periodo_12_meses():
    de, ate = request.args.get("de") or "", request.args.get("ate") or ""
    try:
        date.fromisoformat(de)
        date.fromisoformat(ate)
    except ValueError:
        h = date.fromisoformat(hoje())
        ate = h.isoformat()
        de = (h.replace(day=1) - timedelta(days=330)).replace(day=1).isoformat()
    return (de, ate) if de <= ate else (ate, de)


@app.route("/relatorios/descontos")
@app.route("/relatorios/descontos.<fmt>")
@login_obrigatorio
def relatorio_descontos(fmt=None):
    de, ate = _periodo_12_meses()
    forn = request.args.get("fornecedor", type=int)
    x = dados_descontos(de, ate, forn)
    cartoes = _cartoes_descontos(x)
    periodo = _txt_periodo(de, ate)
    nome = f"descontos-{de}-a-{ate}"
    linhas_abertos = [[a["fornecedor"], a["invoice"], a["moeda"], a["valor"], a["deduzido"], a["saldo"], a["situacao"]]
                      for a in x["abertos"]]
    cab_abertos = ["Fornecedor", "Invoice", "Moeda", "Desconto", "Deduzido", "Saldo a deduzir", "Situação"]
    if fmt == "xlsx":
        abas = [{"nome": "Descontos", "titulo": f"Descontos · inspeções de {periodo}",
                 "cab": ["Fornecedor", "Invoice", "Moeda", "Fim da inspeção", "Valor do invoice", "Desconto", "% do invoice", "Situação"],
                 "linhas": [[r["fornecedor"], r["numero"], r["moeda"], rimp.dma(r["data_fim_inspecao"]), r["valor_invoice"],
                             r["desconto"]["valor_final"] if r["desconto"] else 0,
                             round((r["desconto"]["valor_final"] if r["desconto"] else 0) / r["valor_invoice"] * 100, 1)
                             if r["valor_invoice"] else "—",
                             SITUACAO_DESCONTO_CURTA.get(r["desconto"]["status"]) if r["desconto"] else "Sem desconto"]
                            for r in sorted(x["insp"], key=lambda r: r["data_fim_inspecao"])]},
                {"nome": "Por fornecedor", "cab": ["Fornecedor", "Moeda", "Invoices", "Valor inspecionado", "Desconto", "% desconto"],
                 "linhas": [[t["fornecedor"], t["moeda"], t["invoices"], t["valor"], t["desconto"],
                             round(t["pct"] * 100, 1) if t["pct"] is not None else "—"] for t in x["tabela_forn"]]},
                {"nome": "Qualidade", "cab": ["Fornecedor", "Peças", "% A", "% B", "% C", "% D"],
                 "linhas": [[q_["fornecedor"], q_["total"]] + [round(q_[k] / q_["total"] * 100, 1) for k in "ABCD"]
                            for q_ in x["qualidade"]]},
                {"nome": "Em aberto", "cab": cab_abertos, "linhas": linhas_abertos}]
        return _baixar(exp_imp.xlsx(abas), nome + ".xlsx", XLSX_MIME)
    if fmt == "pdf":
        if not _pdf_disponivel():
            return redirect(url_for("relatorio_descontos", de=de, ate=ate, fornecedor=forn))
        pdf = exp_imp.Pdf("Descontos", f"inspeções de {periodo}", paisagem=True)
        pdf.cartoes(cartoes, 5)
        graf = [(f"Descontos por mês ({m})", s_, "n", False) for m, s_ in x["graf_mes"].items()]
        graf += [(f"% de desconto por fornecedor ({m})", s_, "pct", False) for m, s_ in x["graf_forn"].items()]
        graf.append(("% de peças C + D por fornecedor", [(f"{q_['fornecedor']} ({rimp.num(q_['total'], 0)})", round((q_["C"] + q_["D"]) / q_["total"] * 100, 1))
                                                         for q_ in x["qualidade"]], "pct", False))
        pdf.graficos_lado_a_lado(graf)
        pdf.h2("Em aberto")
        pdf.tabela(cab_abertos, linhas_abertos, [16, 12, 6, 10, 10, 10, 14], vazio="Nenhum desconto em aberto.")
        return _baixar(pdf.bytes(_gerado_em()), nome + ".pdf", "application/pdf")
    fornecedores = get_db().execute("SELECT id, nome FROM fornecedores ORDER BY nome").fetchall()
    qual_js = {"rotulos": [f"{q_['fornecedor']} ({rimp.num(q_['total'], 0)})" for q_ in x["qualidade"]],
               "series": [{"nome": k, "cor": "#" + rel.CORES[k], "valores": [round(q_[k] / q_["total"] * 100, 1) for q_ in x["qualidade"]]}
                          for k in "ABCD"]}
    return render_template("rel_descontos.html", x=x, cartoes=cartoes, de=de, ate=ate, periodo=periodo, forn=forn,
                           fornecedores=fornecedores, qual_js=qual_js, rimp=rimp, aba="descontos")


FILTROS_SEMANAL = {"a_entregar": "A entregar", "entregues": "Entregues", "todos": "Todos"}


@app.route("/relatorios/semanal")
@app.route("/relatorios/semanal.<fmt>")
@login_obrigatorio
def relatorio_semanal(fmt=None):
    hj = hoje()
    filtro = request.args.get("filtro") if request.args.get("filtro") in FILTROS_SEMANAL else "a_entregar"
    procs = rimp.ordenar(processos_importacao())
    if filtro == "a_entregar":
        lista = [p for p in procs if p["status"] != "ENTREGUE"]
    elif filtro == "entregues":
        lista = [p for p in procs if p["status"] == "ENTREGUE"]
    else:
        lista = procs
    ids = {int(x) for x in request.args.getlist("ids") if x.isdigit()}
    escolhidos = [p for p in procs if p["id"] in ids]
    for p in escolhidos:
        p["resumo"] = rimp.resumo_processo(p, hj)
    if fmt in ("xlsx", "pdf") and not escolhidos:
        flash("Escolha pelo menos um processo para o resumo semanal.", "erro")
        return redirect(url_for("relatorio_semanal", filtro=filtro))
    if fmt == "xlsx":
        abas = [{"nome": "Resumo semanal", "cab": rimp.CAB_SEMANAL, "linhas": [rimp.linha_semanal(p, hj) for p in escolhidos],
                 "larguras": [16, 12, 14, 18, 12, 12, 14, 30, 40, 40, 34, 34, 44, 40]}]
        return _baixar(exp_imp.xlsx(abas), f"resumo-semanal-importacao-{hj}.xlsx", XLSX_MIME)
    if fmt == "pdf":
        if not _pdf_disponivel():
            return redirect(url_for("relatorio_semanal", filtro=filtro, ids=sorted(ids)))
        pdf = exp_imp.Pdf("Resumo semanal da importação", f"{rimp.plural(len(escolhidos), 'processo', 'processos')}")
        for p in escolhidos:
            pdf.h2(f"{p['fornecedor']} · {p['invoice']}")
            dados = [["Status", p["status_titulo"]]] + [[r, v] for r, v in (
                ("DUIMP", p.get("duimp")), ("País", p.get("pais")), ("Agente de carga", p.get("agente")),
                ("Despachante", p.get("despachante")), ("Consolidação", p.get("consolidacao"))) if v]
            pdf.tabela(["Dado", "Valor"], dados + [[r, t] for r, t in p["resumo"]], [12, 48])
        return _baixar(pdf.bytes(_gerado_em()), f"resumo-semanal-importacao-{hj}.pdf", "application/pdf")
    return render_template("rel_semanal.html", lista=lista, escolhidos=escolhidos, ids=ids, filtro=filtro,
                           FILTROS_SEMANAL=FILTROS_SEMANAL, rimp=rimp, aba="semanal")


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
    destino = os.path.join(pasta, f"descontos-{agora_dt():%Y%m%d-%H%M%S}{sufixo}.db")
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
        req_antes = git("rev-parse", "HEAD:requirements.txt")[1]
        cod, saida = git("reset", "--hard", "origin/main")
        if cod != 0:
            flash(f"Falha ao atualizar: {saida}", "erro")
            return redirect(url_for("sistema"))
        if git("rev-parse", "HEAD:requirements.txt")[1] != req_antes:  # bibliotecas novas: instala antes de reiniciar
            cmd = [sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r",
                   os.path.join(BASE_DIR, "requirements.txt")]
            if sys.prefix == getattr(sys, "base_prefix", sys.prefix):
                cmd.insert(4, "--user")
            try:
                subprocess.run(cmd, capture_output=True, timeout=300)
            except Exception:
                app.logger.exception("Falha ao instalar as dependências")
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
    uso = get_db().execute("SELECT COUNT(*), COALESCE(SUM(tamanho), 0) FROM arquivos").fetchone()
    contagem_invoices = {t: get_db().execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                         for t in ("invoices", "descontos", "pagamentos", "lancamentos")}
    return render_template("sistema.html", usa_git=usa_git, commit=commit, backups=backups,
                           anexos_qtd=uso[0], anexos_bytes=uso[1], contagem_invoices=contagem_invoices,
                           rede_mapeamentos=config("rede_mapeamentos", ""),
                           rede_ultima=json.loads(config("rede_ultima_verificacao") or "null"),
                           pastas_padrao={o: pasta_padrao(o) for o in SETOR_DOCUMENTO}, NOME_DOCUMENTO=NOME_DOCUMENTO,
                           docs_contagem={k: n for k, n in get_db().execute(
                               "SELECT status, COUNT(*) FROM documentos GROUP BY status")})


# Tabelas com dados de invoices (na ordem de exclusão). Usuários, fornecedores, configurações (e-mails,
# destinatários, agendador) e a conta de envio são mantidos.
TABELAS_INVOICES = ["relatorios_versoes", "desconto_ajustes", "inspecao_reclass", "inspecao_eventos", "duimp_dados", "revisoes_desconto", "deducoes", "deducoes_avulsas", "descontos", "pagamentos", "invoice_itens", "condicao_parcelas",
                    "anexos", "lancamento_anexos", "lancamentos", "eventos", "notificacoes_estado", "invoices"]


def tabelas_dependentes(db, base):
    """Tabelas que apontam (chave estrangeira ou coluna invoice_id) para as tabelas de invoices, inclusive
    tabelas antigas de versões anteriores que ficaram no banco."""
    todas = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'"
                                      " AND name NOT LIKE 'sqlite_%'")]
    alvo = set(base)
    mudou = True
    while mudou:
        mudou = False
        for t in todas:
            if t in alvo or t in ("emails", "arquivos", "usuarios", "fornecedores", "configuracoes"):
                continue
            refs = {r[2] for r in db.execute(f'PRAGMA foreign_key_list("{t}")')}
            colunas = {r[1] for r in db.execute(f'PRAGMA table_info("{t}")')}
            if refs & alvo or "invoice_id" in colunas:
                alvo.add(t)
                mudou = True
    return alvo - set(base)


@app.route("/sistema/apagar-invoices", methods=["POST"])
@somente_admin
def sistema_apagar_invoices():
    """Apaga todos os invoices e o que depende deles (para recomeçar com dados reais). Faz backup antes."""
    if request.form.get("confirmacao", "").strip().upper() != "APAGAR":
        flash('Para confirmar, digite APAGAR no campo de confirmação.', "erro")
        return redirect(url_for("sistema") + "#apagar")
    db = get_db()
    db.commit()  # nenhuma transação aberta antes de desligar a verificação de chaves
    backup = backup_banco("-antes-de-apagar-invoices")
    qtd = db.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
    arquivos = db.execute("SELECT * FROM arquivos WHERE origem IN ('lancamento', 'pagamento', 'duimp', 'invoice', 'relatorio', 'revisao', 'relatorio_versao')").fetchall()
    extras = sorted(tabelas_dependentes(db, TABELAS_INVOICES))
    removidos_fornecedores = 0
    db.execute("PRAGMA foreign_keys = OFF")  # apaga tudo de uma vez, sem esbarrar na ordem das ligações
    try:
        db.execute("DELETE FROM arquivos WHERE origem IN ('lancamento', 'pagamento', 'duimp', 'invoice', 'relatorio', 'revisao', 'relatorio_versao')")
        db.execute("DELETE FROM documentos")
        for t in extras + TABELAS_INVOICES:
            db.execute(f'DELETE FROM "{t}"')
        # histórico de e-mails sobre os invoices apagados (o restante do histórico e a configuração ficam)
        db.execute("DELETE FROM emails WHERE invoice_id IS NOT NULL OR fluxo IN ('resumo_diario', 'resumo_semanal')")
        if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone():
            db.execute(f"DELETE FROM sqlite_sequence WHERE name IN ({','.join('?' * (len(TABELAS_INVOICES) + 1))})",
                       TABELAS_INVOICES + ["arquivos"])
        if request.form.get("fornecedores"):
            removidos_fornecedores = db.execute("DELETE FROM fornecedores").rowcount
        db.commit()
    except sqlite3.Error as e:
        db.rollback()
        app.logger.exception("Falha ao apagar os invoices")
        flash(f"Não foi possível apagar os invoices: {e}. Nada foi alterado. Envie esta mensagem ao suporte.", "erro")
        return redirect(url_for("sistema") + "#apagar")
    finally:
        db.execute("PRAGMA foreign_keys = ON")
    remover_arquivos(arquivos)
    restantes = db.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
    if restantes:
        flash(f"Atenção: ainda restam {restantes} invoice(s). Tente novamente ou envie esta mensagem ao suporte.", "erro")
    flash(f"{qtd} invoice(s) apagado(s), com itens, descontos, deduções, pagamentos, lançamentos financeiros, "
          f"{len(arquivos)} arquivo(s) e histórico." + (f" {removidos_fornecedores} fornecedor(es) apagado(s)."
                                                       if removidos_fornecedores else " Fornecedores mantidos.")
          + (f" Tabelas antigas ligadas aos invoices também limpas: {', '.join(extras)}." if extras else "")
          + f" Usuários e configurações mantidos. Backup feito antes: {os.path.basename(backup) if backup else '—'}.", "ok")
    return redirect(url_for("sistema"))


# ---------------------------------------------------------------------------
# Lançamentos Financeiros (fora do fluxo de invoices)
#
# Cada invoice cadastrado ganha um painel onde a Importação aponta os custos da importação (COFINS, ICMS, PIS,
# armazenagem...) que o Financeiro deve lançar no MEGA; o Financeiro marca cada um quando já lançou no MEGA.
# Todos os campos são opcionais; não geram pendências nem e-mails.
# ---------------------------------------------------------------------------
# v4.44: categorias fixas de custo (base do fator de importação). True = imposto recuperável (não entra no fator).
CATEGORIAS_CUSTO = [
    ("II", False), ("IPI", True), ("PIS", True), ("COFINS", True), ("ICMS", True), ("Taxa Siscomex", False),
    ("Frete internacional + taxas", False), ("Marinha Mercante (AFRMM)", False),
    ("Armazenagem zona primária", False), ("Armazenagem zona secundária", False),
    ("Frete remoção (zona primária → secundária)", False), ("Frete nacional", False), ("GRU / Anuência", False),
    ("Emissão de LI / LPCO", False), ("Sindicato dos despachantes", False), ("Honorários do despachante", False),
    ("ISS despachante", False), ("Multa / taxas adicionais", False),
]
NOMES_CATEGORIA = [c for c, _r in CATEGORIAS_CUSTO]
RECUPERAVEL = dict(CATEGORIAS_CUSTO)
# tipos digitados livremente antes da v4.44 que correspondem a uma categoria (em minúsculas)
MAPA_TIPOS_ANTIGOS = {"ii": "II", "ipi": "IPI", "pis": "PIS", "pis/pasep": "PIS", "cofins": "COFINS", "icms": "ICMS",
                      "taxa siscomex": "Taxa Siscomex", "siscomex": "Taxa Siscomex", "afrmm": "Marinha Mercante (AFRMM)",
                      "marinha mercante": "Marinha Mercante (AFRMM)", "frete internacional": "Frete internacional + taxas",
                      "frete rodoviário": "Frete nacional", "frete nacional": "Frete nacional",
                      "despachante": "Honorários do despachante", "honorários": "Honorários do despachante",
                      "multa": "Multa / taxas adicionais"}


def pode_lancar():
    """Importação aponta os custos (e o Administrador)."""
    return pode("importacao")


def pode_marcar_erp():
    """Financeiro marca o que já lançou no MEGA (e o Administrador)."""
    return pode("financeiro")


def ler_lancamento(f, tipo_atual=None):
    """Lê um lançamento do formulário. Tudo é opcional, mas a linha não pode ficar totalmente vazia.
    O tipo é uma das categorias fixas (v4.44); um tipo antigo, fora da lista, só é aceito se não mudou."""
    tipo = " ".join(f.get("tipo", "").split())[:80]
    if tipo and tipo not in NOMES_CATEGORIA and tipo != (tipo_atual or ""):
        return {"tipo": tipo, "valor": None, "data_pagamento": None}, ["Escolha o tipo na lista de categorias."]
    valor_txt = f.get("valor", "").strip()
    valor = parse_num(valor_txt) if valor_txt else None
    data_txt = f.get("data_pagamento", "").strip()
    data = data_valida(data_txt) if data_txt else None
    erros = []
    if valor_txt and valor is None:
        erros.append("Valor inválido (use, por exemplo, 1.234,56).")
    elif valor is not None and valor < 0:
        erros.append("O valor não pode ser negativo.")
    if data_txt and not data:
        erros.append("Data do pagamento inválida.")
    if not erros and not tipo and valor is None and not data:
        erros.append("Preencha ao menos um campo do lançamento.")
    return {"tipo": tipo, "valor": valor, "data_pagamento": data}, erros


def descrever_lancamento(l):
    partes = [l["tipo"] or "sem tipo", fmt_moeda(l["valor"], "BRL") if l["valor"] is not None else "sem valor",
              fmt_data(l["data_pagamento"]) if l["data_pagamento"] else "sem data"]
    return " · ".join(partes)


def paineis_lancamentos(args):
    db = get_db()
    busca = (args.get("q") or "").strip().lower()
    filtro = args.get("filtro", "")
    invoices = db.execute(
        "SELECT i.id, i.numero, i.moeda, i.data_assinatura, i.cancelado_em, i.criado_em, f.nome AS fornecedor"
        " FROM invoices i JOIN fornecedores f ON f.id = i.fornecedor_id ORDER BY i.id DESC").fetchall()
    lancs = {}
    for l in db.execute("SELECT l.*, u.nome AS usuario, e.nome AS usuario_erp FROM lancamentos l"
                        " LEFT JOIN usuarios u ON u.id = COALESCE(l.atualizado_por, l.criado_por)"
                        " LEFT JOIN usuarios e ON e.id = l.lancado_erp_por"
                        " ORDER BY l.data_pagamento IS NULL, l.data_pagamento, l.id"):
        lancs.setdefault(l["invoice_id"], []).append(l)
    anexos = anexos_por_lancamento()
    docs = documentos_de("lancamento", [l["id"] for lista in lancs.values() for l in lista])
    avisos = {}
    for e in db.execute("SELECT invoice_id, criado_em, status FROM emails WHERE fluxo = 'lancamentos_mega'"
                        " ORDER BY id"):
        avisos[e["invoice_id"]] = e
    # v4.66: processos concluídos saem da lista (a não ser que ainda tenham custo a lançar no MEGA)
    concluidos = {r["id"] for r in todos_invoices() if r["situacao"] == "CONCLUIDO"}
    paineis = []
    for inv in invoices:
        lista = lancs.get(inv["id"], [])
        if inv["cancelado_em"] and not lista and filtro != "todos":
            continue
        a_lancar = any(not l["lancado_erp"] for l in lista)
        if filtro == "concluidos":
            if inv["id"] not in concluidos:
                continue
        elif inv["id"] in concluidos and filtro != "todos" and not a_lancar:
            continue
        if busca and busca not in inv["numero"].lower() and busca not in inv["fornecedor"].lower():
            continue
        if filtro == "com" and not lista:
            continue
        if filtro == "sem" and lista:
            continue
        pendentes = [l for l in lista if not l["lancado_erp"]]
        if filtro == "pendentes_erp" and not pendentes:
            continue
        paineis.append({"inv": inv, "lancamentos": lista, "pendentes_erp": len(pendentes), "anexos": anexos, "docs": docs,
                        "ultimo_aviso": avisos.get(inv["id"]),
                        "total_pendente": round(sum(l["valor"] for l in pendentes if l["valor"] is not None), 2),
                        "total": round(sum(l["valor"] for l in lista if l["valor"] is not None), 2)})
    return paineis


@app.route("/lancamentos")
@login_obrigatorio
def lancamentos():
    paineis = paineis_lancamentos(request.args)
    sugestoes = NOMES_CATEGORIA
    total = round(sum(p["total"] for p in paineis), 2)
    qtd = sum(len(p["lancamentos"]) for p in paineis)
    pend_qtd = sum(p["pendentes_erp"] for p in paineis)
    pend_total = round(sum(p["total_pendente"] for p in paineis), 2)
    return render_template("lancamentos.html", paineis=paineis, sugestoes=sugestoes, total=total, qtd=qtd,
                           pend_qtd=pend_qtd, pend_total=pend_total, args=request.args,
                           aberto=request.args.get("aberto", type=int), editar=pode_lancar(),
                           marcar=pode_marcar_erp(), pode_anexar=pode_anexar(), ANEXO_MAX_MB=ANEXO_MAX_MB)


def voltar_lancamentos(iid):
    destino = request.form.get("voltar") or url_for("lancamentos")
    sep = "&" if "?" in destino else "?"
    return redirect(f"{destino}{sep}aberto={iid}#inv-{iid}")


@app.route("/invoices/<int:id>/lancamentos", methods=["POST"])
@login_obrigatorio
def lancamento_novo(id):
    if not pode_lancar():
        abort(403)
    if get_db().execute("SELECT 1 FROM invoices WHERE id = ?", (id,)).fetchone() is None:
        abort(404)
    dados, erros = ler_lancamento(request.form)
    if erros:
        for e in erros:
            flash(e, "erro")
        return voltar_lancamentos(id)
    get_db().execute("INSERT INTO lancamentos (invoice_id, tipo, valor, data_pagamento, criado_por, criado_em)"
                     " VALUES (?, ?, ?, ?, ?, ?)",
                     (id, dados["tipo"], dados["valor"], dados["data_pagamento"], g.usuario["id"], agora()))
    registrar_evento(id, "Lançamento financeiro incluído", descrever_lancamento(dados))
    get_db().commit()
    flash("Lançamento incluído.", "ok")
    return voltar_lancamentos(id)


@app.route("/lancamentos/<int:lid>/editar", methods=["POST"])
@login_obrigatorio
def lancamento_editar(lid):
    if not pode_lancar():
        abort(403)
    db = get_db()
    antigo = db.execute("SELECT * FROM lancamentos WHERE id = ?", (lid,)).fetchone()
    if antigo is None:
        abort(404)
    dados, erros = ler_lancamento(request.form, antigo["tipo"])
    if erros:
        for e in erros:
            flash(e, "erro")
        return voltar_lancamentos(antigo["invoice_id"])
    if (dados["tipo"], dados["valor"], dados["data_pagamento"]) != (antigo["tipo"], antigo["valor"], antigo["data_pagamento"]):
        # se já estava lançado no MEGA, volta para pendente e avisa o Financeiro para conferir
        ja_no_erp = bool(antigo["lancado_erp"]) or bool(antigo["alterado_apos_erp"])
        db.execute("UPDATE lancamentos SET tipo = ?, valor = ?, data_pagamento = ?, atualizado_por = ?,"
                   " atualizado_em = ?, lancado_erp = 0, alterado_apos_erp = ? WHERE id = ?",
                   (dados["tipo"], dados["valor"], dados["data_pagamento"], g.usuario["id"], agora(),
                    1 if ja_no_erp else 0, lid))
        registrar_evento(antigo["invoice_id"], "Lançamento financeiro corrigido",
                         f"{descrever_lancamento(antigo)} → {descrever_lancamento(dados)}"
                         + (" (já estava lançado no MEGA: voltou para conferência do Financeiro)" if antigo["lancado_erp"] else ""))
        db.commit()
        flash("Lançamento atualizado." + (" Ele já estava lançado no MEGA: o Financeiro verá o aviso para conferir."
                                          if antigo["lancado_erp"] else ""), "ok")
    return voltar_lancamentos(antigo["invoice_id"])


@app.route("/lancamentos/<int:lid>/excluir", methods=["POST"])
@login_obrigatorio
def lancamento_excluir(lid):
    if not pode_lancar():
        abort(403)
    db = get_db()
    l = db.execute("SELECT * FROM lancamentos WHERE id = ?", (lid,)).fetchone()
    if l is None:
        abort(404)
    if l["lancado_erp"] and g.usuario["perfil"] != "admin":
        flash("Este custo já foi lançado no MEGA pelo Financeiro: para excluí-lo, peça ao Financeiro para desmarcar "
              "o lançamento no MEGA antes (assim o ajuste no MEGA não se perde).", "erro")
        return voltar_lancamentos(l["invoice_id"])
    anexos = excluir_arquivos_de("lancamento", lid)
    db.execute("DELETE FROM lancamentos WHERE id = ?", (lid,))
    registrar_evento(l["invoice_id"], "Lançamento financeiro excluído", descrever_lancamento(l)
                     + (f" (com {len(anexos)} arquivo(s) anexado(s))" if anexos else ""))
    db.commit()
    remover_arquivos(anexos)
    flash("Lançamento excluído.", "ok")
    return voltar_lancamentos(l["invoice_id"])


@app.route("/lancamentos/<int:lid>/erp", methods=["POST"])
@login_obrigatorio
def lancamento_erp(lid):
    """Financeiro marca/desmarca o custo como já lançado no MEGA (controle próprio)."""
    if not pode_marcar_erp():
        abort(403)
    db = get_db()
    l = db.execute("SELECT * FROM lancamentos WHERE id = ?", (lid,)).fetchone()
    if l is None:
        abort(404)
    marcar = request.form.get("lancado") == "1"
    if marcar:
        db.execute("UPDATE lancamentos SET lancado_erp = 1, lancado_erp_por = ?, lancado_erp_em = ?,"
                   " alterado_apos_erp = 0 WHERE id = ?", (g.usuario["id"], agora(), lid))
        registrar_evento(l["invoice_id"], "Custo lançado no MEGA (Financeiro)", descrever_lancamento(l))
    else:
        db.execute("UPDATE lancamentos SET lancado_erp = 0, lancado_erp_por = NULL, lancado_erp_em = NULL"
                   " WHERE id = ?", (lid,))
        registrar_evento(l["invoice_id"], "Lançamento no MEGA desmarcado (Financeiro)", descrever_lancamento(l))
    db.commit()
    return voltar_lancamentos(l["invoice_id"])


# ---------- arquivos anexados (padrão do sistema para qualquer anexo) ----------
# Os arquivos são enviados pelo navegador e ficam no servidor (pasta ARQUIVOS_DIR, fora do GitHub).
# origem: 'lancamento' (custos da aba Lançamentos Financeiros) | 'pagamento' (Swift de cada pagamento)
ORIGENS_ARQUIVO = {"lancamento": "lançamento financeiro", "pagamento": "pagamento (Swift)", "duimp": "DUIMP",
                   "invoice": "invoice assinado", "relatorio": "relatório de inspeção",
                   "revisao": "revisão do relatório de inspeção", "relatorio_versao": "relatório enviado ao fornecedor"}
# v4.41: relatório de inspeção (desconto) e revisão do relatório (revalidação) — obrigatórios, somente Excel
EXTENSOES_EXCEL = ("xlsx", "xlsm", "xls")


def arquivo_excel_form(campo):
    """(arquivo, erro) do campo de upload que precisa ser uma planilha Excel."""
    f = request.files.get(campo)
    if not f or not f.filename:
        return None, None
    if f.filename.rsplit(".", 1)[-1].lower() not in EXTENSOES_EXCEL:
        return None, f"{f.filename}: o relatório deve ser um arquivo Excel (.xlsx, .xlsm ou .xls)."
    return f, None


def relatorios_desconto(did):
    return arquivos_de("relatorio", [did]).get(did, []) if did else []


def arquivos_de(origem, ids=None):
    """{origem_id: [arquivos]} de uma origem (opcionalmente só alguns ids)."""
    sql = ("SELECT a.*, u.nome AS usuario FROM arquivos a LEFT JOIN usuarios u ON u.id = a.enviado_por"
           " WHERE a.origem = ?")
    params = [origem]
    if ids is not None:
        ids = list(ids)
        if not ids:
            return {}
        sql += f" AND a.origem_id IN ({','.join('?' * len(ids))})"
        params += ids
    mapa = {}
    for a in get_db().execute(sql + " ORDER BY a.id", params):
        mapa.setdefault(a["origem_id"], []).append(a)
    return mapa


def anexos_por_lancamento(ids=None):
    return arquivos_de("lancamento", ids)


def caminho_anexo(a):
    return os.path.join(ARQUIVOS_DIR, a["arquivo"])


def fmt_tamanho(n):
    n = n or 0
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB".replace(".", ",")
    return f"{max(1, round(n / 1024))} KB"


app.jinja_env.filters["tamanho"] = fmt_tamanho


def pode_anexar():
    """Importação, Financeiro e Administrador podem anexar arquivos aos lançamentos."""
    return g.usuario is not None and g.usuario["perfil"] in ("admin", "importacao", "financeiro")


def remover_arquivos(anexos):
    for a in anexos:
        try:
            os.remove(caminho_anexo(a))
        except OSError:
            pass


def excluir_arquivos_de(origem, origem_id):
    lista = arquivos_de(origem, [origem_id]).get(origem_id, [])
    get_db().execute("DELETE FROM arquivos WHERE origem = ? AND origem_id = ?", (origem, origem_id))
    return lista  # apagar do disco depois do commit (remover_arquivos)


def salvar_arquivos(origem, origem_id, arquivos):
    """Grava os arquivos enviados (lista de FileStorage). Retorna (nomes salvos, mensagens de erro)."""
    os.makedirs(ARQUIVOS_DIR, exist_ok=True)
    salvos, erros = [], []
    for f in arquivos:
        if not f or not f.filename:
            continue
        nome = os.path.basename(f.filename.replace("\\", "/")).strip()[:150] or "arquivo"
        ext = nome.rsplit(".", 1)[-1].lower() if "." in nome else ""
        if ext not in EXTENSOES_ANEXO:
            erros.append(f"{nome}: tipo de arquivo não permitido (use PDF, imagem, Excel, Word, XML, ZIP…).")
            continue
        conteudo = f.read(ANEXO_MAX_MB * 1024 * 1024 + 1)
        if len(conteudo) > ANEXO_MAX_MB * 1024 * 1024:
            erros.append(f"{nome}: maior que {ANEXO_MAX_MB} MB.")
            continue
        if not conteudo:
            erros.append(f"{nome}: arquivo vazio.")
            continue
        gravado = f"{origem}-{origem_id}-{secrets.token_hex(8)}.{ext}"
        with open(os.path.join(ARQUIVOS_DIR, gravado), "wb") as saida:
            saida.write(conteudo)
        get_db().execute("INSERT INTO arquivos (origem, origem_id, nome, arquivo, tamanho, enviado_por, enviado_em)"
                         " VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (origem, origem_id, nome, gravado, len(conteudo), g.usuario["id"], agora()))
        salvos.append(nome)
    return salvos, erros


def info_origem(a):
    """(invoice_id, descrição, url de volta) do registro ao qual o arquivo pertence."""
    db = get_db()
    if a["origem"] == "lancamento":
        l = db.execute("SELECT invoice_id, tipo FROM lancamentos WHERE id = ?", (a["origem_id"],)).fetchone()
        if l is None:
            return None, "", url_for("lancamentos")
        return l["invoice_id"], l["tipo"] or "sem tipo", None
    if a["origem"] == "duimp":
        return a["origem_id"], "DUIMP", url_for("invoice_detalhe", id=a["origem_id"]) + "#etapa-DESEMBARACO"
    if a["origem"] == "invoice":
        return a["origem_id"], "invoice assinado", url_for("invoice_detalhe", id=a["origem_id"])
    if a["origem"] in ("relatorio", "revisao", "relatorio_versao"):
        sql = {"relatorio": "SELECT invoice_id FROM descontos WHERE id = ?",
               "revisao": "SELECT d.invoice_id FROM revisoes_desconto v JOIN descontos d ON d.id = v.desconto_id WHERE v.id = ?",
               "relatorio_versao": "SELECT invoice_id FROM relatorios_versoes WHERE id = ?"}[a["origem"]]
        x = db.execute(sql, (a["origem_id"],)).fetchone()
        if x is None:
            return None, "", url_for("descontos")
        return x["invoice_id"], ORIGENS_ARQUIVO[a["origem"]], url_for("invoice_detalhe", id=x["invoice_id"]) + "#inspecao"
    p = db.execute("SELECT invoice_id, data_pagamento FROM pagamentos WHERE id = ?", (a["origem_id"],)).fetchone()
    if p is None:
        return None, "", url_for("painel")
    return p["invoice_id"], f"pagamento de {fmt_data(p['data_pagamento'])}", \
        url_for("invoice_detalhe", id=p["invoice_id"]) + "#pagamentos"


@app.route("/lancamentos/<int:lid>/anexos", methods=["POST"])
@login_obrigatorio
def lancamento_anexar(lid):
    if not pode_anexar():
        abort(403)
    db = get_db()
    l = db.execute("SELECT * FROM lancamentos WHERE id = ?", (lid,)).fetchone()
    if l is None:
        abort(404)
    arquivos = [f for f in request.files.getlist("arquivo") if f and f.filename]
    if not arquivos:
        flash("Escolha um arquivo para anexar.", "erro")
        return voltar_lancamentos(l["invoice_id"])
    salvos, erros = salvar_arquivos("lancamento", lid, arquivos)
    if salvos:
        registrar_evento(l["invoice_id"], "Arquivo anexado ao lançamento financeiro",
                         f"{l['tipo'] or 'sem tipo'}: " + ", ".join(salvos))
        db.commit()
        flash(("Arquivo anexado: " if len(salvos) == 1 else "Arquivos anexados: ") + ", ".join(salvos) + ".", "ok")
    for e in erros:
        flash(e, "erro")
    return voltar_lancamentos(l["invoice_id"])


@app.route("/invoices/<int:id>/duimp", methods=["POST"])
@login_obrigatorio
def duimp_anexar(id):
    """A Importação anexa o arquivo da DUIMP (etapa Desembaraço)."""
    if not pode("importacao"):
        abort(403)
    r = carregar_invoice(id)
    arquivos = [f for f in request.files.getlist("arquivo") if f and f.filename]
    if not arquivos:
        flash("Escolha o arquivo da DUIMP.", "erro")
    else:
        salvos, erros = salvar_arquivos("duimp", id, arquivos)
        if salvos:
            registrar_evento(id, "DUIMP anexada", ", ".join(salvos))
            get_db().commit()
            flash("DUIMP anexada: " + ", ".join(salvos) + ".", "ok")
        for e in erros:
            flash(e, "erro")
        pdf = get_db().execute("SELECT id FROM arquivos WHERE origem = 'duimp' AND origem_id = ? AND lower(nome) LIKE '%.pdf'"
                               " ORDER BY id DESC", (id,)).fetchone() if salvos else None
        if pdf and not request.form.get("sem_leitura"):
            return redirect(url_for("duimp_dados", id=id, arquivo=pdf["id"]))
    return redirect(url_for("invoice_detalhe", id=r["id"]) + "#etapa-DESEMBARACO")


# ---------------------------------------------------------------------------
# Fator de importação (v4.44)
# Fator = (FOB em R$ + custos não recuperáveis) ÷ FOB em R$ = 1 + custos não recuperáveis ÷ FOB em R$
# - FOB: valor final do invoice (depois das deduções), na moeda; em R$ pelo câmbio dos pagamentos registrados
#   (média ponderada das cotações). Sem pagamento com cotação, usa a taxa da DUIMP (estimativa).
# - Custos: lançamentos financeiros do invoice por categoria (recuperáveis: IPI, PIS, COFINS, ICMS — não entram).
# - Os tributos da DUIMP viram lançamentos na conferência dos dados da DUIMP (sem duplicar).
# ---------------------------------------------------------------------------
CAMPOS_DUIMP_CATEGORIA = [("ii", "II"), ("ipi", "IPI"), ("pis", "PIS"), ("cofins", "COFINS"), ("icms", "ICMS"),
                          ("taxa_siscomex", "Taxa Siscomex"), ("frete_brl", "Frete internacional + taxas"),
                          ("multa", "Multa / taxas adicionais")]


def garantir_pypdf():
    """A leitura do PDF usa a biblioteca pypdf. Se o servidor ainda não a tem (atualização feita por uma versão
    antiga), instala uma vez, automaticamente."""
    try:
        import pypdf  # noqa: F401
        return True
    except ImportError:
        pass
    cmd = [sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "pypdf>=4"]
    if sys.prefix == getattr(sys, "base_prefix", sys.prefix):  # fora de ambiente virtual (PythonAnywhere)
        cmd.insert(4, "--user")
    try:
        subprocess.run(cmd, capture_output=True, timeout=180)
        import site, importlib
        importlib.invalidate_caches()
        if site.getusersitepackages() not in sys.path:
            sys.path.append(site.getusersitepackages())
        import pypdf  # noqa: F401
        return True
    except Exception:
        app.logger.exception("Não foi possível instalar o pypdf")
        return False


def fator_importacao(r):
    """Fator de importação do invoice e o detalhamento (ou None se ainda não há base)."""
    db = get_db()
    dd = db.execute("SELECT * FROM duimp_dados WHERE invoice_id = ?", (r["id"],)).fetchone()
    lancs = db.execute("SELECT * FROM lancamentos WHERE invoice_id = ? AND valor IS NOT NULL", (r["id"],)).fetchall()
    fob = r["valor_invoice"]
    avisos = []
    # câmbio do FOB: pagamentos registrados (cotação de cada pagamento)
    pagos = [p for p in r["pagamentos"] if p["cotacao"]]
    if r["moeda"] == "BRL":
        taxa, origem_taxa = 1.0, "invoice em reais"
    elif pagos:
        taxa = sum(p["valor"] * p["cotacao"] for p in pagos) / sum(p["valor"] for p in pagos)
        origem_taxa = "câmbio dos pagamentos" + (" (média ponderada)" if len(pagos) > 1 else "")
        if len(pagos) < len(r["pagamentos"]):
            avisos.append("Há pagamentos sem cotação: só os pagamentos com cotação entram no câmbio do FOB.")
    elif dd and dd["taxa_cambio"]:
        taxa, origem_taxa = dd["taxa_cambio"], "taxa da DUIMP (estimativa: ainda não há pagamento com cotação)"
        avisos.append("FOB em R$ estimado pela taxa da DUIMP: será recalculado com a cotação dos pagamentos.")
    else:
        taxa, origem_taxa = None, None
    if not dd:
        avisos.append("Dados da DUIMP ainda não conferidos.")
    por_cat = {}
    sem_categoria = []
    for l in lancs:
        if l["tipo"] in RECUPERAVEL:
            por_cat[l["tipo"]] = round(por_cat.get(l["tipo"], 0) + l["valor"], 2)
        else:
            sem_categoria.append(l)
    if sem_categoria:
        avisos.append("Lançamentos sem categoria não entram no cálculo: "
                      + ", ".join(f"{l['tipo'] or 'sem tipo'} ({fmt_moeda(l['valor'], 'BRL')})" for l in sem_categoria)
                      + ". Corrija o tipo na aba Lançamentos Financeiros.")
    nao_rec = round(sum(v for c, v in por_cat.items() if not RECUPERAVEL[c]), 2)
    rec = round(sum(v for c, v in por_cat.items() if RECUPERAVEL[c]), 2)
    fob_brl = round(fob * taxa, 2) if taxa and fob else None
    fator = round(1 + nao_rec / fob_brl, 4) if fob_brl else None
    if r["situacao"] != "CONCLUIDO":
        avisos.append("Invoice ainda não concluído: o fator pode mudar com novos custos e pagamentos.")
    pdfs = [x["id"] for x in r.get("duimp_arquivos", []) if x["nome"].lower().endswith(".pdf")]
    return {"duimp": dd, "fob": fob, "pdf_id": pdfs[-1] if pdfs else None, "taxa": taxa, "origem_taxa": origem_taxa, "fob_brl": fob_brl,
            "fob_brl_duimp": round(fob * dd["taxa_cambio"], 2) if dd and dd["taxa_cambio"] and fob else None,
            "categorias": [(c, RECUPERAVEL[c], por_cat.get(c)) for c in NOMES_CATEGORIA],
            "nao_rec": nao_rec, "rec": rec, "fator": fator, "sem_categoria": sem_categoria,
            "pct_nao_rec": nao_rec / fob_brl if fob_brl else None, "pct_rec": rec / fob_brl if fob_brl else None,
            "total_brl": round((fob_brl or 0) + nao_rec + rec, 2), "avisos": avisos,
            "final": bool(fator and dd and r["situacao"] == "CONCLUIDO" and pagos and not sem_categoria)}


def fator_resumido(iid, r=None):
    """Só o número do fator (para o Painel)."""
    try:
        f = fator_importacao(r or carregar_invoice(iid))
        return (f["fator"], f["final"]) if f["fator"] else (None, False)
    except Exception:
        return None, False


@app.route("/invoices/<int:id>/duimp-dados", methods=["GET", "POST"])
@login_obrigatorio
def duimp_dados(id):
    """Conferência dos dados da DUIMP: lidos do PDF anexado (ou digitados) e confirmados pela Importação.
    Ao salvar, os tributos (e o frete/multa, se escolhido) viram lançamentos financeiros do invoice."""
    if not pode("importacao"):
        abort(403)
    r = carregar_invoice(id)
    db = get_db()
    atual = db.execute("SELECT * FROM duimp_dados WHERE invoice_id = ?", (id,)).fetchone()
    existentes = {}
    for l in db.execute("SELECT * FROM lancamentos WHERE invoice_id = ? ORDER BY id", (id,)):
        existentes.setdefault(l["tipo"], []).append(l)
    if request.method == "POST":
        f = request.form
        dados = {}
        erros = []
        for campo in leitor_duimp.CAMPOS:
            v = (f.get(campo) or "").strip()
            if campo == "numero":
                dados[campo] = v[:30] or None
            elif campo == "data_registro":
                dados[campo] = data_valida(v) if v else None
                if v and not dados[campo]:
                    erros.append("Data de registro inválida.")
            elif campo == "moeda":
                dados[campo] = v if v in MOEDAS else r["moeda"]
            else:
                dados[campo] = parse_num(v) if v else None
                if v and dados[campo] is None:
                    erros.append(f"Valor inválido em {campo}.")
        if not dados["taxa_cambio"] and r["moeda"] != "BRL":
            erros.append("Informe a taxa de câmbio da DUIMP.")
        if erros:
            for e in erros:
                flash(e, "erro")
            return redirect(url_for("duimp_dados", id=id))
        arquivo_id = f.get("arquivo_id", type=int)
        valores = [dados[c] for c in leitor_duimp.CAMPOS]
        if atual:
            db.execute(f"UPDATE duimp_dados SET {', '.join(c + ' = ?' for c in leitor_duimp.CAMPOS)}, arquivo_id = ?,"
                       " conferido_por = ?, conferido_em = ? WHERE invoice_id = ?",
                       (*valores, arquivo_id, g.usuario["id"], agora(), id))
        else:
            db.execute(f"INSERT INTO duimp_dados (invoice_id, {', '.join(leitor_duimp.CAMPOS)}, arquivo_id, conferido_por,"
                       f" conferido_em) VALUES (?, {', '.join('?' * len(valores))}, ?, ?, ?)",
                       (id, *valores, arquivo_id, g.usuario["id"], agora()))
        # lançamentos: criar / atualizar conforme a escolha de cada linha
        feitos = []
        for campo, categoria in CAMPOS_DUIMP_CATEGORIA:
            acao = f.get(f"acao_{campo}")
            valor = dados.get(campo)
            if not valor or acao not in ("criar", "atualizar"):
                continue
            ja = existentes.get(categoria, [])
            if acao == "atualizar" and len(ja) == 1:
                l = ja[0]
                if abs((l["valor"] or 0) - valor) > TOLERANCIA:
                    db.execute("UPDATE lancamentos SET valor = ?, atualizado_por = ?, atualizado_em = ?, lancado_erp = 0,"
                               " alterado_apos_erp = ? WHERE id = ?",
                               (valor, g.usuario["id"], agora(), 1 if (l["lancado_erp"] or l["alterado_apos_erp"]) else 0,
                                l["id"]))
                    feitos.append(f"{categoria} atualizado: {fmt_moeda(l['valor'], 'BRL')} → {fmt_moeda(valor, 'BRL')}")
            elif acao == "criar":
                db.execute("INSERT INTO lancamentos (invoice_id, tipo, valor, data_pagamento, criado_por, criado_em)"
                           " VALUES (?, ?, ?, ?, ?, ?)",
                           (id, categoria, valor, dados["data_registro"] if campo not in ("frete_brl",) else None,
                            g.usuario["id"], agora()))
                feitos.append(f"{categoria} {fmt_moeda(valor, 'BRL')}")
        registrar_evento(id, "Dados da DUIMP conferidos" + (f" ({dados['numero']})" if dados["numero"] else ""),
                         (f"Taxa {fmt_num(dados['taxa_cambio'], 4)}" if dados["taxa_cambio"] else "")
                         + (" · lançamentos: " + "; ".join(feitos) if feitos else ""))
        db.commit()
        flash("Dados da DUIMP salvos." + (f" Lançamentos financeiros: {'; '.join(feitos)}." if feitos else ""), "ok")
        return redirect(url_for("invoice_detalhe", id=id) + "#fator")
    # GET: lê o PDF escolhido (ou mostra os dados já salvos)
    arquivo_id = request.args.get("arquivo", type=int)
    lidos, avisos = None, []
    if arquivo_id:
        a = db.execute("SELECT * FROM arquivos WHERE id = ? AND origem = 'duimp' AND origem_id = ?",
                       (arquivo_id, id)).fetchone()
        if a is None:
            abort(404)
        if not garantir_pypdf():
            avisos.append("O componente de leitura de PDF não está instalado no servidor: preencha os dados manualmente.")
        else:
            try:
                lidos, avisos = leitor_duimp.ler_pdf(caminho_anexo(a))
            except Exception as e:
                app.logger.warning("Falha ao ler a DUIMP %s: %s", arquivo_id, e)
                avisos.append("Não foi possível ler este PDF: preencha os dados manualmente.")
        if lidos and lidos.get("moeda") and lidos["moeda"] != r["moeda"]:
            avisos.append(f"A moeda da DUIMP ({lidos['moeda']}) é diferente da moeda do invoice ({r['moeda']}).")
        if lidos and lidos.get("fob_moeda") and abs(lidos["fob_moeda"] - r["valor_invoice"]) > max(0.05, r["valor_invoice"] * 0.001):
            avisos.append(f"O FOB da DUIMP ({fmt_moeda(lidos['fob_moeda'], r['moeda'])}) é diferente do valor final do invoice "
                          f"({fmt_moeda(r['valor_invoice'], r['moeda'])}). Confira (o fator usa o valor final do invoice).")
    base = lidos or (dict(atual) if atual else {c: None for c in leitor_duimp.CAMPOS})
    if not arquivo_id and atual:
        arquivo_id = atual["arquivo_id"]
    linhas = []
    for campo, categoria in CAMPOS_DUIMP_CATEGORIA:
        ja = existentes.get(categoria, [])
        valor = base.get(campo)
        if not ja:
            padrao = "criar" if valor and campo != "frete_brl" else "nada"
        elif len(ja) == 1 and valor and abs((ja[0]["valor"] or 0) - valor) > TOLERANCIA:
            padrao = "atualizar"
        else:
            padrao = "nada"
        linhas.append({"campo": campo, "categoria": categoria, "valor": valor, "existentes": ja, "padrao": padrao,
                       "recuperavel": RECUPERAVEL[categoria]})
    arquivos_pdf = [x for x in r["duimp_arquivos"] if x["nome"].lower().endswith(".pdf")]
    return render_template("duimp_dados.html", r=r, d=base, avisos=avisos, lidos=bool(lidos), linhas=linhas,
                           arquivo_id=arquivo_id, atual=atual, arquivos_pdf=arquivos_pdf)


@app.route("/invoices/<int:id>/invoice-assinado", methods=["POST"])
@login_obrigatorio
def invoice_assinado_anexar(id):
    """A Importação anexa o invoice assinado (obrigatório para concluir o cadastro)."""
    if not pode("importacao"):
        abort(403)
    r = carregar_invoice(id)
    arquivos = [f for f in request.files.getlist("arquivo") if f and f.filename]
    if not arquivos:
        flash("Escolha o arquivo do invoice assinado.", "erro")
    else:
        salvos, erros = salvar_arquivos("invoice", id, arquivos)
        if salvos:
            registrar_evento(id, "Invoice assinado anexado", ", ".join(salvos))
            get_db().commit()
            flash("Invoice assinado anexado: " + ", ".join(salvos) + ".", "ok")
            if registrar_conclusao_cadastro(id):
                flash("Cadastro do invoice concluído: o vencimento da parcela antecipada conta a partir de hoje.", "ok")
        for e in erros:
            flash(e, "erro")
    return redirect(url_for("invoice_detalhe", id=r["id"]))


@app.route("/invoices/<int:id>/liberar-pagamento", methods=["POST"])
@somente_admin
def pagamento_liberar(id):
    """A Engenharia de Processos libera ao Financeiro o pagamento das parcelas após a HAWB."""
    r = carregar_invoice(id)
    if r["pagamento_liberado"]:
        flash("O pagamento deste invoice já está liberado.", "erro")
    elif not r["tem_parcela_prazo"]:
        flash("Este invoice não tem parcela após a HAWB para liberar.", "erro")
    else:
        obs = " ".join(request.form.get("obs", "").split())[:300] or None
        get_db().execute("UPDATE invoices SET pagamento_liberado_em = ?, pagamento_liberado_por = ?,"
                         " pagamento_liberado_obs = ? WHERE id = ?", (agora(), g.usuario["id"], obs, id))
        registrar_evento(id, "Pagamento após a HAWB liberado ao Financeiro (Engenharia de Processos)", obs)
        get_db().commit()
        flash("Pagamento liberado. O Financeiro será avisado por e-mail.", "ok")
    return redirect(url_for("invoice_detalhe", id=id) + "#pagamentos")


@app.route("/invoices/<int:id>/bloquear-pagamento", methods=["POST"])
@somente_admin
def pagamento_bloquear(id):
    """Desfaz a liberação (o pagamento após a HAWB volta a ficar bloqueado)."""
    r = carregar_invoice(id)
    if r["pagamento_liberado"]:
        get_db().execute("UPDATE invoices SET pagamento_liberado_em = NULL, pagamento_liberado_por = NULL,"
                         " pagamento_liberado_obs = NULL WHERE id = ?", (id,))
        registrar_evento(id, "Liberação do pagamento desfeita (Engenharia de Processos)")
        get_db().commit()
        flash("Liberação desfeita: o pagamento após a HAWB voltou a ficar bloqueado.", "ok")
    return redirect(url_for("invoice_detalhe", id=id) + "#pagamentos")


VER_NO_NAVEGADOR = ("pdf", "png", "jpg", "jpeg", "gif", "webp")


@app.route("/arquivos/<int:aid>")
@login_obrigatorio
def arquivo_baixar(aid):
    """Entrega o arquivo: PDF e imagens abrem no navegador (para a tela de visualização); ?baixar=1 força o download."""
    a = get_db().execute("SELECT * FROM arquivos WHERE id = ?", (aid,)).fetchone()
    if a is None or not os.path.exists(caminho_anexo(a)):
        abort(404)
    ext = a["nome"].rsplit(".", 1)[-1].lower()
    baixar = request.args.get("baixar") == "1" or ext not in VER_NO_NAVEGADOR
    return send_file(caminho_anexo(a), download_name=a["nome"], as_attachment=baixar)


def conteudo_para_visualizar(a):
    """(tipo, conteúdo) para a tela de visualização. Tipos: pdf, imagem, texto, planilha, nenhum."""
    ext = a["nome"].rsplit(".", 1)[-1].lower()
    arq = caminho_anexo(a)
    try:
        if ext == "pdf":
            return "pdf", None
        if ext in ("png", "jpg", "jpeg", "gif", "webp"):
            return "imagem", None
        if ext in ("txt", "csv", "xml", "ofx", "eml"):
            with open(arq, "rb") as f:
                bruto = f.read(300_000)
            for cod in ("utf-8", "latin-1"):
                try:
                    return "texto", bruto.decode(cod)
                except UnicodeDecodeError:
                    continue
        if ext == "xlsx":
            from openpyxl import load_workbook
            wb = load_workbook(arq, read_only=True, data_only=True)
            abas = []
            for ws in wb.worksheets[:10]:
                linhas = []
                for linha in ws.iter_rows(max_row=500, max_col=40, values_only=True):
                    linhas.append(["" if v is None else (fmt_data(v.strftime("%Y-%m-%d")) if hasattr(v, "strftime")
                                                         else str(v)) for v in linha])
                while linhas and not any(linhas[-1]):
                    linhas.pop()
                largura = max((max((i + 1 for i, v in enumerate(l) if v != ""), default=0) for l in linhas), default=0)
                linhas = [l[:largura] for l in linhas]
                abas.append((ws.title, linhas))
            wb.close()
            return "planilha", abas
        if ext == "docx":
            import zipfile
            from xml.etree import ElementTree as ET
            with zipfile.ZipFile(arq) as z:
                xml = z.read("word/document.xml")
            ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            paragrafos = ["".join(t.text or "" for t in p.iter(ns + "t")) for p in ET.fromstring(xml).iter(ns + "p")]
            return "texto", "\n".join(paragrafos)
    except Exception as e:  # arquivo corrompido ou num formato que não dá para ler: só o download fica disponível
        app.logger.warning("Visualização indisponível para o arquivo %s: %s", a["id"], e)
    return "nenhum", None


@app.route("/arquivos/<int:aid>/ver")
@login_obrigatorio
def arquivo_ver(aid):
    """Tela de visualização (abre em nova aba): mostra o arquivo e o botão para baixar uma cópia."""
    a = get_db().execute("SELECT a.*, u.nome AS usuario FROM arquivos a LEFT JOIN usuarios u ON u.id = a.enviado_por"
                         " WHERE a.id = ?", (aid,)).fetchone()
    if a is None or not os.path.exists(caminho_anexo(a)):
        abort(404)
    tipo, conteudo = conteudo_para_visualizar(a)
    invoice_id, descricao, _volta = info_origem(a)
    inv = get_db().execute("SELECT numero FROM invoices WHERE id = ?", (invoice_id,)).fetchone() if invoice_id else None
    return render_template("arquivo_ver.html", a=a, tipo=tipo, conteudo=conteudo, descricao=descricao,
                           origem=ORIGENS_ARQUIVO.get(a["origem"], a["origem"]), invoice_id=invoice_id,
                           invoice_numero=inv["numero"] if inv else None)


@app.route("/arquivos/<int:aid>/excluir", methods=["POST"])
@login_obrigatorio
def arquivo_excluir(aid):
    db = get_db()
    a = db.execute("SELECT * FROM arquivos WHERE id = ?", (aid,)).fetchone()
    if a is None:
        abort(404)
    invoice_id, descricao, volta_url = info_origem(a)
    voltar = (lambda: voltar_lancamentos(invoice_id)) if a["origem"] == "lancamento" and invoice_id \
        else (lambda: redirect(volta_url))
    if a["origem"] == "pagamento":
        permitido = pode("financeiro")
    elif a["origem"] in ("duimp", "invoice"):
        permitido = pode("importacao")
    elif a["origem"] in ("relatorio", "revisao", "relatorio_versao"):  # histórico do desconto: o relatório não é excluído
        flash("O relatório de inspeção faz parte do histórico do desconto e não pode ser excluído. "
              "Para corrigir, anexe a versão certa na edição da inspeção ou numa revalidação.", "erro")
        return voltar()
    else:
        permitido = g.usuario["perfil"] == "admin" or (pode_anexar() and a["enviado_por"] == g.usuario["id"])
    if not permitido:
        flash({"lancamento": "Só quem anexou o arquivo (ou o Administrador) pode excluí-lo.",
               "duimp": "Só a Importação pode excluir a DUIMP.",
               "invoice": "Só a Importação pode excluir o invoice assinado."}.get(a["origem"], "Só o Financeiro pode excluir o Swift."),
              "erro")
        return voltar()
    db.execute("DELETE FROM arquivos WHERE id = ?", (aid,))
    if invoice_id:
        registrar_evento(invoice_id, f"Arquivo excluído ({ORIGENS_ARQUIVO.get(a['origem'], a['origem'])})",
                         f"{descricao}: {a['nome']}")
    db.commit()
    remover_arquivos([a])
    flash(f"Arquivo excluído: {a['nome']}.", "ok")
    return voltar()


@app.errorhandler(413)
def arquivo_grande(_e):
    flash(f"Arquivo grande demais: o limite é {ANEXO_MAX_MB} MB por arquivo.", "erro")
    # (não lê o formulário: a requisição grande demais é descartada)
    return redirect(request.referrer or url_for("lancamentos"))


@app.route("/invoices/<int:id>/lancamentos/avisar", methods=["POST"])
@login_obrigatorio
def lancamentos_avisar(id):
    """Importação avisa o Financeiro, por e-mail, de tudo o que falta lançar no MEGA deste invoice."""
    if not pode_lancar():
        abort(403)
    r = carregar_invoice(id)
    db = get_db()
    pendentes = db.execute("SELECT * FROM lancamentos WHERE invoice_id = ? AND lancado_erp = 0"
                           " ORDER BY data_pagamento IS NULL, data_pagamento, id", (id,)).fetchall()
    if not pendentes:
        flash("Nada a lançar no MEGA neste invoice: nenhum e-mail enviado.", "aviso-box")
        return voltar_lancamentos(id)
    linhas = []
    arquivos = anexos_por_lancamento([l["id"] for l in pendentes])
    caminhos = documentos_de("lancamento", [l["id"] for l in pendentes])
    para_anexar = [(caminho_anexo(a), a["nome"]) for l in pendentes for a in arquivos.get(l["id"], [])
                   if os.path.exists(caminho_anexo(a))]
    total_bytes = sum(os.path.getsize(c) for c, _n in para_anexar)
    if total_bytes > 20 * 1024 * 1024:  # limite comum dos servidores de e-mail: ~25 MB
        para_anexar = []
    for l in pendentes:
        txt = f"{l['tipo'] or 'Sem tipo'} · " + (fmt_moeda(l["valor"], "BRL") if l["valor"] is not None else "valor não informado")
        txt += f" · pagamento {fmt_data(l['data_pagamento'])}" if l["data_pagamento"] else " · data não informada"
        if arquivos.get(l["id"]):
            txt += " · arquivos: " + ", ".join(a["nome"] for a in arquivos[l["id"]])
        if caminhos.get(l["id"]):
            txt += " · documentos na rede: " + " ; ".join(d["caminho"] for d in caminhos[l["id"]])
        if l["alterado_apos_erp"]:
            txt += " · ALTERADO após já ter sido lançado: conferir no MEGA"
        linhas.append(txt)
    total = round(sum(l["valor"] for l in pendentes if l["valor"] is not None), 2)
    linhas.append({"secao": f"Total a lançar: {fmt_moeda(total, 'BRL')} ({len(pendentes)} custo(s))"})
    msg = " ".join(request.form.get("mensagem", "").split())[:500]
    obs = f"Solicitado por {g.usuario['nome']} (Importação)."
    if msg:
        obs = f"Mensagem: {msg} — {obs}"
    if para_anexar:
        obs += f" Os {len(para_anexar)} arquivo(s) citados seguem anexados a este e-mail."
    elif any(arquivos.values()):
        obs += " Os arquivos citados são grandes demais para o e-mail: baixe-os na aba Lançamentos Financeiros."
    obs += " Depois de lançar, marque \"Lançado no MEGA\" em cada custo na aba Lançamentos Financeiros do sistema."
    status, erro = enfileirar_email("lancamentos_mega", r, f"{len(pendentes)} custo(s) a lançar", linhas, obs,
                                    anexos=para_anexar)
    registrar_evento(id, "Financeiro avisado dos custos a lançar no MEGA",
                     f"{len(pendentes)} custo(s), {fmt_moeda(total, 'BRL')}" + (f" · {msg}" if msg else ""))
    db.commit()
    if status == "PENDENTE":
        enviar_fila()
        final = db.execute("SELECT status, erro, para, cc FROM emails WHERE fluxo = 'lancamentos_mega'"
                           " AND invoice_id = ? ORDER BY id DESC LIMIT 1", (id,)).fetchone()
        if final["status"] == "ENVIADO":
            flash(f"E-mail enviado para {final['para']}" + (f" (cópia: {final['cc']})" if final["cc"] else "") +
                  f" com {len(pendentes)} custo(s) a lançar no MEGA.", "ok")
        else:
            flash(f"O e-mail não foi enviado agora ({final['erro'] or 'falha no envio'}); o sistema tenta de novo "
                  "automaticamente. Veja a aba E-mails.", "erro")
    else:
        flash(f"E-mail não enviado: {erro}" + (" Peça ao Administrador para configurar a aba E-mails."
                                                if g.usuario["perfil"] != "admin" else ""), "erro")
    return voltar_lancamentos(id)


@app.route("/lancamentos.csv")
@login_obrigatorio
def lancamentos_csv():
    saida = io.StringIO()
    w = csv.writer(saida, delimiter=";")
    w.writerow(["Fornecedor", "Invoice", "Tipo de pagamento", "Valor (R$)", "Data do pagamento",
                "Lançado no MEGA", "Lançado por", "Lançado em", "Apontado por"])
    for p in paineis_lancamentos(request.args):
        for l in p["lancamentos"]:
            w.writerow([p["inv"]["fornecedor"], p["inv"]["numero"], l["tipo"],
                        fmt_num(l["valor"]) if l["valor"] is not None else "",
                        fmt_data(l["data_pagamento"]) if l["data_pagamento"] else "",
                        "Sim" if l["lancado_erp"] else ("Não (alterado após lançamento)" if l["alterado_apos_erp"] else "Não"),
                        l["usuario_erp"] or "", fmt_data(l["lancado_erp_em"]) if l["lancado_erp_em"] else "",
                        l["usuario"] or ""])
    return Response("﻿" + saida.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=lancamentos-financeiros-{hoje()}.csv"})


# ---------------------------------------------------------------------------
# E-mails automáticos
#
# A cada alteração salva no sistema, a "fotografia" de previsibilidade de cada invoice (parcelas,
# janela de nacionalização, liberações, desconto, chegada...) é comparada com a anterior; as
# diferenças viram e-mails, na fila da tabela `emails`. O envio acontece logo após a resposta da
# página (o usuário não espera) pela conta de envio configurada na aba E-mails.
# ---------------------------------------------------------------------------
FLUXOS_EMAIL = {
    # chave: (título, quando é enviado, setor sugerido em "Para", setor sugerido em "Cc")
    "pagamento_programado": (
        "Pagamento programado",
        "Parcela antecipada (conclusão do cadastro + 5 dias úteis) e parcelas após a HAWB, assim que a data de "
        "vencimento e o valor ficam definidos.", "financeiro", ""),
    "pagamento_liberado": (
        "Pagamento liberado pela Engenharia de Processos",
        "Quando a Engenharia de Processos libera ao Financeiro o pagamento das parcelas após a HAWB "
        "(que ficam bloqueadas até essa liberação, mesmo vencidas).", "financeiro", ""),
    "pagamento_alterado": (
        "Pagamento alterado",
        "Mudança na data de vencimento ou no valor de uma parcela já programada (ex.: HAWB corrigida, itens, "
        "dedução, desconto) e cancelamento de invoice.", "financeiro", ""),
    "nacionalizacao": (
        "Impostos de nacionalização",
        "Janela de pagamento: ETA planejado −1 a +3 dias úteis; com o ETA real, ETA real a +2 dias úteis. "
        "Avisa no cadastro e em cada alteração.", "financeiro", ""),
    "banco_debito": (
        "Pendência do Financeiro na etapa de Embarque",
        "Pedido para o Financeiro informar o banco de débito, quando o ETA real é cadastrado (se o banco ainda "
        "estiver vazio).",
        "financeiro", ""),
    "duimp_registrada": (
        "DUIMP registrada: impostos aptos a lançamento no MEGA",
        "Quando a data de emissão da DUIMP é cadastrada (etapa Desembaraço) — e se ela for corrigida depois. "
        "Sugestão: Anny em Para e Gabriel (Importação) em Cc.",
        "financeiro", "importacao"),
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
    "lancamentos_mega": (
        "Custos a lançar no MEGA",
        "Enviado quando a Importação clica em \"Avisar o Financeiro\" num invoice da aba Lançamentos Financeiros: "
        "lista tudo o que falta lançar no MEGA daquele invoice. Sugestão: Anny em Para e o restante do Financeiro em Cc.",
        "financeiro", ""),
    "resumo_semanal": (
        "Resumo semanal de pendências do Financeiro",
        "Toda segunda-feira às 8h (Brasília): tudo o que o Financeiro precisa fazer, separado em vencidos, "
        "vencem na semana, próximos e ações sem data.", "financeiro", ""),
    "resumo_diario": (
        "Pendências do Financeiro vencidas ou que vencem hoje",
        "Todos os dias às 8h (Brasília): somente o que vence no dia ou já venceu. Se não houver nada, não envia.",
        "financeiro", ""),
}
HORA_RESUMOS = 8  # horário de Brasília dos resumos (o agendador externo deve chamar neste horário)
STATUS_EMAIL = {"PENDENTE": "Na fila", "ENVIADO": "Enviado", "ERRO": "Erro", "NAO_ENVIADO": "Não enviado"}
app.jinja_env.globals["STATUS_EMAIL"] = STATUS_EMAIL
SMTP_PADRAO = {"smtp_host": "smtp.gmail.com", "smtp_porta": "587",
               "smtp_nome": "Controle de Invoices · Quinelato"}
RE_EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
def agora_brasil():
    return agora_dt()


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
    return bool(config("smtp_host") and (config("smtp_remetente") or config("smtp_usuario")))


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
    itens = "".join(
        f'<li style="list-style:none;margin:14px 0 4px -20px;font-weight:bold;color:#0a2a7b">{escape(x["secao"])}</li>'
        if isinstance(x, dict) else f'<li style="margin:4px 0">{escape(x)}</li>' for x in linhas)
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


def enfileirar_email(fluxo, r, assunto_extra, linhas, obs=None, anexos=None):
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
        status, erro = "NAO_ENVIADO", "Conta de envio não configurada (aba E-mails)."
    else:
        status, erro = "PENDENTE", None
        g.ha_emails_na_fila = True
    get_db().execute("INSERT INTO emails (fluxo, invoice_id, assunto, corpo, para, cc, status, erro, criado_em, anexos)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (fluxo, r["id"] if r is not None else None, assunto, corpo, para, cc, status, erro, agora(),
                      json.dumps(anexos, ensure_ascii=False) if anexos else None))
    return status, erro


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
        "eta_real": r["eta_real"],
        "liberado": r["pagamento_liberado"],
        "duimp": r["data_registro_br"],
        "banco": bool(r["banco_debito"]),
        "chegada": r["data_chegada"],
        "desconto": r["desconto"]["id"] if r["desconto"] else None,
        "lib": {p["texto"]: {"valor": p["valor"], "venc": p["vencimento"]}
                for p in r["parcelas_liberacao"] if p["status"] == "DISPONIVEL"},
    }


def descrever_parcela(r, p):
    if p["tipo"] == "ANTECIPADO":
        regra = (f"cadastro concluído em {fmt_data((r['cadastro_concluido_em'] or '')[:10])} + {PRAZO_ANTECIPADO_DIAS} dias úteis"
                 if r["cadastro_concluido_em"] else "aguardando a conclusão do cadastro")
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
                               f"valor a pagar {fmt_moeda(n['valor'], m)}"
                               + (" · bloqueado até a liberação da Engenharia de Processos" if p["bloqueado"] else ""))
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
    # pedido ao Financeiro somente quando o ETA real é cadastrado e o banco ainda está vazio
    # (fotografias antigas, sem "eta_real", contam como sem mudança: não dispara para o histórico)
    if novo.get("eta_real") and not antigo.get("eta_real", novo.get("eta_real")) and not novo["banco"]:
        # texto neutro: pedidos do tipo "informe o banco" são confundidos com golpe pelos filtros de e-mail
        enfileirar_email("banco_debito", r, None,
                         [f"ETA real cadastrado em {fmt_data(novo['eta_real'])}.",
                          "Pendência do Financeiro no sistema Controle de Invoices: preencher o campo "
                          "\"Banco de débito\" na etapa de Embarque (seção Logística do invoice)."],
                         "A etapa de Embarque é concluída quando esse campo estiver preenchido no sistema.")

    # pagamento após a HAWB liberado pela Engenharia de Processos
    # (fotografias antigas, sem "liberado", contam como sem mudança)
    if novo.get("liberado") and not antigo.get("liberado", novo.get("liberado")) and r["tem_parcela_prazo"]:
        linhas = [f"{descrever_parcela(r, p)}: vencimento "
                  + (fmt_data(p["vencimento"]) if p["vencimento"] else "a definir (aguardando HAWB)")
                  + f" · valor a pagar {fmt_moeda(p['pendente'], m)}"
                  for p in r["parcelas"] if p["tipo"] == "PRAZO" and p["pendente"] > TOLERANCIA]
        quem = get_db().execute("SELECT nome FROM usuarios WHERE id = ?", (r["pagamento_liberado_por"],)).fetchone()
        obs = (f"Liberado por {quem['nome'] if quem else 'Engenharia de Processos'} em "
               f"{fmt_data(r['pagamento_liberado_em'])}.")
        if r["pagamento_liberado_obs"]:
            obs += f" Observação: {r['pagamento_liberado_obs']}"
        obs += " O pagamento já pode ser realizado e registrado no sistema."
        enfileirar_email("pagamento_liberado", r, None, linhas or ["Parcelas após a HAWB liberadas para pagamento."], obs)

    # DUIMP registrada: impostos aptos a lançamento no MEGA
    # (fotografias antigas, sem "duimp", contam como sem mudança: não dispara para o histórico)
    duimp_antes = antigo.get("duimp", novo.get("duimp"))
    if novo.get("duimp") and novo["duimp"] != duimp_antes:
        corrigida = bool(duimp_antes)
        linhas = [f"DUIMP registrada: data de emissão {fmt_data(novo['duimp'])}"
                  + (f" (corrigida; antes {fmt_data(duimp_antes)})" if corrigida else "") + ".",
                  "Os impostos da nacionalização deste invoice estão aptos a lançamento no MEGA."]
        pend = get_db().execute("SELECT tipo, valor, data_pagamento FROM lancamentos WHERE invoice_id = ?"
                                " AND lancado_erp = 0 ORDER BY id", (r["id"],)).fetchall()
        if pend:
            linhas.append({"secao": f"Custos já apontados pela Importação a lançar no MEGA ({len(pend)})"})
            linhas += [f"{l['tipo'] or 'Sem tipo'} · "
                       + (fmt_moeda(l["valor"], "BRL") if l["valor"] is not None else "valor não informado")
                       + (f" · pagamento {fmt_data(l['data_pagamento'])}" if l["data_pagamento"] else "")
                       for l in pend]
        enfileirar_email("duimp_registrada", r, "data corrigida" if corrigida else None, linhas,
                         "Os valores e arquivos dos impostos ficam na aba Lançamentos Financeiros do sistema; "
                         "depois de lançar, marque \"Lançado no MEGA\" em cada custo.")

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


# ---------- resumos do Financeiro ----------
def linha_pendencia(p):
    txt = f"{p['r']['fornecedor']} · {p['r']['numero']}: {p['acao']}"
    if p["data"]:
        txt += f" · {fmt_data(p['data'])}"
    if p["valor"] is not None:
        txt += f" · {fmt_moeda(p['valor'], p['r']['moeda'])}"
    return txt


def gerar_resumo(tipo):
    """tipo 'diario': só o que vence hoje ou já venceu; 'semanal': todas as pendências do Financeiro."""
    br = agora_brasil()
    dia = br.date().isoformat()
    pend = [p for p in listar_pendencias(todos_invoices(), perfil="financeiro") if p["quem"] == "financeiro"]
    vencidos = [p for p in pend if p["data"] and p["data"] < dia]
    hoje_ = [p for p in pend if p["data"] == dia]
    if tipo == "diario":
        grupos = [("Vencidos", vencidos), ("Vencem hoje", hoje_)]
        obs = "Por favor, realize hoje as ações acima e registre-as no sistema."
    else:
        domingo = (br.date() + timedelta(days=6 - br.weekday())).isoformat()
        grupos = [("Vencidos", vencidos), ("Vencem hoje", hoje_),
                  (f"Vencem nesta semana (até {fmt_data(domingo)})", [p for p in pend if p["data"] and dia < p["data"] <= domingo]),
                  ("Próximos vencimentos", [p for p in pend if p["data"] and p["data"] > domingo]),
                  ("Ações sem data (registrar no sistema)", [p for p in pend if not p["data"]])]
        obs = "Programação da semana do Financeiro. Registre no sistema cada ação realizada."
    total = sum(len(itens) for _t, itens in grupos)
    nome = "resumo diário" if tipo == "diario" else "resumo semanal"
    if not total:
        return f"{nome.capitalize()}: nada a enviar."
    linhas = []
    for titulo, itens in grupos:
        if itens:
            linhas.append({"secao": f"{titulo} ({len(itens)})"})
            linhas += [linha_pendencia(p) for p in itens]
    fluxo = "resumo_diario" if tipo == "diario" else "resumo_semanal"
    enfileirar_email(fluxo, None, f"{total} pendência(s) · {br.strftime('%d/%m/%Y')}", linhas, obs)
    get_db().commit()
    return f"{nome.capitalize()} gerado com {total} pendência(s)."


def executar_resumos(pelo_agendador=False):
    """Gera os resumos do dia (uma vez por dia; o semanal só às segundas-feiras).
    Pelo agendador o horário é o dele; no acesso ao sistema (reserva), só a partir das 8h."""
    br = agora_brasil()
    dia = br.date().isoformat()
    if pelo_agendador:
        salvar_config("agendador_ultimo", br.strftime("%Y-%m-%d %H:%M:%S"))
        get_db().commit()
    elif br.hour < HORA_RESUMOS:
        return []
    msgs = []
    if config("resumo_ultimo") != dia:
        salvar_config("resumo_ultimo", dia)
        get_db().commit()
        msgs.append(gerar_resumo("diario"))
    if br.weekday() == 0 and config("resumo_semanal_ultimo") != dia:
        salvar_config("resumo_semanal_ultimo", dia)
        get_db().commit()
        msgs.append(gerar_resumo("semanal"))
    return msgs or ["Os resumos de hoje já tinham sido processados."]


def resumos_pendentes_hoje():
    if config("email_fluxos") is None:
        return False
    br = agora_brasil()
    dia = br.date().isoformat()
    return br.hour >= HORA_RESUMOS and (config("resumo_ultimo") != dia or
                                        (br.weekday() == 0 and config("resumo_semanal_ultimo") != dia))


# ---------- envio ----------
SEGURANCA_SMTP = {"ssl": "SSL/TLS (porta 465)", "starttls": "STARTTLS (porta 587)", "nenhuma": "Nenhuma"}


def seguranca_smtp():
    padrao = "ssl" if config("smtp_porta", SMTP_PADRAO["smtp_porta"]) == "465" else "starttls"
    valor = config("smtp_seguranca", padrao)
    return valor if valor in SEGURANCA_SMTP else padrao


def conectar_smtp():
    """Conecta no servidor de envio configurado na aba E-mails (Outlook/Exchange da empresa, Gmail etc.)."""
    host = config("smtp_host", SMTP_PADRAO["smtp_host"])
    porta = int(config("smtp_porta", SMTP_PADRAO["smtp_porta"]))
    seg = seguranca_smtp()
    try:
        if seg == "ssl":
            srv = smtplib.SMTP_SSL(host, porta, timeout=20, context=ssl.create_default_context())
        else:
            srv = smtplib.SMTP(host, porta, timeout=20)
            srv.ehlo()
            if seg == "starttls":
                srv.starttls(context=ssl.create_default_context())
                srv.ehlo()
    except (OSError, smtplib.SMTPException) as e:
        raise RuntimeError(explicar_falha_conexao(host, porta, e)) from e
    if config("smtp_usuario") and config("smtp_senha"):  # servidor que exige autenticação
        try:
            srv.login(config("smtp_usuario"), config("smtp_senha"))
        except smtplib.SMTPAuthenticationError as e:
            srv.close()
            raise RuntimeError(f"Usuário ou senha recusados pelo servidor {host}: confira a conta de envio ({e}).") from e
    return srv


def em_pythonanywhere():
    return bool(os.environ.get("PYTHONANYWHERE_DOMAIN") or os.environ.get("PYTHONANYWHERE_SITE"))


def explicar_falha_conexao(host, porta, erro):
    msg = f"Não foi possível conectar em {host}:{porta} ({erro})."
    if "gmail.com" not in host.lower() and em_pythonanywhere():
        msg += (" No PythonAnywhere, contas gratuitas só podem enviar e-mails pelo Gmail; para usar o servidor"
                " da empresa é preciso um plano pago (ou manter o envio pela conta Gmail).")
    return msg


def montar_mensagem(assunto, html, para, cc, anexos=None):
    msg = EmailMessage()
    msg["Subject"] = assunto
    remetente = config("smtp_remetente") or config("smtp_usuario")
    msg["From"] = formataddr((config("smtp_nome", SMTP_PADRAO["smtp_nome"]), remetente))
    msg["To"] = ", ".join(lista_emails(para))
    if lista_emails(cc):
        msg["Cc"] = ", ".join(lista_emails(cc))
    msg.set_content(texto_simples(html))
    msg.add_alternative(html, subtype="html")
    if "cid:" + CID_ASSINATURA in html:  # v4.62: imagem da assinatura da empresa embutida no corpo
        img = imagem_assinatura()
        if img:
            with open(caminho_anexo(img), "rb") as arq:
                sub_tipo = (mimetypes.guess_type(img["nome"])[0] or "image/png").split("/", 1)[1]
                msg.get_payload()[1].add_related(arq.read(), maintype="image", subtype=sub_tipo, cid=f"<{CID_ASSINATURA}>")
    for caminho, nome in anexos or []:
        if not os.path.exists(caminho):
            continue
        tipo = mimetypes.guess_type(nome)[0] or "application/octet-stream"
        principal, sub = tipo.split("/", 1)
        with open(caminho, "rb") as arq:
            msg.add_attachment(arq.read(), maintype=principal, subtype=sub, filename=nome)
    return msg


def rascunho_outlook(assunto, html, para, cc, anexos):
    """v4.69: e-mail pronto como arquivo .eml marcado como "não enviado" (X-Unsent): o Outlook do computador o abre
    como um e-mail novo, para revisar e enviar pela conta que está logada lá. Sem remetente: o Outlook usa a conta padrão.
    v4.70: só a versão HTML (sem a alternativa em texto simples, que o Outlook escolhia ao abrir o rascunho e perdia toda
    a formatação), num documento HTML completo. Estrutura: mixed → related (HTML + imagem da assinatura) + anexos.
    anexos: [(nome, bytes)]"""
    from email.mime.application import MIMEApplication
    from email.mime.image import MIMEImage
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    documento = ('<!DOCTYPE html><html><head><meta http-equiv="Content-Type" content="text/html; charset=utf-8">'
                 '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
                 f'<body style="margin:0;padding:0">{html}</body></html>')
    raiz = MIMEMultipart("mixed")
    raiz["X-Unsent"] = "1"
    raiz["Subject"] = assunto
    if lista_emails(para):
        raiz["To"] = ", ".join(lista_emails(para))
    if lista_emails(cc):
        raiz["Cc"] = ", ".join(lista_emails(cc))
    corpo = MIMEMultipart("related")
    corpo.attach(MIMEText(documento, "html", "utf-8"))
    if "cid:" + CID_ASSINATURA in html:
        img = imagem_assinatura()
        if img:
            with open(caminho_anexo(img), "rb") as arq:
                sub_tipo = (mimetypes.guess_type(img["nome"])[0] or "image/png").split("/", 1)[1]
                parte = MIMEImage(arq.read(), _subtype=sub_tipo)
            parte.add_header("Content-ID", f"<{CID_ASSINATURA}>")
            parte.add_header("Content-Disposition", "inline", filename=img["nome"])
            corpo.attach(parte)
    raiz.attach(corpo)
    for nome, conteudo in anexos:
        tipo = mimetypes.guess_type(nome)[0] or "application/octet-stream"
        parte = MIMEApplication(conteudo, _subtype=tipo.split("/", 1)[1])
        parte.add_header("Content-Disposition", "attachment", filename=nome)
        raiz.attach(parte)
    return raiz.as_bytes()


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
                srv.send_message(montar_mensagem(em["assunto"], em["corpo"], em["para"], em["cc"],
                                                 json.loads(em["anexos"]) if em["anexos"] else None))
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
                executar_resumos()
            enviar_fila()
        except Exception:
            app.logger.exception("Falha nas tarefas de e-mail")


def tarefa_diaria():
    """Para tarefa agendada (deploy/tarefa_diaria.sh): resumos + fila."""
    with app.app_context():
        msg = " ".join(executar_resumos(pelo_agendador=True))
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
        precisa_resumo = resumos_pendentes_hoje()  # reserva, caso o agendador não tenha chamado
        if g.get("ha_emails_na_fila") or precisa_resumo:
            resp.call_on_close(lambda: tarefas_em_segundo_plano(resumo=precisa_resumo))
    except Exception:
        app.logger.exception("Falha ao agendar e-mails")
    return resp


@app.route("/tarefas/diaria/<chave>")
def tarefa_diaria_url(chave):
    """Endereço para um agendador externo (ex.: cron-job.org) chamar todo dia às 8h (Brasília)."""
    certa = config("tarefa_chave")
    if not certa or not secrets.compare_digest(certa, chave):
        abort(404)
    msg = " ".join(executar_resumos(pelo_agendador=True))
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
        smtp={k: config(k, SMTP_PADRAO.get(k, "")) for k in ("smtp_host", "smtp_porta", "smtp_usuario", "smtp_nome",
                                                               "smtp_remetente")},
        smtp_seguranca=seguranca_smtp(), SEGURANCA_SMTP=SEGURANCA_SMTP,
        pythonanywhere=em_pythonanywhere(),
        tem_senha=bool(config("smtp_senha")), agendador_ultimo=config("agendador_ultimo"),
        resumo_ultimo=config("resumo_ultimo"), resumo_semanal_ultimo=config("resumo_semanal_ultimo"), historico=historico, contagem=contagem,
        url_tarefa=re.sub(r"^http://(?=[^/]*pythonanywhere\.com)", "https://",
                          url_for("tarefa_diaria_url", chave=config("tarefa_chave"), _external=True)),
        FLUXOS_EMAIL=FLUXOS_EMAIL, STATUS_EMAIL=STATUS_EMAIL, copia=copia_destinatarios())


@app.route("/emails/conta", methods=["POST"])
@somente_admin
def emails_conta():
    f = request.form
    usuario = f.get("smtp_usuario", "").strip()
    remetente = f.get("smtp_remetente", "").strip().lower()
    host = f.get("smtp_host", "").strip().lower()
    porta = f.get("smtp_porta", "").strip()
    seg = f.get("smtp_seguranca") or ("ssl" if porta == "465" else "starttls")
    if not host:
        flash("Informe o servidor SMTP.", "erro")
    elif not porta.isdigit():
        flash("A porta deve ser um número (ex.: 465 com SSL, 587 com STARTTLS).", "erro")
    elif seg not in SEGURANCA_SMTP:
        flash("Escolha a criptografia.", "erro")
    elif remetente and not RE_EMAIL.match(remetente):
        flash("E-mail do remetente inválido.", "erro")
    elif not remetente and usuario and not RE_EMAIL.match(usuario.lower()):
        flash("O usuário não é um e-mail: informe também o e-mail do remetente.", "erro")
    else:
        salvar_config("smtp_usuario", usuario)
        salvar_config("smtp_remetente", remetente or None)
        salvar_config("smtp_host", host)
        salvar_config("smtp_porta", porta)
        salvar_config("smtp_seguranca", seg)
        salvar_config("smtp_nome", f.get("smtp_nome", "").strip() or SMTP_PADRAO["smtp_nome"])
        senha = f.get("smtp_senha", "")
        if senha.strip():
            # senha de app do Gmail vem em grupos com espaços; nas demais contas a senha é guardada como digitada
            salvar_config("smtp_senha", senha.replace(" ", "") if "gmail.com" in host else senha)
        if f.get("remover_senha"):
            salvar_config("smtp_senha", None)
        get_db().commit()
        flash("Conta de envio salva. Use \"Enviar teste\" para conferir.", "ok")
    return redirect(url_for("emails_config") + "#conta")


@app.route("/emails/teste", methods=["POST"])
@somente_admin
def emails_teste():
    destino = request.form.get("destino", "").strip()
    if not RE_EMAIL.match(destino):
        flash("Informe um e-mail válido para o teste.", "erro")
    elif not conta_configurada():
        flash("Configure a conta de envio (servidor, usuário e senha) antes do teste.", "erro")
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
    if erros:
        for e in erros:
            flash(e, "erro")
    else:
        salvar_config("email_fluxos", json.dumps(novos, ensure_ascii=False))
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
    elif acao in ("resumo_diario", "resumo_semanal"):
        msg = gerar_resumo("diario" if acao == "resumo_diario" else "semanal")
        n = enviar_fila()
        flash(f"{msg} E-mails enviados agora: {n}.", "ok")
    elif acao == "nova_chave":
        salvar_config("tarefa_chave", secrets.token_urlsafe(24))
        db.commit()
        flash("Novo endereço gerado: atualize-o no agendador externo.", "ok")
    elif acao == "guardar_destinatarios":
        atual = config("email_fluxos")
        if atual is None:
            flash("Ainda não há destinatários salvos para guardar.", "erro")
        else:
            salvar_config("email_fluxos_copia", json.dumps(
                {"guardado_em": agora(), "por": g.usuario["nome"], "fluxos": json.loads(atual),
                 "resumo_hora": config("resumo_hora")}, ensure_ascii=False))
            db.commit()
            flash("Configuração atual dos destinatários guardada. Para voltar a ela, use \"Restaurar\".", "ok")
        return redirect(url_for("emails_config") + "#fluxos")
    elif acao == "restaurar_destinatarios":
        c = copia_destinatarios()
        if not c:
            flash("Não há configuração guardada para restaurar.", "erro")
        else:
            # fluxos criados depois da cópia mantêm a configuração atual
            restaurado = {**config_fluxos(), **c["fluxos"]}
            salvar_config("email_fluxos", json.dumps(restaurado, ensure_ascii=False))
            db.commit()
            flash(f"Destinatários restaurados para a configuração guardada em {fmt_data(c['guardado_em'])} "
                  f"por {c['por']}.", "ok")
        return redirect(url_for("emails_config") + "#fluxos")
    elif acao == "enviar_fila":
        flash(f"E-mails enviados agora: {enviar_fila()}.", "ok")
    return redirect(url_for("emails_config") + "#historico")


def copia_destinatarios():
    """Cópia guardada dos destinatários (para testes: guardar antes, restaurar depois)."""
    try:
        c = json.loads(config("email_fluxos_copia") or "null")
    except ValueError:
        return None
    if not c:
        return None
    c["linhas"] = [{"titulo": FLUXOS_EMAIL[k][0], **v} for k, v in c["fluxos"].items() if k in FLUXOS_EMAIL]
    return c


@app.route("/emails/<int:eid>")
@somente_admin
def email_ver(eid):
    em = get_db().execute("SELECT * FROM emails WHERE id = ?", (eid,)).fetchone()
    if em is None:
        abort(404)
    return render_template("email_ver.html", em=em, STATUS_EMAIL=STATUS_EMAIL, FLUXOS_EMAIL=FLUXOS_EMAIL)


# ---------------------------------------------------------------------------
# Redefinição de senha pelo próprio usuário (código de 6 dígitos enviado por e-mail)
# Código em vez de link: os e-mails do sistema não levam links, e filtros como o do Outlook
# costumam bloquear ou "pré-clicar" links de remetentes externos.
# ---------------------------------------------------------------------------
CODIGO_VALIDADE_MIN = 15
CODIGO_MAX_TENTATIVAS = 5
CODIGO_MAX_POR_HORA = 3


def enviar_email_direto(destino, assunto, html):
    """Envio imediato e fora da fila (o conteúdo sensível não fica guardado no histórico de e-mails)."""
    srv = conectar_smtp()
    try:
        srv.send_message(montar_mensagem(assunto, html, destino, ""))
    finally:
        try:
            srv.quit()
        except Exception:
            pass


@app.route("/redefinir-senha", methods=["GET", "POST"])
def redefinir_senha():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        db = get_db()
        u = db.execute("SELECT * FROM usuarios WHERE email = ? AND ativo = 1", (email,)).fetchone()
        if u:
            uma_hora = (agora_dt() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            recentes = db.execute("SELECT COUNT(*) FROM senha_codigos WHERE usuario_id = ? AND criado_em > ?",
                                  (u["id"], uma_hora)).fetchone()[0]
            if recentes >= CODIGO_MAX_POR_HORA:
                flash("Já foram enviados vários códigos na última hora. Use o último código recebido ou "
                      "tente novamente mais tarde.", "erro")
                return redirect(url_for("redefinir_senha_codigo", email=email))
            if not conta_configurada():
                flash("O envio de e-mails do sistema não está configurado. Peça ao Administrador para "
                      "redefinir sua senha.", "erro")
                return render_template("redefinir_senha.html", email=email)
            codigo = f"{secrets.randbelow(10 ** 6):06d}"
            # só o último código vale
            db.execute("UPDATE senha_codigos SET usado_em = ? WHERE usuario_id = ? AND usado_em IS NULL",
                       (agora(), u["id"]))
            expira = (agora_dt() + timedelta(minutes=CODIGO_VALIDADE_MIN)).strftime("%Y-%m-%d %H:%M:%S")
            db.execute("INSERT INTO senha_codigos (usuario_id, codigo_hash, criado_em, expira_em, tentativas)"
                       " VALUES (?, ?, ?, ?, 0)", (u["id"], generate_password_hash(codigo), agora(), expira))
            db.commit()
            html = corpo_email("Redefinição de senha", None, [
                f"Olá, {u['nome'].split()[0]}.",
                f"Seu código para redefinir a senha do sistema Controle de Invoices é: {codigo}",
                f"O código vale por {CODIGO_VALIDADE_MIN} minutos e só pode ser usado uma vez.",
            ], "Se você não pediu a redefinição, ignore este e-mail: sua senha atual continua valendo.")
            try:
                enviar_email_direto(email, f"[Invoices] Código para redefinir a senha: {codigo}", html)
                db.execute("INSERT INTO emails (fluxo, assunto, corpo, para, status, criado_em, enviado_em,"
                           " tentativas) VALUES ('redefinir_senha', ?, ?, ?, 'ENVIADO', ?, ?, 1)",
                           ("[Invoices] Código para redefinir a senha", "<p>Código de redefinição de senha "
                            "(não guardado no histórico).</p>", email, agora(), agora()))
                db.commit()
            except Exception as e:
                app.logger.exception("Falha ao enviar o código de redefinição de senha")
                flash(f"Não foi possível enviar o e-mail agora ({e}). Tente novamente ou peça ao "
                      "Administrador para redefinir sua senha.", "erro")
                return render_template("redefinir_senha.html", email=email)
        # mesma resposta para e-mail cadastrado ou não (não revela quem tem acesso)
        flash(f"Se o e-mail {email} estiver cadastrado e ativo, você receberá em instantes um código de 6 dígitos "
              f"(confira também o lixo eletrônico).", "ok")
        return redirect(url_for("redefinir_senha_codigo", email=email))
    return render_template("redefinir_senha.html", email=request.args.get("email", ""))


@app.route("/redefinir-senha/codigo", methods=["GET", "POST"])
def redefinir_senha_codigo():
    email = (request.values.get("email") or "").strip().lower()
    if request.method == "POST":
        codigo = re.sub(r"\D", "", request.form.get("codigo", ""))
        nova, nova2 = request.form.get("senha", ""), request.form.get("senha2", "")
        db = get_db()
        u = db.execute("SELECT * FROM usuarios WHERE email = ? AND ativo = 1", (email,)).fetchone()
        c = db.execute("SELECT * FROM senha_codigos WHERE usuario_id = ? AND usado_em IS NULL"
                       " ORDER BY id DESC LIMIT 1", (u["id"],)).fetchone() if u else None
        erro = None
        if len(nova) < 8:
            erro = "A nova senha deve ter no mínimo 8 caracteres."
        elif nova != nova2:
            erro = "A confirmação não confere com a nova senha."
        elif c is None or c["expira_em"] < agora():
            erro = "Código inválido ou expirado. Peça um novo código."
        elif not check_password_hash(c["codigo_hash"], codigo):
            tentativas = c["tentativas"] + 1
            db.execute("UPDATE senha_codigos SET tentativas = ?, usado_em = ? WHERE id = ?",
                       (tentativas, agora() if tentativas >= CODIGO_MAX_TENTATIVAS else None, c["id"]))
            db.commit()
            erro = ("Código incorreto." if tentativas < CODIGO_MAX_TENTATIVAS
                    else "Código incorreto várias vezes: ele foi cancelado. Peça um novo código.")
        if erro:
            flash(erro, "erro")
            return render_template("redefinir_senha_codigo.html", email=email, validade=CODIGO_VALIDADE_MIN)
        db.execute("UPDATE usuarios SET senha_hash = ? WHERE id = ?", (generate_password_hash(nova), u["id"]))
        db.execute("UPDATE senha_codigos SET usado_em = ? WHERE id = ?", (agora(), c["id"]))
        db.execute("DELETE FROM tentativas_login WHERE email = ?", (email,))
        db.commit()
        flash("Senha redefinida. Entre com a nova senha.", "ok")
        return redirect(url_for("login"))
    return render_template("redefinir_senha_codigo.html", email=email, validade=CODIGO_VALIDADE_MIN)


# ===========================================================================
# CARGA DE HISTÓRICO (TEMPORÁRIA)
# Cadastro simplificado de processos do passado para deixar o sistema em dia. Para remover depois:
# apagar este bloco, o template carga_historico.html e o link "Carga de histórico" do base.html.
# Os invoices já carregados continuam válidos (coluna invoices.historico / pagamentos.historico).
# ===========================================================================
@app.route("/carga-historico", methods=["GET", "POST"])
@somente_admin
def carga_historico():
    db = get_db()
    form = request.form if request.method == "POST" else {}
    if request.method == "POST":
        erros, dados = ler_carga_historico(request.form)
        if not erros:
            iid = gravar_carga_historico(dados)
            flash(f"Invoice {dados['numero']} ({dados['fornecedor']}) carregado do histórico.", "ok")
            return redirect(url_for("carga_historico") + f"#h-{iid}")
        for e in erros:
            flash(e, "erro")
    carregados = [calcular(inv) for inv in db.execute(
        SQL_INVOICE + " WHERE i.historico = 1 ORDER BY i.id DESC").fetchall()]
    fornecedores = db.execute("SELECT nome FROM fornecedores ORDER BY nome").fetchall()
    return render_template("carga_historico.html", carregados=carregados, fornecedores=fornecedores, f=form,
                           STATUS_DESCONTO=STATUS_DESCONTO)


def ler_carga_historico(f):
    erros = []
    d = {"fornecedor": " ".join(f.get("fornecedor", "").split()), "numero": f.get("numero", "").strip(),
         "moeda": f.get("moeda", ""), "data_assinatura": data_valida(f.get("data_assinatura")),
         "data_hawb": data_valida(f.get("data_hawb")), "valor": parse_num(f.get("valor")),
         "obs": f.get("obs", "").strip() or None}
    if not d["fornecedor"]:
        erros.append("Informe o fornecedor.")
    if not d["numero"]:
        erros.append("Informe o número do invoice.")
    if d["moeda"] not in MOEDAS:
        erros.append("Selecione a moeda.")
    if not d["data_assinatura"]:
        erros.append("Informe a data de assinatura.")
    if f.get("data_hawb") and not d["data_hawb"]:
        erros.append("Data da HAWB inválida.")
    if d["valor"] is None or d["valor"] <= 0:
        erros.append("Informe o valor total do invoice.")
    if not erros:
        fid = obter_fornecedor(d["fornecedor"], criar=False)
        if fid and get_db().execute("SELECT 1 FROM invoices WHERE fornecedor_id = ? AND numero = ?",
                                    (fid, d["numero"])).fetchone():
            erros.append(f"O invoice {d['numero']} de {d['fornecedor']} já existe no sistema.")
    # condição de pagamento
    cond = []
    for pct, tipo, dias in zip(f.getlist("cond_pct"), f.getlist("cond_tipo"), f.getlist("cond_dias") +
                               [""] * len(f.getlist("cond_pct"))):
        p = parse_num(pct)
        if p is None:
            continue
        if tipo not in CONDICOES or p <= 0 or p > 100:
            erros.append("Condição de pagamento: percentual ou tipo inválido.")
            continue
        if tipo == "PRAZO" and not dias.strip().isdigit():
            erros.append(f"Condição de pagamento: informe os dias após a HAWB da parcela de {fmt_qtd(p)}%.")
            continue
        cond.append({"percentual": p, "tipo": tipo, "dias": int(dias) if tipo == "PRAZO" else None})
    if not cond:
        erros.append("Informe a condição de pagamento.")
    elif abs(sum(c["percentual"] for c in cond) - 100) > 0.01:
        erros.append("A condição de pagamento precisa somar 100%.")
    d["condicao"] = cond
    # desconto
    desc_txt = f.get("desconto", "").strip()
    d["desconto"] = parse_num(desc_txt) if desc_txt else None
    d["desconto_status"] = f.get("desconto_status", "APROVADO")
    seg_txt = f.get("segurado", "").strip()
    d["segurado"] = parse_num(seg_txt) if seg_txt else d["desconto"]
    if desc_txt and (d["desconto"] is None or d["desconto"] <= 0):
        erros.append("Valor do desconto inválido (deixe vazio se não houve desconto).")
    elif d["desconto"]:
        if d["desconto_status"] not in STATUS_DESCONTO:
            erros.append("Selecione o status do desconto.")
        if d["segurado"] is None or d["segurado"] < 0:
            erros.append("Valor segurado inválido.")
        elif d["valor"] and d["segurado"] > d["valor"] + TOLERANCIA:
            erros.append("O valor segurado não pode ser maior que o valor do invoice.")
    else:
        d["segurado"] = 0
    # pagamentos já realizados
    pags = []
    for data_p, tipo, valor, banco in zip(f.getlist("pg_data"), f.getlist("pg_tipo"), f.getlist("pg_valor"),
                                          f.getlist("pg_banco")):
        if not (data_p or valor.strip()):
            continue
        v, dt = parse_num(valor), data_valida(data_p)
        if not dt or v is None or v <= 0 or tipo not in TIPOS_PAGAMENTO:
            erros.append("Pagamentos: informe data, tipo e valor de cada pagamento.")
            continue
        pags.append({"data": dt, "tipo": tipo, "valor": round(v, 2), "banco": banco.strip() or None})
    principal = sum(p["valor"] for p in pags if p["tipo"] == "PRINCIPAL")
    liberado = sum(p["valor"] for p in pags if p["tipo"] == "LIBERACAO")
    if d["valor"] and principal > d["valor"] - (d["segurado"] or 0) + TOLERANCIA:
        erros.append(f"Os pagamentos do invoice ({fmt_num(principal)}) passam do valor a pagar "
                     f"({fmt_num(d['valor'] - (d['segurado'] or 0))} = valor total − valor segurado).")
    if liberado > (d["segurado"] or 0) + TOLERANCIA:
        erros.append("As liberações do segurado passam do valor segurado.")
    d["pagamentos"] = pags
    return erros, d


def gravar_carga_historico(d):
    db = get_db()
    fid = obter_fornecedor(d["fornecedor"])
    unica = d["condicao"][0] if len(d["condicao"]) == 1 else None
    # v4.51: com desconto informado, a inspeção já está feita; sem desconto, fica para a Engenharia registrar
    data_insp = (d["data_hawb"] or d["data_assinatura"]) if d["desconto"] else None
    iid = db.execute(
        "INSERT INTO invoices (fornecedor_id, numero, moeda, data_assinatura, condicao_tipo, condicao_dias, data_hawb,"
        " data_fim_inspecao, tem_desconto, obs, historico, pagamento_liberado_em, pagamento_liberado_por,"
        " pagamento_liberado_obs, criado_por, criado_em, cadastro_concluido_em)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
        (fid, d["numero"], d["moeda"], d["data_assinatura"], unica["tipo"] if unica else "MISTA",
         unica["dias"] if unica else None, d["data_hawb"], data_insp, 1 if d["desconto"] else None, d["obs"],
         agora(), g.usuario["id"], "Carga de histórico", g.usuario["id"], agora(), d["data_assinatura"])).lastrowid
    salvar_condicao(iid, d["condicao"])
    db.execute("INSERT INTO invoice_itens (invoice_id, codigo, item, quantidade, valor_unit_original, valor_unit)"
               " VALUES (?, 'HIST', 'Valor total do invoice (carga de histórico)', 1, ?, ?)", (iid, d["valor"], d["valor"]))
    if d["desconto"]:
        aprovado = d["desconto_status"] == "APROVADO"
        db.execute("INSERT INTO descontos (invoice_id, valor_inicial, valor_final, valor_segurado, data_relatorio, status,"
                   " aprovado_em, aprovado_por, obs, criado_por, criado_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (iid, d["desconto"], d["desconto"], round(d["segurado"], 2), data_insp, d["desconto_status"],
                    agora() if aprovado else None, g.usuario["id"] if aprovado else None, "Carga de histórico",
                    g.usuario["id"], agora()))
    for p in d["pagamentos"]:
        db.execute("INSERT INTO pagamentos (invoice_id, tipo, valor, data_pagamento, banco, obs, registrado_por,"
                   " registrado_em, historico) VALUES (?, ?, ?, ?, ?, 'Carga de histórico', ?, ?, 1)",
                   (iid, p["tipo"], p["valor"], p["data"], p["banco"], g.usuario["id"], agora()))
    registrar_evento(iid, "Invoice cadastrado pela carga de histórico",
                     f"valor {fmt_moeda(d['valor'], d['moeda'])}"
                     + (f" · desconto {fmt_moeda(d['desconto'], d['moeda'])} ({STATUS_DESCONTO[d['desconto_status']]})"
                        if d["desconto"] else " · sem desconto") + f" · {len(d['pagamentos'])} pagamento(s)")
    # registra a situação atual para as notificações: a carga não gera e-mails
    if config("notif_inicio") is None:
        salvar_config("notif_inicio", agora())
    estado = json.dumps(estado_previsao(carregar_invoice(iid)), sort_keys=True)
    db.execute("INSERT INTO notificacoes_estado (invoice_id, estado, atualizado_em) VALUES (?, ?, ?)"
               " ON CONFLICT(invoice_id) DO UPDATE SET estado = excluded.estado, atualizado_em = excluded.atualizado_em",
               (iid, estado, agora()))
    db.commit()
    return iid


@app.route("/carga-historico/<int:id>/excluir", methods=["POST"])
@somente_admin
def carga_historico_excluir(id):
    """Remove um invoice carregado por engano (só os da carga de histórico, sem deduções ligadas)."""
    db = get_db()
    inv = db.execute("SELECT * FROM invoices WHERE id = ? AND historico = 1", (id,)).fetchone()
    if inv is None:
        abort(404)
    ligado = db.execute("SELECT COUNT(*) FROM deducoes x LEFT JOIN descontos d ON d.id = x.desconto_id"
                        " WHERE x.invoice_id = ? OR d.invoice_id = ?", (id, id)).fetchone()[0]
    if ligado:
        flash("Este invoice tem deduções ligadas a outros invoices: remova as deduções antes de excluí-lo.", "erro")
        return redirect(url_for("carga_historico"))
    excluir_invoice(id)
    flash(f"Invoice {inv['numero']} excluído da carga de histórico.", "ok")
    return redirect(url_for("carga_historico"))
# ---- importação da planilha de histórico (parte da CARGA DE HISTÓRICO, também temporária) ----
from werkzeug.datastructures import MultiDict  # noqa: E402
# Colunas: A Fornecedor | B Invoice | C Moeda | D Data de assinatura | E HAWB | F Total Invoice |
#          G % adiantamento | H % depois de HAWB | I X dias depois de HAWB | J Valor pago (demais colunas ignoradas)
MOEDAS_PLANILHA = {"dolar": "USD", "dólar": "USD", "usd": "USD", "us$": "USD", "euro": "EUR", "eur": "EUR",
                   "€": "EUR", "chf": "CHF", "franco suico": "CHF", "franco suíço": "CHF", "real": "BRL",
                   "brl": "BRL", "r$": "BRL"}


def _data_planilha(v):
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, date):
        return v.isoformat()
    txt = str(v or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(txt, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return txt


def _pct_planilha(v):
    n = v if isinstance(v, (int, float)) else parse_num(v)
    if n is None:
        return 0.0
    return round(n * 100, 4) if abs(n) <= 1 else round(n, 4)  # 0,25 (formato %) ou 25


def linhas_planilha_historico(arquivo):
    """Converte cada linha da planilha num formulário equivalente ao da Carga de histórico."""
    import openpyxl
    wb = openpyxl.load_workbook(arquivo, data_only=True, read_only=True)
    ws = wb.worksheets[0]
    linhas = []
    for n, row in enumerate(ws.iter_rows(values_only=True), 1):
        row = list(row) + [None] * 10
        if n == 1 or not any(row[:6]):
            continue
        if str(row[0] or "").strip().lower() == "fornecedor":
            continue
        forn, numero, moeda, assin, hawb, total, adiant, prazo, dias, pago = row[:10]
        moeda_cod = MOEDAS_PLANILHA.get(str(moeda or "").strip().lower(), str(moeda or "").strip().upper())
        p_adi, p_prz = _pct_planilha(adiant), _pct_planilha(prazo)
        dias_txt = str(int(dias)) if isinstance(dias, (int, float)) else str(dias or "").strip()
        cond_pct, cond_tipo, cond_dias = [], [], []
        if p_adi > 0.0001:
            cond_pct.append(fmt_qtd(p_adi)); cond_tipo.append("ANTECIPADO"); cond_dias.append("")
        if p_prz > 0.0001:
            cond_pct.append(fmt_qtd(p_prz)); cond_tipo.append("PRAZO"); cond_dias.append(dias_txt)
        valor = total if isinstance(total, (int, float)) else parse_num(total)
        pago_v = pago if isinstance(pago, (int, float)) else (parse_num(pago) or 0)
        pago_v = round(pago_v or 0, 2)
        d_assin, d_hawb = _data_planilha(assin), _data_planilha(hawb) if hawb else ""
        form = {"fornecedor": [str(forn or "").strip()], "numero": [str(numero or "").strip()],
                "moeda": [moeda_cod], "data_assinatura": [d_assin], "data_hawb": [d_hawb],
                "valor": [fmt_num(valor) if isinstance(valor, (int, float)) else ""],
                "cond_pct": cond_pct, "cond_tipo": cond_tipo, "cond_dias": cond_dias,
                "desconto": [""], "desconto_status": ["APROVADO"], "segurado": [""],
                "pg_data": [], "pg_tipo": [], "pg_valor": [], "pg_banco": [],
                "obs": ["Importado da planilha de histórico"]}
        data_pg = None
        if pago_v > TOLERANCIA:
            data_pg = data_pagamento_estimada(d_assin, d_hawb, p_adi, valor, pago_v, dias_txt)
            form.update(pg_data=[data_pg], pg_tipo=["PRINCIPAL"], pg_valor=[fmt_num(pago_v)], pg_banco=[""])
            form["obs"] = ["Importado da planilha de histórico; data do pagamento estimada pelo vencimento"]
        linhas.append({"linha": n, "form": form, "pago": pago_v, "data_pg": data_pg})
    wb.close()
    return linhas


def data_pagamento_estimada(d_assin, d_hawb, p_adi, valor, pago, dias):
    """A planilha não traz a data do pagamento: usa o vencimento (antecipado: assinatura + 5 dias úteis;
    após a HAWB: HAWB + dias), limitado a hoje."""
    try:
        if p_adi > 0 and valor and pago <= valor * p_adi / 100 + TOLERANCIA:
            venc = somar_dias_uteis(d_assin, PRAZO_ANTECIPADO_DIAS)
        elif d_hawb and str(dias).isdigit():
            venc = somar_dias(d_hawb, int(dias))
        else:
            venc = d_assin
    except ValueError:
        venc = None
    venc = venc or hoje()
    return min(venc, hoje())


@app.route("/carga-historico/importar", methods=["POST"])
@somente_admin
def carga_historico_importar():
    db = get_db()
    acao = request.form.get("acao", "previa")
    if acao == "cancelar":
        salvar_config("carga_importacao", None)
        db.commit()
        flash("Importação cancelada.", "ok")
        return redirect(url_for("carga_historico"))
    if acao == "confirmar":
        pendente = json.loads(config("carga_importacao") or "null")
        if not pendente:
            flash("Nenhuma importação pendente. Envie a planilha de novo.", "erro")
            return redirect(url_for("carga_historico"))
        criados, falhas = 0, []
        for item in pendente["linhas"]:
            erros, dados = ler_carga_historico(MultiDict([(k, v) for k, vs in item["form"].items() for v in vs]))
            if erros:
                falhas.append(f"Linha {item['linha']}: {erros[0]}")
                continue
            gravar_carga_historico(dados)
            criados += 1
        salvar_config("carga_importacao", None)
        db.commit()
        flash(f"Importação concluída: {criados} invoice(s) cadastrado(s) pela carga de histórico.", "ok")
        for f in falhas[:20]:
            flash(f, "erro")
        return redirect(url_for("carga_historico"))
    # prévia
    arq = request.files.get("planilha")
    if not arq or not arq.filename.lower().endswith((".xlsx", ".xlsm")):
        flash("Escolha a planilha Excel (.xlsx).", "erro")
        return redirect(url_for("carga_historico"))
    try:
        linhas = linhas_planilha_historico(io.BytesIO(arq.read()))
    except Exception as e:
        flash(f"Não foi possível ler a planilha: {e}", "erro")
        return redirect(url_for("carga_historico"))
    previa, validas = [], []
    for item in linhas:
        erros, dados = ler_carga_historico(MultiDict([(k, v) for k, vs in item["form"].items() for v in vs]))
        f = item["form"]
        previa.append({"linha": item["linha"], "fornecedor": f["fornecedor"][0], "numero": f["numero"][0],
                       "moeda": f["moeda"][0], "assinatura": f["data_assinatura"][0], "hawb": f["data_hawb"][0],
                       "valor": dados.get("valor"), "condicao": " + ".join(
                           (f"{p}% antecipado" if t == "ANTECIPADO" else f"{p}% {dd} dias após a HAWB")
                           for p, t, dd in zip(f["cond_pct"], f["cond_tipo"], f["cond_dias"])) or "—",
                       "pago": item["pago"], "data_pg": item["data_pg"], "erros": erros})
        if not erros:
            validas.append(item)
    salvar_config("carga_importacao", json.dumps({"linhas": validas, "arquivo": arq.filename,
                                                  "em": agora()}, ensure_ascii=False))
    db.commit()
    return render_template("carga_historico_previa.html", previa=previa, validas=len(validas),
                           arquivo=arq.filename)


# ======================= fim da CARGA DE HISTÓRICO =========================


# ---------------------------------------------------------------------------
# Documentos na pasta compartilhada da empresa (padrão desde a v4.27)
# Cada documento é o CAMINHO do arquivo na rede (\\servidor\pasta\arquivo.pdf ou Z:\pasta\arquivo.pdf).
# Se o arquivo some (renomeado, movido, apagado), o documento fica "não encontrado" e vira pendência do setor
# que o cadastra. Detecção: (1) qualquer usuário marca "Não encontrado"; (2) verificação automática diária pelo
# próprio servidor, que só roda quando o servidor enxerga a pasta da rede (configuração em Sistema).
# ---------------------------------------------------------------------------
SETOR_DOCUMENTO = {"lancamento": "importacao", "pagamento": "financeiro", "duimp": "importacao", "invoice": "importacao"}
NOME_DOCUMENTO = {"lancamento": "documento do lançamento financeiro", "pagamento": "Swift", "duimp": "DUIMP",
                  "invoice": "invoice assinado"}
RE_CAMINHO_REDE = re.compile(r"^(\\\\[^\\/:*?\"<>|]+\\[^\\/:*?\"<>|]+.*|[A-Za-z]:\\.+)$")


def normalizar_caminho(txt):
    c = (txt or "").strip().strip('"').strip()
    c = c.replace("/", "\\")
    if c.lower().startswith("file:"):
        c = c[5:].lstrip("\\")
        c = "\\\\" + c if not re.match(r"^[A-Za-z]:", c) else c
    return c


def documentos_de(origem, ids):
    ids = list(ids)
    if not ids:
        return {}
    mapa = {}
    for d in get_db().execute(
            "SELECT d.*, u.nome AS autor, p.nome AS quem_problema FROM documentos d"
            " LEFT JOIN usuarios u ON u.id = d.cadastrado_por LEFT JOIN usuarios p ON p.id = d.problema_por"
            f" WHERE d.origem = ? AND d.origem_id IN ({','.join('?' * len(ids))}) ORDER BY d.id", [origem] + ids):
        mapa.setdefault(d["origem_id"], []).append(d)
    return mapa


def pode_cadastrar_documento(origem):
    if origem == "pagamento":
        return pode("financeiro")
    if origem in ("duimp", "invoice"):
        return pode("importacao")
    return pode_anexar()


def invoice_do_documento(origem, origem_id):
    db = get_db()
    if origem in ("duimp", "invoice"):
        return origem_id
    tabela = "pagamentos" if origem == "pagamento" else "lancamentos"
    r = db.execute(f"SELECT invoice_id FROM {tabela} WHERE id = ?", (origem_id,)).fetchone()
    return r["invoice_id"] if r else None


def voltar_seguro(padrao):
    v = request.form.get("voltar") or ""
    return v if v.startswith("/") and not v.startswith("//") else padrao


def metadados_arquivo_form():
    """Nome, tamanho, data de modificação e SHA-256 calculados no navegador ao escolher o arquivo.
    Ainda não são usados para validar nada: servirão para a futura leitura do arquivo (conferir se é o mesmo)."""
    f = request.form
    sha = (f.get("sha256") or "").strip().lower()
    tam = f.get("tamanho", "").strip()
    mod = f.get("modificado_em", "").strip()
    return {"nome_arquivo": (f.get("nome_arquivo") or "").strip()[:255] or None,
            "tamanho": int(tam) if tam.isdigit() else None,
            "arquivo_modificado_em": mod[:19] if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?.*", mod) else None,
            "sha256": sha if re.fullmatch(r"[0-9a-f]{64}", sha) else None}


def pasta_padrao(origem):
    return config(f"pasta_padrao_{origem}", "") or ""


app.jinja_env.globals["pasta_padrao"] = lambda origem: pasta_padrao(origem)
app.jinja_env.globals["arquivos_invoice"] = lambda iid: arquivos_de("invoice", [iid]).get(iid, []) if iid else []
app.jinja_env.globals["documentos_invoice"] = lambda iid: documentos_de("invoice", [iid]).get(iid, []) if iid else []


@app.route("/documentos/novo", methods=["POST"])
@login_obrigatorio
def documento_novo():
    origem, origem_id = request.form.get("origem"), request.form.get("origem_id", type=int)
    if origem not in SETOR_DOCUMENTO or not origem_id:
        abort(400)
    if not pode_cadastrar_documento(origem):
        abort(403)
    iid = invoice_do_documento(origem, origem_id)
    if not iid:
        abort(404)
    volta = voltar_seguro(url_for("invoice_detalhe", id=iid))
    caminho = normalizar_caminho(request.form.get("caminho"))
    if not RE_CAMINHO_REDE.match(caminho):
        flash("Informe o caminho completo do arquivo na pasta da rede, por exemplo "
              "\\\\servidor\\Importacao\\2026\\Swift 123.pdf ou Z:\\Importacao\\2026\\Swift 123.pdf.", "erro")
        return redirect(volta)
    db = get_db()
    meta = metadados_arquivo_form()
    db.execute("INSERT INTO documentos (origem, origem_id, caminho, status, cadastrado_por, cadastrado_em, nome_arquivo,"
               " tamanho, arquivo_modificado_em, sha256) VALUES (?, ?, ?, 'NAO_VERIFICADO', ?, ?, ?, ?, ?, ?)",
               (origem, origem_id, caminho, g.usuario["id"], agora(), meta["nome_arquivo"], meta["tamanho"],
                meta["arquivo_modificado_em"], meta["sha256"]))
    registrar_evento(iid, f"Caminho cadastrado ({NOME_DOCUMENTO[origem]})", caminho)
    db.commit()
    if origem == "invoice" and registrar_conclusao_cadastro(iid):
        flash("Cadastro do invoice concluído: o vencimento da parcela antecipada conta a partir de hoje.", "ok")
    flash("Caminho do documento cadastrado.", "ok")
    return redirect(volta)


@app.route("/documentos/<int:did>/<acao>", methods=["POST"])
@login_obrigatorio
def documento_acao(did, acao):
    db = get_db()
    d = db.execute("SELECT * FROM documentos WHERE id = ?", (did,)).fetchone()
    if d is None:
        abort(404)
    iid = invoice_do_documento(d["origem"], d["origem_id"])
    volta = voltar_seguro(url_for("invoice_detalhe", id=iid) if iid else url_for("painel"))
    nome = NOME_DOCUMENTO.get(d["origem"], "documento")
    if acao == "nao-encontrado":  # qualquer usuário pode avisar que o arquivo sumiu
        obs = " ".join(request.form.get("obs", "").split())[:300] or None
        db.execute("UPDATE documentos SET status = 'NAO_ENCONTRADO', problema_em = ?, problema_por = ?, problema_obs = ?"
                   " WHERE id = ?", (agora(), g.usuario["id"], obs, did))
        if iid:
            registrar_evento(iid, f"Caminho não encontrado ({nome})", d["caminho"] + (f" · {obs}" if obs else ""))
        db.commit()
        flash(f"Registrado: o arquivo não foi encontrado. A pendência foi para {SETORES[SETOR_DOCUMENTO[d['origem']]]}.",
              "ok")
        return redirect(volta)
    if not pode_cadastrar_documento(d["origem"]):
        abort(403)
    if acao == "editar":
        caminho = normalizar_caminho(request.form.get("caminho"))
        if not RE_CAMINHO_REDE.match(caminho):
            flash("Caminho inválido: use \\\\servidor\\pasta\\arquivo ou Z:\\pasta\\arquivo.", "erro")
            return redirect(volta)
        meta = metadados_arquivo_form()
        if meta["nome_arquivo"] or meta["sha256"]:  # outro arquivo escolhido: atualiza a impressão digital
            db.execute("UPDATE documentos SET nome_arquivo = ?, tamanho = ?, arquivo_modificado_em = ?, sha256 = ?"
                       " WHERE id = ?", (meta["nome_arquivo"], meta["tamanho"], meta["arquivo_modificado_em"],
                                         meta["sha256"], did))
        db.execute("UPDATE documentos SET caminho = ?, status = 'NAO_VERIFICADO', problema_em = NULL, problema_por = NULL,"
                   " problema_obs = NULL, verificado_em = NULL WHERE id = ?", (caminho, did))
        if iid:
            registrar_evento(iid, f"Caminho corrigido ({nome})", f"{d['caminho']} → {caminho}")
        db.commit()
        flash("Caminho corrigido.", "ok")
    elif acao == "excluir":
        db.execute("DELETE FROM documentos WHERE id = ?", (did,))
        if iid:
            registrar_evento(iid, f"Caminho excluído ({nome})", d["caminho"])
        db.commit()
        flash("Caminho excluído.", "ok")
    else:
        abort(404)
    return redirect(volta)


# ---------- verificação automática (só quando o servidor enxerga a pasta da rede) ----------
def mapeamentos_rede():
    """Linhas 'prefixo no Windows = caminho no servidor' (Sistema). Ex.: Z:\\ = /mnt/rede/  ou  \\\\srv01\\dados = /mnt/dados"""
    pares = []
    for linha in (config("rede_mapeamentos") or "").splitlines():
        if "=" in linha:
            a, b = linha.split("=", 1)
            if a.strip() and b.strip():
                pares.append((a.strip(), b.strip()))
    return pares


def caminho_no_servidor(caminho):
    for prefixo, destino in sorted(mapeamentos_rede(), key=lambda p: -len(p[0])):
        if caminho.lower().startswith(prefixo.lower()):
            resto = caminho[len(prefixo):].lstrip("\\")
            if os.sep == "/":
                resto = resto.replace("\\", "/")
            return os.path.join(destino, resto), destino
    if os.name == "nt":  # servidor Windows dentro da rede: usa o caminho direto
        raiz = "\\\\" + "\\".join(caminho.lstrip("\\").split("\\")[:2]) if caminho.startswith("\\\\") else caminho[:3]
        return caminho, raiz
    return None, None


def verificar_documentos():
    """Confere cada caminho cadastrado. Documentos cuja pasta-raiz o servidor não alcança não são alterados."""
    db = get_db()
    res = {"ok": 0, "nao_encontrado": 0, "sem_acesso": 0}
    acessivel = {}
    for d in db.execute("SELECT * FROM documentos").fetchall():
        local, raiz = caminho_no_servidor(d["caminho"])
        if not local:
            res["sem_acesso"] += 1
            continue
        if raiz not in acessivel:
            acessivel[raiz] = os.path.isdir(raiz)
        if not acessivel[raiz]:
            res["sem_acesso"] += 1
            continue
        if os.path.exists(local):
            db.execute("UPDATE documentos SET status = 'OK', verificado_em = ?, problema_em = NULL, problema_por = NULL,"
                       " problema_obs = NULL WHERE id = ?", (agora(), d["id"]))
            res["ok"] += 1
        else:
            if d["status"] != "NAO_ENCONTRADO":
                db.execute("UPDATE documentos SET status = 'NAO_ENCONTRADO', verificado_em = ?, problema_em = ?,"
                           " problema_por = NULL, problema_obs = 'Verificação automática' WHERE id = ?",
                           (agora(), agora(), d["id"]))
                iid = invoice_do_documento(d["origem"], d["origem_id"])
                if iid:
                    registrar_evento(iid, f"Caminho não encontrado na verificação automática "
                                          f"({NOME_DOCUMENTO.get(d['origem'], 'documento')})", d["caminho"])
            else:
                db.execute("UPDATE documentos SET verificado_em = ? WHERE id = ?", (agora(), d["id"]))
            res["nao_encontrado"] += 1
    salvar_config("rede_ultima_verificacao", json.dumps({"em": agora(), **res}))
    db.commit()
    return res


def texto_verificacao(res):
    if res["ok"] + res["nao_encontrado"] == 0:
        return (f"Verificação de caminhos: o servidor não tem acesso à pasta da rede ({res['sem_acesso']} documento(s) "
                "não verificado(s)).")
    return (f"Verificação de caminhos: {res['ok']} encontrado(s), {res['nao_encontrado']} não encontrado(s)"
            + (f", {res['sem_acesso']} sem acesso." if res["sem_acesso"] else "."))


@app.route("/sistema/rede", methods=["POST"])
@somente_admin
def sistema_rede():
    if request.form.get("acao") == "verificar":
        flash(texto_verificacao(verificar_documentos()), "ok")
    elif request.form.get("acao") == "pastas":
        erros = []
        for origem in SETOR_DOCUMENTO:
            v = normalizar_caminho(request.form.get(f"pasta_{origem}", ""))
            if v and not RE_CAMINHO_REDE.match(v.rstrip("\\") + "\\x"):
                erros.append(f"Pasta padrão inválida para {NOME_DOCUMENTO[origem]}: {v}")
                continue
            salvar_config(f"pasta_padrao_{origem}", (v.rstrip("\\") + "\\") if v else None)
        get_db().commit()
        for e in erros:
            flash(e, "erro")
        if not erros:
            flash("Pastas padrão salvas.", "ok")
    else:
        salvar_config("rede_mapeamentos", request.form.get("mapeamentos", "").strip() or None)
        get_db().commit()
        flash("Mapeamento da pasta da rede salvo.", "ok")
    return redirect(url_for("sistema") + "#rede")


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
