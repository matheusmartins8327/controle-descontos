"""
Atualização automática do Controle de Descontos.

Executado pelo "Iniciar Controle de Descontos.bat" antes de abrir o sistema:
  1. Consulta a versão mais recente publicada no GitHub.
  2. Se houver versão nova, faz backup do banco de dados e substitui os arquivos do sistema.
  3. Os dados (pasta instance) e o ambiente Python (.venv) nunca são alterados.

Sem internet ou com qualquer erro, o sistema abre normalmente com a versão atual.
"""
import io
import json
import os
import shutil
import sys
import urllib.request
import zipfile
from datetime import datetime

REPOSITORIO = "matheusmartins8327/controle-descontos"
RAMO = "main"

BASE = os.path.dirname(os.path.abspath(__file__))
INSTANCE = os.path.join(BASE, "instance")
ARQ_VERSAO = os.path.join(INSTANCE, "versao_instalada.txt")
ARQ_TOKEN = os.path.join(INSTANCE, "github_token.txt")  # opcional, só para repositório privado
PRESERVAR = {"instance", ".venv", "Iniciar Controle de Descontos.bat"}
MAX_BACKUPS = 20


def requisicao(url):
    req = urllib.request.Request(url, headers={"User-Agent": "controle-descontos",
                                               "Accept": "application/vnd.github+json"})
    if os.path.exists(ARQ_TOKEN):
        with open(ARQ_TOKEN) as f:
            token = f.read().strip()
        if token:
            req.add_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(req, timeout=20)


def versao_instalada():
    try:
        with open(ARQ_VERSAO) as f:
            return f.read().strip()
    except OSError:
        return ""


def backup_banco():
    banco = os.path.join(INSTANCE, "descontos.db")
    if not os.path.exists(banco):
        return
    pasta = os.path.join(INSTANCE, "backups")
    os.makedirs(pasta, exist_ok=True)
    destino = os.path.join(pasta, f"descontos-{datetime.now():%Y%m%d-%H%M%S}.db")
    shutil.copy2(banco, destino)
    antigos = sorted(f for f in os.listdir(pasta) if f.endswith(".db"))
    for f in antigos[:-MAX_BACKUPS]:
        os.remove(os.path.join(pasta, f))


def aplicar(conteudo_zip):
    with zipfile.ZipFile(io.BytesIO(conteudo_zip)) as z:
        raiz = z.namelist()[0].split("/")[0] + "/"
        for item in z.infolist():
            relativo = item.filename[len(raiz):]
            if not relativo or relativo.split("/")[0] in PRESERVAR:
                continue
            destino = os.path.join(BASE, *relativo.split("/"))
            if item.is_dir():
                os.makedirs(destino, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(destino), exist_ok=True)
            temporario = destino + ".novo"
            with z.open(item) as origem, open(temporario, "wb") as saida:
                shutil.copyfileobj(origem, saida)
            os.replace(temporario, destino)


def main():
    if not REPOSITORIO:
        return
    print(" Verificando atualizações...")
    try:
        with requisicao(f"https://api.github.com/repos/{REPOSITORIO}/commits/{RAMO}") as r:
            ultimo = json.load(r)
        sha = ultimo["sha"]
        if sha == versao_instalada():
            print(" Sistema já está na versão mais recente.")
            return
        resumo = ultimo["commit"]["message"].splitlines()[0]
        with requisicao(f"https://api.github.com/repos/{REPOSITORIO}/zipball/{sha}") as r:
            conteudo = r.read()
        os.makedirs(INSTANCE, exist_ok=True)
        backup_banco()
        aplicar(conteudo)
        with open(ARQ_VERSAO, "w") as f:
            f.write(sha)
        print(f" Sistema atualizado: {resumo}")
    except Exception as e:  # sem internet, GitHub fora do ar etc.: segue com a versão atual
        print(f" Não foi possível verificar atualizações ({e.__class__.__name__}). Abrindo a versão atual.")


if __name__ == "__main__":
    main()
    sys.exit(0)
