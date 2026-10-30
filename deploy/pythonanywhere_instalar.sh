#!/bin/bash
# Instalação do Controle de Descontos no PythonAnywhere.
# Uso (no console Bash do PythonAnywhere):
#   bash <(curl -s https://raw.githubusercontent.com/matheusmartins8327/controle-descontos/main/deploy/pythonanywhere_instalar.sh)
set -e
REPO="${REPO:-https://github.com/matheusmartins8327/controle-descontos.git}"
DIR="$HOME/controle-descontos"
PY="${PY:-python3.11}"
# Localiza o arquivo WSGI do Web app (ex.: /var/www/usuario_pythonanywhere_com_wsgi.py)
localizar_wsgi() {
  local arquivos=(/var/www/*_wsgi.py)
  [ -e "${arquivos[0]}" ] || return 1
  for f in "${arquivos[@]}"; do
    # prefere o do próprio usuário (sem diferenciar maiúsculas/minúsculas)
    if [[ "${f,,}" == "/var/www/${USER,,}_"* ]]; then echo "$f"; return 0; fi
  done
  echo "${arquivos[0]}"
}

echo "== Controle de Descontos: instalação =="
WSGI="${WSGI:-$(localizar_wsgi || true)}"
if [ -z "$WSGI" ] || [ ! -f "$WSGI" ]; then
  echo
  echo "ERRO: não encontrei o Web app desta conta."
  echo "  Usuário: $USER"
  echo "  Conteúdo de /var/www: $(ls /var/www 2>/dev/null | tr '\n' ' ')"
  echo
  echo "Confira na aba 'Web' se aparece o endereço do sistema (ex.: ${USER,,}.pythonanywhere.com)."
  echo "Se não aparecer: 'Add a new web app' > Next > 'Manual configuration' > 'Python 3.11' > Next."
  echo "Depois rode este comando de novo. Se o erro continuar, envie uma foto desta tela."
  exit 1
fi
ENDERECO="$(basename "$WSGI" _wsgi.py | tr '_' '.')"
echo "Web app encontrado: $ENDERECO"

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
echo "Endereço do sistema: https://$ENDERECO"
if [ -n "$CODIGO" ]; then
  echo "Código de instalação (para criar o administrador): $CODIGO"
fi
echo
echo "Último passo: aba 'Tasks' > agende diariamente o comando:"
echo "  bash $DIR/deploy/tarefa_diaria.sh"
