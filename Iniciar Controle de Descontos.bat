@echo off
chcp 65001 >nul
title Controle de Descontos - Fornecedores Paquistao
cd /d "%~dp0"

echo.
echo  Controle de Descontos - Fornecedores do Paquistao
echo  -------------------------------------------------
echo.

rem ---- Localiza o Python instalado ----
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY goto semPython

rem ---- Primeira execucao: cria o ambiente e instala dependencias ----
if not exist ".venv\Scripts\python.exe" (
  echo  Preparando o sistema pela primeira vez. Isso pode levar 1 ou 2 minutos...
  %PY% -m venv .venv
  if errorlevel 1 goto erro
)
rem ---- Baixa a versao mais recente do sistema (se houver) ----
".venv\Scripts\python.exe" atualizar.py

".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto erro

rem ---- Abre o navegador alguns segundos depois e inicia o servidor ----
start "" cmd /c "timeout /t 3 >nul & start http://localhost:5000"
".venv\Scripts\python.exe" app.py
echo.
echo  O sistema foi encerrado.
pause
exit /b 0

:semPython
echo  [ERRO] Python nao foi encontrado neste computador.
echo.
echo  1. Baixe e instale o Python em https://www.python.org/downloads/
echo  2. Na primeira tela do instalador, MARQUE a opcao "Add python.exe to PATH".
echo  3. Depois de instalar, de dois cliques neste arquivo novamente.
echo.
start "" https://www.python.org/downloads/
pause
exit /b 1

:erro
echo.
echo  [ERRO] Nao foi possivel preparar o sistema.
echo  Verifique a conexao com a internet (necessaria apenas na primeira vez)
echo  e tente novamente. Se persistir, apague a pasta ".venv" e tente de novo.
pause
exit /b 1
