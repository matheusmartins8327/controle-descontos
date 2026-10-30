#!/bin/bash
# Tarefa diária no PythonAnywhere: backup do banco + instalação de atualizações publicadas no GitHub.
DIR="$HOME/controle-descontos"
PY="${PY:-python3.11}"
WSGI="/var/www/${USER//./_}_pythonanywhere_com_wsgi.py"
set -e
cd "$DIR"

$PY -c "import app; print('Backup:', app.backup_banco('-diario'))"

git fetch -q origin main
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
  $PY -c "import app; app.backup_banco('-antes-atualizacao')"
  git reset -q --hard origin/main  # o servidor fica idêntico ao GitHub
  $PY -m pip install --user -q --disable-pip-version-check --no-warn-script-location -r requirements.txt
  touch "$WSGI"
  echo "Atualizado para: $(git log -1 --format='%h %s')"
else
  echo "Sem atualizações."
fi
