#!/bin/bash
# Instalação do Controle de Descontos no PythonAnywhere.
# Uso (no console Bash do PythonAnywhere):
#   bash <(curl -s https://raw.githubusercontent.com/matheusmartins8327/controle-descontos/main/deploy/pythonanywhere_instalar.sh)
set -e
REPO="${REPO:-https://github.com/matheusmartins8327/controle-descontos.git}"
DIR="$HOME/controle-descontos"
PY="${PY:-python3.11}"
WSGI="/var/www/${USER//./_}_pythonanywhere_com_wsgi.py"

echo "== Controle de Descontos: instalação =="
if [ ! -f "$WSGI" ]; then
  echo
  echo "ERRO: o Web app ainda não foi criado."
  echo "Vá na aba 'Web' > 'Add a new web app' > 'Manual configuration' > 'Python 3.11' e rode este comando de novo."
  exit 1
fi

if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch -q origin main
  git -C "$DIR" reset -q --hard origin/main
else
  git clone -q "$REPO" "$DIR"
fi
$PY -m pip install --user -q --disable-pip-version-check --no-warn-script-location -r "$DIR/requirements.txt"

cat > "$WSGI" <<WSGI_EOF
# Gerado pelo instalador do Controle de Descontos
import os
import sys

os.environ["HTTPS"] = "1"
caminho = "$DIR"
if caminho not in sys.path:
    sys.path.insert(0, caminho)

from app import app as application  # noqa
WSGI_EOF

CODIGO=$(cd "$DIR" && $PY -c "
import app
with app.app.app_context():
    print(app.codigo_instalacao() if app.sem_usuarios() else '')
")
touch "$WSGI"

echo
echo "== Instalação concluída =="
echo "Endereço do sistema: https://${USER}.pythonanywhere.com"
if [ -n "$CODIGO" ]; then
  echo "Código de instalação (para criar o administrador): $CODIGO"
fi
echo
echo "Último passo: aba 'Tasks' > agende diariamente o comando:"
echo "  bash $DIR/deploy/tarefa_diaria.sh"
