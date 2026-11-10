# Controle de Descontos – Fornecedores do Paquistão (v2.0)

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
   bash ~/controle-descontos/deploy/tarefa_diaria.sh
   ```
5. Abra `https://matheusmartins8327.pythonanywhere.com`, informe o código de instalação e crie sua conta de administrador.

### Atualizações
- **Automáticas:** a tarefa diária faz backup do banco, reenvia e-mails que falharam e instala a versão mais recente publicada no GitHub.
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
| 1 | Cadastrar o desconto após a inspeção (fornecedor, invoice inspecionado, data de envio do relatório, valor total do invoice, valor do desconto e observações). **Ao cadastrar, o valor já fica segurado** | Administrador |
| 2 | Registrar o(s) **novo(s) invoice(s)** com a dedução: nº do invoice e valor deduzido (obrigatórios) e observação (opcional). O desconto pode ser dividido em vários invoices | Importação |
| 3 | Confirmar a **chegada** de cada invoice deduzido na fábrica | Importação |
| 4 | **Ciência do Financeiro** pelo link do e-mail (o Financeiro não usa o sistema) | Financeiro, por e-mail |

O desconto fica **Concluído** quando todo o valor foi deduzido, todos os invoices chegaram e o Financeiro deu ciência de todos os e-mails.

### E-mails ao Financeiro
Um e-mail informativo é enviado a cada evento que exige ação do Financeiro:

| Evento | O que o e-mail pede |
|---|---|
| Cadastro do desconto | Segurar o valor no pagamento do invoice de origem |
| Revalidação para cima | Segurar o valor adicional |
| Chegada do invoice deduzido | Liberar a parte correspondente do valor segurado |
| Revalidação para baixo / desconto zerado | Liberar a diferença ao fornecedor |
| Cancelamento | Liberar o valor segurado |
| Correção (ex.: chegada desfeita, cadastro corrigido) | Desconsiderar o e-mail anterior (não pede ciência) |

- O e-mail **não tem link para o sistema**: só o botão **Confirmar ciência**, que abre uma página sem login com o botão **"Li e estou de acordo"** (um por destinatário principal). São 2 cliques de propósito: a proteção de links do Outlook abre os links automaticamente, e um clique só seria "confirmado" pelo robô.
- **Destinatários** (menu **Notificações**): **Principal** recebe em "Para" e confirma; **Cópia** recebe em "Cc". Basta a confirmação de um principal.
- **Registro**: cada e-mail fica registrado no desconto e no menu **Notificações** (enviado, falhou, ciência, quem confirmou e quando), com **Ver e-mail** e **Reenviar**.
- **Falhas**: o sistema tenta de novo na hora, avisa no painel e a tarefa diária reenvia automaticamente.

### Configurar o envio (Gmail)
1. Crie uma conta Gmail dedicada ao sistema (ex.: `controle.descontos.empresa@gmail.com`).
2. Na conta Google: **Segurança → Verificação em duas etapas** (ativar) → **Senhas de app** → gere uma senha (16 letras).
3. No sistema, menu **Notificações**: informe o e-mail remetente e a senha de app, salve, use **Enviar teste** e, funcionando, mude o modo para **Ativo**.
4. Cadastre os destinatários e peça ao Financeiro para marcar o remetente como **confiável** (para não cair no lixo eletrônico).

Enquanto o modo estiver em **Teste**, os e-mails são apenas registrados (dá para ver cada um em **Ver e-mail**). Ao ativar, use **Enviar agora os e-mails pendentes** para mandar os gerados durante o teste.
A senha de app fica somente no servidor (`instance/gmail_senha_app.txt`), nunca no GitHub.

### Revalidação do valor
- Depois do cadastro, o Administrador pode **revalidar** o valor do desconto (com motivo obrigatório). O sistema guarda o **valor inicial segurado** e todas as revalidações; as etapas seguintes usam apenas o **valor final**.
- O valor final não pode ser menor que o total já deduzido em invoices. Pode ser **zerado** (US$ 0).
- Pode ficar **acima** do segurado somente enquanto o Financeiro ainda não foi avisado para liberar nenhuma parte; o Administrador confirma que o invoice de origem ainda não foi pago.

### Regras gerais
- Somente o Administrador cadastra, edita (fornecedor, invoice e valor só enquanto não houver revalidação, dedução ou liberação), revalida, cancela e desfaz etapas (sempre com motivo).
- O perfil **Consulta** apenas visualiza.
- Tudo fica registrado no histórico (quem, quando, o quê).

## Usuários e perfis

- Limite de **5 usuários ativos** (incluindo o administrador). Configurável em `MAX_USUARIOS` no `app.py`.
- Perfis: Administrador, Importação e Consulta (somente leitura). O Financeiro não precisa de usuário: é avisado por e-mail.
- Usuários de perfis antigos são convertidos automaticamente: Compras e Recebimento → Importação; Qualidade e Financeiro → Consulta.
- Somente o Administrador cria, edita, ativa/desativa usuários e redefine senhas.

## Backup

Os dados ficam em `instance\descontos.db`. **Copie a pasta `instance` periodicamente** para um local seguro (rede, OneDrive etc.).

## Próximas versões (planejado)

- **Relatórios**: retido em aberto por fornecedor, descontos por período, pendências por etapa, exportação Excel/PDF.
- **Indicadores**: total retido em aberto, tempo médio entre etapas, % de desconto por fornecedor, ranking de qualidade.
- Outras ideias: anexos (relatório de inspeção, invoices), notificações por e-mail, hospedagem em servidor.
