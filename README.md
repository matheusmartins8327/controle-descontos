# Controle de Descontos – Fornecedores do Paquistão (v1.4)

Sistema web para controlar descontos por má qualidade, desde a inspeção do invoice até o pagamento do valor retido.
Todos os valores são em **dólar (US$)**.

## Hospedagem na nuvem: PythonAnywhere (recomendado)

O sistema fica no ar 24h em `https://matheusmartins8327.pythonanywhere.com`, sem depender de nenhum computador ligado.
Os usuários acessam pelo navegador, de qualquer lugar (o link pode ser salvo no OneDrive, Teams ou favoritos).

### Instalação (uma única vez, ~10 minutos)
1. Crie uma conta gratuita em https://www.pythonanywhere.com/registration/register/beginner/ com o usuário **matheusmartins8327**.
2. Aba **Web** → **Add a new web app** → **Next** → **Manual configuration** → **Python 3.11** → **Next**.
3. Aba **Consoles** → **Bash**. Cole o comando abaixo e tecle Enter:
   ```
   bash <(curl -s https://raw.githubusercontent.com/matheusmartins8327/controle-descontos/main/deploy/pythonanywhere_instalar.sh)
   ```
   No final aparece o **código de instalação** (6 dígitos). Anote.
4. Aba **Tasks** → em *Scheduled tasks*, escolha um horário de madrugada (ex.: 03:00 UTC) e cadastre o comando:
   ```
   bash /home/matheusmartins8327/controle-descontos/deploy/tarefa_diaria.sh
   ```
5. Abra `https://matheusmartins8327.pythonanywhere.com`, informe o código de instalação e crie sua conta de administrador.

### Atualizações
- **Automáticas:** a tarefa diária faz backup do banco e instala a versão mais recente publicada no GitHub.
- **Na hora:** menu **Sistema** → **Verificar e instalar atualização** (somente administrador).

### Cuidados
- No plano gratuito, o PythonAnywhere pede que você clique em **"Run until 3 months from today"** (aba **Web**) a cada 3 meses; eles avisam por e-mail antes. O plano pago (~US$ 5/mês) remove essa exigência.
- Backups ficam em `controle-descontos/instance/backups` (aba **Files** do PythonAnywhere, de onde podem ser baixados).
- Para levar dados de um computador para o servidor: aba **Files** → `controle-descontos/instance/` → **Upload** do arquivo `descontos.db` e depois **Reload** na aba **Web**.

## Uso em um computador local (alternativa)

## Como abrir no Windows (passo a passo)

**1. Instale o Python (só na primeira vez)**
- Acesse https://www.python.org/downloads/ e clique em **Download Python**.
- Abra o instalador e, **antes de clicar em "Install Now", marque a caixa "Add python.exe to PATH"** (na parte de baixo da primeira tela).
- Conclua a instalação.

**2. Extraia a pasta do sistema**
- Clique com o botão direito no arquivo `controle-descontos.zip` → **Extrair tudo…**
- Escolha um local fixo, por exemplo `C:\ControleDescontos`. Não rode o sistema de dentro do .zip nem da pasta Downloads, porque o banco de dados fica dentro desta pasta.

**3. Abra o sistema**
- Dentro da pasta extraída, dê **dois cliques** em **`Iniciar Controle de Descontos.bat`**.
- Na primeira vez ele prepara o sistema (precisa de internet, leva 1–2 minutos).
- O navegador abre sozinho em **http://localhost:5000**. Na primeira vez, crie a sua conta de administrador.
- Se o Windows perguntar sobre o **Firewall**, clique em **Permitir** (redes privadas) para que os colegas consigam acessar.
- **Mantenha a janela preta aberta** enquanto o sistema estiver em uso. Fechar a janela desliga o sistema.

**4. Acesso dos outros usuários**
- A janela preta mostra o endereço para a rede, por exemplo `Outros na rede: http://192.168.0.15:5000`.
- Os colegas abrem esse endereço no navegador deles (estando na mesma rede da empresa). Não precisam instalar nada.
- O computador onde o sistema roda precisa estar ligado e com a janela aberta.

### Pelo cmd (alternativa)
```
cd C:\ControleDescontos
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python app.py
```

### Problemas comuns
| Sintoma | Solução |
|---|---|
| `'python' não é reconhecido…` ou abre a Microsoft Store | Python não instalado ou sem "Add to PATH". Reinstale marcando a opção. |
| `No such file or directory: app.py` | O cmd não está na pasta do sistema. Use `cd` para a pasta onde extraiu, ou use o arquivo `.bat`. |
| Colegas não conseguem acessar | Verifique se a janela está aberta, se estão na mesma rede e se o Firewall permitiu o Python. |
| Porta 5000 ocupada | Rode `set PORT=5050` antes de `python app.py` e acesse `:5050`. |

## Atualizações automáticas

Toda vez que o sistema é aberto pelo `Iniciar Controle de Descontos.bat`, ele verifica no GitHub se há uma versão nova e se atualiza sozinho:
- Antes de atualizar, é feito um **backup automático do banco** em `instance\backups` (mantém os 20 mais recentes).
- A pasta `instance` (seus dados) nunca é alterada pela atualização.
- Sem internet, o sistema abre normalmente com a versão atual.
- Se o sistema estiver aberto, **feche a janela preta e abra novamente** para receber a atualização.

## Fluxo

| # | Etapa | Responsável |
|---|-------|-------------|
| 1 | Cadastrar o desconto após a inspeção: fornecedor, invoice inspecionado, data de envio do relatório, valor total do invoice, valor do desconto (fechado) e observações | Qualidade |
| 2 | Confirmar que o valor foi **retido** no pagamento do invoice de origem | Financeiro |
| 3 | Registrar o(s) **novo(s) invoice(s)** com a dedução. **O desconto pode ser dividido em vários invoices** (ex.: US$ 10.000 = US$ 4.000 no invoice A + US$ 6.000 no invoice B) | Compras |
| 4 | Confirmar a **chegada** de cada invoice deduzido na fábrica | Recebimento |
| 5 | **Pagar** a parte correspondente do valor retido (cada dedução libera o seu valor) | Financeiro |

- O sistema não permite deduzir mais que o saldo nem repetir o mesmo invoice no mesmo desconto.
- Cada dedução segue seu próprio caminho (chegada → pagamento). O desconto só é **Concluído** quando todo o valor foi deduzido e pago.
- Cada etapa só pode ser executada pelo perfil responsável (ou pelo Administrador).
- Tudo fica registrado no histórico (quem, quando, o quê).
- Os valores do desconto só podem ser editados antes da retenção.
- O Administrador pode desfazer etapas, excluir deduções e cancelar descontos (sempre com motivo).

## Usuários e perfis

- Limite de **5 usuários ativos** (incluindo o administrador). Configurável em `MAX_USUARIOS` no `app.py`.
- Perfis: Administrador, Qualidade, Financeiro, Compras, Recebimento.
- Somente o Administrador cria, edita, ativa/desativa usuários e redefine senhas.

## Backup

Os dados ficam em `instance\descontos.db`. **Copie a pasta `instance` periodicamente** para um local seguro (rede, OneDrive etc.).

## Próximas versões (planejado)

- **Relatórios**: retido em aberto por fornecedor, descontos por período, pendências por etapa, exportação Excel/PDF.
- **Indicadores**: total retido em aberto, tempo médio entre etapas, % de desconto por fornecedor, ranking de qualidade.
- Outras ideias: anexos (relatório de inspeção, invoices), notificações por e-mail, hospedagem em servidor.
