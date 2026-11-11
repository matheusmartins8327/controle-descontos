# Controle de Invoices e Descontos (v3.8)

Sistema único que controla o ciclo completo de cada invoice de fornecedor, da criação até o pagamento completo,
reunindo **Importação**, **Engenharia de Processos** (inspeção e descontos) e **Financeiro** (pagamentos).
Valores em **dólar, euro ou real**. Visual na identidade da **Quinelato** (azul #327FEF, fonte Plus Jakarta Sans e logo, conforme quinelato.com.br). Documentos ficam no **OneDrive** (o sistema guarda os links).

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

## Fluxo do invoice

| # | Etapa | Quem informa | Informações |
|---|-------|--------------|-------------|
| 1 | Criação do invoice | Importação | Fornecedor, nº, moeda, **data de assinatura**, **condição de pagamento** (uma ou mais parcelas), agente de carga, despachante, grupo de consolidação, coleta programada e itens (código, descrição, quantidade e preço original), digitados ou **carregados de uma planilha Excel**. Aparece a **janela de descontos aprovados** do fornecedor (mesma moeda) para deduzir um ou mais, total ou parcialmente; o **valor final** é calculado após as deduções |
| 2 | Coleta | Importação | Data da HAWB e data real de coleta |
| 3 | Embarque | Importação / Financeiro | ETD e ETA planejados (obrigatórios para salvar), ETD e ETA reais; **banco de débito** (Financeiro, obrigatório para concluir a etapa) |
| 4 | Desembaraço | Importação | Data de registro no Brasil → **DUIMP e boleto do ICMS anexados** (obrigatórios para seguir) → data de parametrização, canal, exigências da ANVISA e data de desembaraço |
| 5 | Transporte | Importação | Data programada de chegada (obrigatória) e data real de chegada na fábrica |
| 6 | Inspeção | Engenharia (administrador) | Fim da inspeção e se há desconto; se sim: valor, data do relatório e aprovação do fornecedor (Enviado → Aprovado) |
| 7 | Pagamento | Financeiro | Pagamento de cada parcela (integral, ou parcial quando há valor segurado), com Swift |
| 8 | Dedução e liberação | Importação / Financeiro | O desconto é deduzido em próximo(s) invoice(s); quando eles chegam, o Financeiro libera o valor segurado, com Swift |

**Salvar sempre funciona**: no cadastro basta fornecedor e nº do invoice; nas etapas logísticas, qualquer informação pode ser salva a qualquer momento, **desde que o cadastro esteja completo** (a etapa 2, Coleta, só começa com todas as informações obrigatórias do cadastro). A etapa só é concluída (e o processo só avança) quando todas as informações obrigatórias (*) estão preenchidas. O painel mostra em **Minhas pendências** o que falta em cada etapa e também as informações não obrigatórias ainda vazias (exceto Exigências da ANVISA e Grupo de consolidação), até serem preenchidas.

O invoice fica **Concluído** quando está pago, o desconto (se houver) foi deduzido e liberado, e todos os pagamentos têm Swift.

### Itens e carga por planilha
- **Valor total do invoice** = soma de quantidade × preço original de cada item. **Valor final** = valor total − deduções de descontos aplicadas no invoice.
- Com desconto deduzido no invoice, aparecem duas colunas calculadas (somente exibição): **Preço com desconto** = preço original × (total com desconto ÷ total original) e **Total com desconto** por item, cuja soma é exatamente o total original − descontos aplicados.
- Para invoices extensos, use **Carregar itens de uma planilha Excel** na criação (ou correção) do invoice: colunas **Código, Descrição, Quantidade, Preço Original** (o botão **Baixar modelo** traz a planilha pronta). Aceita .xlsx e .csv, até 2.000 itens; linhas inválidas são apontadas pelo número.
- Código, quantidade e preço são obrigatórios em cada item.
- **Exportar itens para Excel**: na tela do invoice (itens salvos) ou no cadastro/correção (o que está na tela, antes de salvar), com Código, Descrição, Quantidade, Preço Original, Total e, havendo desconto, Preço com Desconto, Total com Desconto e os totais.

### Regras de pagamento
- **Condição de pagamento em parcelas** que somam 100% (ex.: 25% antecipado + 75% 60 dias após a HAWB).
- **Vencimento de cada parcela**: antecipada = data de assinatura + 5 dias; a prazo = data da HAWB (registrada na Coleta) + dias. **Antes da HAWB, a parcela a prazo fica sem vencimento** (não há estimativa).
- Pagamentos são distribuídos nas parcelas por ordem de vencimento; o valor segurado sai das parcelas que vencem por último.
- **Valor segurado**: com desconto, o invoice é pago menos o valor do desconto. Só é possível segurar o que ainda não foi pago: se o invoice já foi pago (ex.: antecipado), o desconto é apenas deduzido no próximo invoice.
- **Liberação do segurado**: conforme os invoices com a dedução chegam à fábrica. Se o desconto for revalidado para baixo (ou zerado), a diferença fica disponível na hora.
- **Swift**: pode ser anexado depois (fica pendente), mas o processo só termina com todos os Swifts.

### Fluxo de pagamentos (Financeiro)
Menu **Fluxo de pagamentos**: previsão por mês e por moeda de
- invoices a pagar (confirmados, estimados e vencidos);
- liberações do valor segurado (disponíveis e previstas pela chegada dos invoices com dedução);
- custo estimado de importação (40% do invoice), visível a partir do embarque.

Filtros por período, moeda e fornecedor e **exportação para Excel (CSV)**.

### Documentos no OneDrive
Cada documento é um link: salve o arquivo na pasta do OneDrive (o sistema sugere `ano/Fornecedor - Invoice`), copie o link e cole no invoice.
O link da pasta principal é configurado em **Sistema** e fica somente no servidor (não vai para o GitHub).
A tabela de documentos já prevê uma futura integração automática com o OneDrive (Microsoft Graph, que depende de liberação da TI).

### Correções
Quem lança cada informação pode corrigi-la: a Importação corrige dados, itens, condição, etapas, deduções e documentos; o Financeiro corrige pagamentos, Swifts e banco de débito; o Administrador corrige a inspeção (o valor do desconto, pela revalidação). Toda correção fica no histórico com o valor anterior e o novo.

## Usuários e perfis
- Até **10 usuários ativos**.
- **Administrador** (Engenharia de Processos): inspeção, descontos, revalidação, cancelamentos, usuários e sistema; pode executar qualquer etapa.
- **Importação**: criação do invoice, etapas logísticas, deduções e documentos.
- **Financeiro**: banco de débito, pagamentos, liberações e Swifts.
- **Consulta**: somente leitura.

## Backup

Os dados ficam em `instance\descontos.db`. **Copie a pasta `instance` periodicamente** para um local seguro (rede, OneDrive etc.).

## Próximas versões (planejado)
- Relatórios e indicadores (tempo por etapa, pontualidade ETD/ETA, ranking de qualidade, custo real × estimado).
- Notificações de pendências.
- Integração automática dos documentos com o OneDrive.

## Histórico
A versão 3.0 recomeçou os registros do zero. Usuários e fornecedores foram mantidos; os dados da versão 2 (controle só de descontos) ficam preservados no banco com o prefixo `legacy_v2_`.
