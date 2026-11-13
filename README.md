# Controle de Invoices e Descontos (v4.19)

Sistema único que controla o ciclo completo de cada invoice de fornecedor, da criação até o pagamento completo,
reunindo **Importação**, **Engenharia de Processos** (inspeção e descontos) e **Financeiro** (pagamentos).
Valores em **dólar, euro ou real**. Visual na identidade da **Quinelato** (azul #327FEF, fonte Plus Jakarta Sans e logo, conforme quinelato.com.br). Arquivos (Swift de cada pagamento e documentos dos lançamentos financeiros) são **anexados direto no sistema** e ficam no servidor.

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
| 4 | Desembaraço | Importação | Data de emissão da DUIMP → data de parametrização, canal (Verde ou Amarelo), exigências da ANVISA e data de desembaraço. DUIMP e boleto do ICMS seguem por e-mail, fora do sistema (não são anexados nem guardados) |
| 5 | Transporte | Importação | Data programada de chegada (obrigatória) e data real de chegada na fábrica |
| 6 | Inspeção | Engenharia (administrador) | Fim da inspeção e se há desconto; se sim: valor, data do relatório e aprovação do fornecedor (Enviado → Aprovado) |
| 7 | Pagamento | Financeiro | Pagamento de cada parcela (integral, ou parcial quando há valor segurado), com Swift |
| 8 | Dedução e liberação | Importação / Financeiro | O desconto é deduzido em próximo(s) invoice(s); quando eles chegam, o Financeiro libera o valor segurado, com Swift |

**Salvar sempre funciona**: no cadastro basta fornecedor e nº do invoice; nas etapas logísticas, qualquer informação pode ser salva a qualquer momento, **desde que o cadastro esteja completo** (a etapa 2, Coleta, só começa com todas as informações obrigatórias do cadastro). A etapa só é concluída (e o processo só avança) quando todas as informações obrigatórias (*) estão preenchidas. O painel mostra as **pendências de todos os setores** (as do setor de quem está logado no topo, as dos outros setores abaixo; cada setor só preenche a sua parte) com o que falta em cada etapa e também as informações não obrigatórias ainda vazias (exceto Exigências da ANVISA e Grupo de consolidação), até serem preenchidas.

O invoice fica **Concluído** quando está pago, o desconto (se houver) foi deduzido e liberado, e todos os pagamentos têm Swift.

### Itens e carga por planilha
- **Valor total do invoice** = soma de quantidade × preço original de cada item. **Valor final** = valor total − deduções de descontos aplicadas no invoice.
- Com desconto deduzido no invoice, aparecem duas colunas calculadas (somente exibição): **Preço com desconto** = preço original × (total com desconto ÷ total original) e **Total com desconto** por item, cuja soma é exatamente o total original − descontos aplicados.
- Para invoices extensos, use **Carregar itens de uma planilha Excel** na criação (ou correção) do invoice: colunas **Código, Descrição, Quantidade, Preço Original** (o botão **Baixar modelo** traz a planilha pronta). Aceita .xlsx e .csv, até 2.000 itens; linhas inválidas são apontadas pelo número.
- Código, quantidade e preço são obrigatórios em cada item.
- **Exportar itens para Excel**: na tela do invoice (itens salvos) ou no cadastro/correção (o que está na tela, antes de salvar), com Código, Descrição, Quantidade, Preço Original, Total e, havendo desconto, Preço com Desconto, Total com Desconto e os totais.

### Regras de pagamento
- **Condição de pagamento em parcelas** que somam 100% (ex.: 25% antecipado + 75% 60 dias após a HAWB).
- **Vencimento de cada parcela**: antecipada = data de assinatura + 5 dias úteis; a prazo = data da HAWB (registrada na Coleta) + dias. **Antes da HAWB, a parcela a prazo fica sem vencimento** (não há estimativa).
- Pagamentos são distribuídos nas parcelas por ordem de vencimento; o valor segurado sai das parcelas que vencem por último.
- **Valor segurado**: com desconto, o invoice é pago menos o valor do desconto. Só é possível segurar o que ainda não foi pago: se o invoice já foi pago (ex.: antecipado), o desconto é apenas deduzido no próximo invoice.
- **Liberação do segurado**: conforme os invoices com a dedução chegam à fábrica. Se o desconto for revalidado para baixo (ou zerado), a diferença fica disponível na hora. Cada parcela liberada vence na **data real de chegada na fábrica do invoice deduzido + 5 dias úteis**; enquanto esse invoice não chega, não há vencimento ("aguardando a chegada na Quinelato"). A diferença de uma revalidação para baixo vence na data da revalidação + 5 dias úteis.
- **Swift**: pode ser anexado depois (fica pendente), mas o processo só termina com todos os Swifts.

### Fluxo de pagamentos (Financeiro)
Menu **Fluxo de pagamentos**: previsão por mês e por moeda de
- invoices a pagar (confirmados, estimados e vencidos);
- liberações do valor segurado (disponíveis e previstas pela chegada dos invoices com dedução);
- custo estimado de importação (40% do invoice), visível a partir do embarque.

Filtros por período, moeda e fornecedor e **exportação para Excel (CSV)**.

### Apagar todos os invoices (Sistema)
Para recomeçar com dados reais, o Administrador pode, em **Sistema → Apagar todos os invoices** (digitando APAGAR),
remover todos os invoices e o que depende deles (itens, condições, descontos, deduções, pagamentos e Swifts,
lançamentos financeiros e arquivos, histórico e e-mails sobre eles). Usuários, configurações, conta de envio,
destinatários e agendador são mantidos; fornecedores também, a não ser que se marque a opção. Backup do banco antes.

### Esqueci minha senha
Na tela de login, **Esqueci minha senha**: o usuário informa o e-mail e recebe (pela conta de envio da aba E-mails) um
**código de 6 dígitos** válido por 15 minutos, que digita junto com a nova senha. Sem links no e-mail; o código só vale
uma vez, é cancelado após 5 erros, há limite de 3 pedidos por hora e a tela não revela se o e-mail está cadastrado.
O código não fica guardado no histórico de e-mails. O Administrador continua podendo redefinir senhas em Usuários.

### Arquivos anexados (padrão do sistema)
Todo anexo é feito pelo botão **Anexar** (envio do arquivo pelo navegador; vários de uma vez; até 10 MB cada; PDF,
imagens, Excel, Word, XML, ZIP…), nunca por link. Os arquivos ficam no servidor, na pasta `instance/arquivos`
(fora do GitHub); qualquer usuário logado abre/baixa. A página **Sistema** mostra quantos são e o espaço ocupado.
- **Swift** de cada pagamento: anexado pelo Financeiro ao registrar o pagamento ou depois, na tabela de pagamentos.
  Pagamentos antigos com link do OneDrive continuam válidos ("Swift (link antigo)").
- **Lançamentos Financeiros**: arquivos de cada custo (ver abaixo).
DUIMP e boleto do ICMS seguem por e-mail, fora do sistema.

### Correções
Quem lança cada informação pode corrigi-la: a Importação corrige dados, itens, condição, etapas e deduções; o Financeiro corrige pagamentos, Swifts e banco de débito; o Administrador corrige a inspeção (o valor do desconto, pela revalidação). Toda correção fica no histórico com o valor anterior e o novo.

## Usuários e perfis
- Até **20 usuários ativos**.
- **Administrador** (Engenharia de Processos): inspeção, descontos, revalidação, cancelamentos, usuários e sistema; pode executar qualquer etapa.
- **Importação**: criação do invoice, etapas logísticas e deduções.
- **Financeiro**: banco de débito, pagamentos, liberações e Swifts.
- **Consulta**: somente leitura.

## Backup

Os dados ficam em `instance\descontos.db`. **Copie a pasta `instance` periodicamente** para um local seguro (rede, OneDrive etc.).

## Próximas versões (planejado)
- Relatórios e indicadores (tempo por etapa, pontualidade ETD/ETA, ranking de qualidade, custo real × estimado).
- Notificações de pendências.

## Histórico
A versão 3.0 recomeçou os registros do zero. Usuários e fornecedores foram mantidos; os dados da versão 2 (controle só de descontos) ficam preservados no banco com o prefixo `legacy_v2_`.

## E-mails automáticos (v4.0)
Configurados pelo Administrador na aba **E-mails** (canto superior, ao lado de Usuários/Sistema): conta de envio (servidor SMTP: conta corporativa ou Gmail),
destinatários (Para/Cc) e ativação de cada fluxo, horário do resumo diário e histórico de tudo que foi enviado.

| E-mail | Quando | Sugestão de destino |
|---|---|---|
| Pagamento programado | Parcela antecipada (assinatura + 5 dias úteis) ou parcela após a HAWB fica com data e valor definidos | Financeiro |
| Pagamento alterado | Muda a data ou o valor de uma parcela já programada (HAWB corrigida, itens, dedução…) ou o invoice é cancelado | Financeiro |
| Impostos de nacionalização | ETA planejado cadastrado/alterado (pagar entre ETA −1 e +3 dias úteis) e ETA real (ETA real a +2 dias úteis) | Financeiro |
| Pendência do Financeiro na etapa de Embarque (banco de débito) | ETA real cadastrado e banco ainda vazio | Financeiro |
| DUIMP registrada: impostos aptos a lançamento no MEGA | Data de emissão da DUIMP cadastrada (ou corrigida) | Anny (Financeiro), Cc Gabriel (Importação) |
| Desconto cadastrado: segurar valor | Desconto registrado na inspeção (a dedução em outro invoice só fica disponível após a aprovação) | Financeiro, Cc Importação |
| Valor segurado liberado | Invoice com a dedução chega à fábrica (pagar até chegada + 5 dias úteis) ou revalidação para baixo | Financeiro |
| Chegada de invoice com dedução | Chegada real na fábrica de invoice que recebeu dedução de outro invoice | Engenharia de Processos |
| Resumo semanal | Toda segunda-feira às 8h (Brasília): todas as pendências do Financeiro (vencidos, semana, próximos, sem data) | Financeiro |
| Pendências vencidas ou que vencem hoje | Todos os dias às 8h (Brasília), só se houver algo vencido ou vencendo no dia | Financeiro |

**Cópia de segurança dos destinatários:** na aba E-mails, "Guardar configuração atual" salva Para/Cc/ativo de todos os
fluxos; "Restaurar configuração guardada" volta a ela (útil para testes com outros destinatários).

Funcionamento: a cada alteração salva, o sistema compara a previsibilidade de cada invoice com a última notificada e
gera os e-mails das diferenças (fila na tabela `emails`, enviados logo após a página responder; até 3 tentativas).
Ao instalar a v4.0, a situação atual é registrada sem enviar nada sobre o histórico. Dias úteis = segunda a sexta
(feriados não são considerados). A senha da conta de envio fica só no banco do servidor (pasta `instance/`). Criptografia SSL/TLS (465) ou STARTTLS (587).
No plano gratuito do PythonAnywhere só é possível enviar pelo Gmail; servidor próprio exige plano pago.
Os resumos são disparados por um agendador externo gratuito (cron-job.org) que chama, todo dia às 8h de Brasília,
o endereço secreto exibido na aba E-mails (o passo a passo está na própria aba). Se o agendador falhar, os resumos
saem no primeiro acesso ao sistema depois das 8h. Cada resumo sai no máximo uma vez por dia.

## Lançamentos Financeiros (v4.6)
Aba separada do fluxo de invoices, para a **Importação apontar os custos de cada importação** (COFINS, ICMS, PIS,
armazenagem…) que o **Financeiro deve lançar no MEGA** (ERP da Quinelato). Todo invoice cadastrado aparece automaticamente com um painel.

- **Importação** (e Administrador): inclui, corrige e exclui custos: tipo de pagamento (livre, com sugestões), valor em R$
  e data do pagamento. Todos os campos são opcionais (a linha só não pode ficar totalmente vazia).
- **Financeiro** (e Administrador): marca **Lançado no MEGA** em cada custo (fica registrado quem e quando); pode desmarcar.
- Se a Importação altera um custo já lançado no MEGA, ele volta para "a lançar" com o aviso **"Alterado: conferir no MEGA"**.
  Custo já lançado no MEGA só pode ser excluído depois que o Financeiro desmarcar (o Administrador pode excluir direto).
- Quadros com o que falta lançar no MEGA, filtro "Com custos a lançar no MEGA", busca e exportação para Excel (CSV).
- Botão **Avisar o Financeiro** (Importação), em cada invoice com custos a lançar: envia na hora o e-mail
  "Custos a lançar no MEGA" com tudo o que falta lançar daquele invoice (e mensagem opcional). Destinatários
  configurados na aba E-mails (sugestão: Anny em Para e o restante do Financeiro em Cc).
- **Arquivos por lançamento** (guias, boletos, comprovantes, planilhas…): Importação, Financeiro e Administrador anexam
  pelo botão **Anexar** de cada custo (vários arquivos, até 10 MB cada; PDF, imagens, Excel, Word, XML, ZIP…). Qualquer
  usuário logado abre/baixa; só quem anexou (ou o Administrador) exclui. Os arquivos ficam no servidor, na pasta
  `instance/arquivos` (fora do GitHub), e seguem anexados no e-mail "Avisar o Financeiro" (até 20 MB no total).
- Não gera pendências; os e-mails só saem pelo botão. Tudo fica no histórico do invoice.
