# Controle de Invoices e Descontos (v4.52)

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
| 1 | Criação do invoice | Importação | Fornecedor, nº, moeda, **data de assinatura**, **condição de pagamento** (uma ou mais parcelas), agente de carga, despachante, grupo de consolidação, coleta programada e itens (código, descrição, quantidade e preço original) e o **invoice assinado** (arquivo anexado, obrigatório), digitados ou **carregados de uma planilha Excel**. Aparece a **janela de descontos aprovados** do fornecedor (mesma moeda) para deduzir um ou mais, total ou parcialmente; o **valor final** é calculado após as deduções |
| 2 | Coleta | Importação | Data da HAWB e data real de coleta |
| 3 | Embarque | Importação / Financeiro | ETD e ETA planejados (obrigatórios para salvar), ETD e ETA reais; **banco de débito** (Financeiro, obrigatório para concluir a etapa) |
| 4 | Desembaraço | Importação | Data de emissão da DUIMP (e o arquivo da DUIMP, opcional) → data de parametrização, canal (Verde, Amarelo ou Vermelho), exigências da ANVISA, data de desembaraço e **número da NF**. DUIMP e boleto do ICMS seguem por e-mail, fora do sistema (não são anexados nem guardados) |
| 5 | Transporte Terrestre | Importação | Data programada de chegada (obrigatória), data real de chegada na fábrica e **NF Liberada** (sem essa marcação o fluxo não segue para a Inspeção) |
| 6 | Inspeção | Engenharia (administrador) | Fim da inspeção e se há desconto; se sim: valor, data do relatório e aprovação do fornecedor (Enviado → Aprovado) |
| 7 | Pagamento | Financeiro | Pagamento de cada parcela (integral, ou parcial quando há valor segurado), com Swift |
| 8 | Dedução e liberação | Importação / Financeiro | O desconto é deduzido em próximo(s) invoice(s); quando eles chegam, o Financeiro libera o valor segurado, com Swift |

**DUIMP enviada ao banco (v4.33):** depois de preenchida a data de emissão da DUIMP, o Financeiro marca na etapa Desembaraço
a caixa **DUIMP enviada ao banco**. Não trava o processo, mas enquanto não for marcada fica uma pendência do Financeiro.
(Invoices que já tinham chegado à fábrica antes desta versão não geram essa pendência.)

**Aba Importação (v4.33):** lista os invoices em andamento nas etapas 1 a 5 (cadastro até o Transporte Terrestre), com
contagem por etapa (clique para filtrar), busca, a **última informação adicionada** no cronograma do fluxo e as datas principais (HAWB, ETA, chegada). Ao abrir
um invoice, todas as informações das etapas 2 a 5 aparecem lado a lado para editar e salvar de uma vez (mesmas regras da tela
do invoice). Cadastros incompletos levam a "Completar cadastro". Todos veem; só a Importação edita.
Invoices com a **NF Liberada** marcada ou com a **data real de chegada na fábrica** preenchida saem da aba.
Invoices com o mesmo **grupo de consolidação** (carga consolidada) aparecem juntos, um embaixo do outro, num quadro
"Grupo de consolidação N"; a busca também aceita o número do grupo.
Em cada grupo, **Preencher Coleta e Embarque para todo o grupo** grava de uma vez as datas da Coleta (HAWB, coleta real) e do
Embarque (ETD/ETA planejados e reais) em todos os invoices do grupo em andamento. Só os campos preenchidos são aplicados
(vazios não apagam nada); o banco de débito continua por invoice (Financeiro); invoices com cadastro incompleto não recebem.

**Grupo de consolidação no cadastro (v4.34):** ao clicar no campo aparece a lista dos grupos já usados, do maior para o menor,
com a quantidade de invoices e os fornecedores de cada um. Clique para escolher ou digite um novo.

**Painel e Pendências (v4.39):** o **Painel** mostra o desenvolvimento do fluxo completo de cada invoice: uma coluna por etapa
(Cadastro, Coleta, Embarque, Desembaraço, Transporte Terrestre, Inspeção, Pagamento, Dedução/liberação, Concluído) com ✓ e a
data quando concluída, ● na etapa atual, ○ a fazer e – quando não se aplica (sem desconto). A aba **Pendências** reúne as
pendências de todos os setores (as do seu setor no topo), com a contagem por setor e de vencidas.

**DUIMP obrigatória (v4.43):** a etapa Desembaraço só é concluída com o **arquivo da DUIMP anexado** (além das datas, canal e
número da NF). Invoices que já tinham data de desembaraço antes desta versão ficam dispensados.

**Cotação no pagamento (v4.43):** em invoices em moeda estrangeira (USD, EUR, CHF), o registro de pagamento pede a **cotação da
moeda no momento do pagamento** (R$ por 1 unidade). Desde a v4.48 não é obrigatória ao registrar: sem ela, fica a
pendência do Financeiro "Informar a cotação do pagamento" até ser informada em Corrigir. A tabela de pagamentos mostra a cotação e o valor em reais, e a
cotação pode ser corrigida em "Corrigir". Invoices em reais não têm o campo.

**Filtros no painel:** as tabelas de Pendências e de Invoices têm filtro em cada coluna (lista ou texto) e ordenação ao clicar no título da coluna.

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
- **Vencimento de cada parcela**: antecipada = data de **conclusão do cadastro (etapa 1)** + 5 dias úteis (antes disso, sem vencimento); a prazo = data da HAWB (registrada na Coleta) + dias. **Antes da HAWB, a parcela a prazo fica sem vencimento** (não há estimativa).
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

### Excluir um invoice (Administrador)
Na tela do invoice → **Ações do administrador → Excluir este invoice definitivamente**, digitando o número do invoice para
confirmar. Apaga o invoice e tudo o que depende dele (itens, condição, desconto, pagamentos e Swifts, lançamentos e arquivos,
DUIMP, histórico e e-mails). Deduções ligadas a outros invoices também são removidas (o sistema avisa antes e registra no
histórico dos outros invoices). Backup do banco antes da exclusão.

### Apagar todos os invoices (Sistema)
Para recomeçar com dados reais, o Administrador pode, em **Sistema → Apagar todos os invoices** (digitando APAGAR),
remover todos os invoices e o que depende deles (itens, condições, descontos, deduções, pagamentos e Swifts,
lançamentos financeiros e arquivos, histórico e e-mails sobre eles). Usuários, configurações, conta de envio,
destinatários e agendador são mantidos; fornecedores também, a não ser que se marque a opção. Backup do banco antes.

### Liberação do pagamento após a HAWB (Engenharia de Processos)
As parcelas "X dias após a HAWB" ficam **bloqueadas** para o Financeiro, mesmo vencidas, até a Engenharia de Processos
(Administrador) clicar em **Liberar pagamento ao Financeiro** na seção Pagamentos do invoice (com observação opcional).
Enquanto bloqueadas: a pendência é da Engenharia ("Liberar o pagamento ao Financeiro"), o Fluxo de pagamentos mostra
"Bloqueado (Engenharia)" e o Financeiro só consegue registrar pagamento das parcelas antecipadas. Ao liberar, o
Financeiro recebe o e-mail "Pagamento liberado pela Engenharia de Processos". A liberação pode ser desfeita.
Os pagamentos abatem primeiro as parcelas antecipadas e depois as após a HAWB.

**Pagar também o valor segurado (v4.49, caso excepcional):** o Financeiro pode registrar o pagamento do invoice inteiro,
incluindo o valor segurado (ex.: invoice de US$ 1.000 com US$ 100 segurados → pagamento de US$ 1.000). O sistema pede
confirmação, o segurado deixa de ficar retido (não haverá liberação dele) e **o desconto continua aparecendo para a Importação
deduzir no próximo invoice**. Corrigir ou excluir o pagamento devolve o valor ao segurado.

### Carga de histórico (temporária)
Menu da conta → **Carga de histórico** (só Administrador): cadastra processos do passado de uma vez (fornecedor, nº,
moeda, assinatura, HAWB, valor total, condição de pagamento, desconto e status, valor segurado e pagamentos realizados).
O invoice fica marcado como "Histórico": a logística não é cobrada, o valor total vira um item único, a parcela após a
HAWB entra liberada, os pagamentos informados não pedem Swift e a carga não envia e-mails. Depois ele segue o fluxo
normal. **Para remover a ferramenta:** apagar o bloco "CARGA DE HISTÓRICO" do app.py, o template carga_historico.html e
o link no base.html (os invoices carregados continuam válidos).
**Importar planilha** (na mesma tela): Excel com as colunas A Fornecedor, B Invoice, C Moeda (Dólar/Euro/CHF/Real),
D Data de assinatura, E HAWB, F Total, G % adiantamento, H % depois de HAWB, I dias depois de HAWB e J Valor pago.
Mostra uma prévia (com os erros de cada linha) e só grava ao confirmar. O valor pago vira um pagamento com data
estimada pelo vencimento (limitada a hoje). Moedas aceitas no sistema: USD, EUR, CHF e BRL.

### Horário
Todo o sistema usa o **horário de Brasília** (UTC−3), independentemente do fuso do servidor (o PythonAnywhere roda em
UTC). Na atualização para a v4.21, os horários já gravados num servidor em UTC são convertidos uma única vez.

### Esqueci minha senha
Na tela de login, **Esqueci minha senha**: o usuário informa o e-mail e recebe (pela conta de envio da aba E-mails) um
**código de 6 dígitos** válido por 15 minutos, que digita junto com a nova senha. Sem links no e-mail; o código só vale
uma vez, é cancelado após 5 erros, há limite de 3 pedidos por hora e a tela não revela se o e-mail está cadastrado.
O código não fica guardado no histórico de e-mails. O Administrador continua podendo redefinir senhas em Usuários.

### Fornecedores: condição padrão e lembretes
Em **Fornecedores → Configurar**, a Importação cadastra para cada fornecedor:
- **Condição de pagamento padrão**: ao escolher o fornecedor na criação do invoice, a condição é preenchida (continua editável).
- **Lembretes**: aparecem numa janela ao escolher o fornecedor na criação do invoice. **OK** dá o lembrete como visto (não
  aparece mais; fica no histórico com quem e quando); **Me lembre no próximo invoice** fecha e ele volta na próxima criação.

### Anexos: arquivos guardados no sistema (v4.31)
Todo anexo (invoice assinado, DUIMP, Swift de cada pagamento, documentos dos lançamentos financeiros) é **enviado pelo
navegador e guardado no servidor** (pasta `instance/arquivos`, fora do GitHub), até 10 MB por arquivo. Ao clicar no nome,
o arquivo abre numa **nova aba de visualização** (PDF, imagens, planilhas .xlsx, textos/CSV/XML e Word .docx são
mostrados na própria tela; os demais tipos mostram só os dados do arquivo), com o botão **Baixar cópia**.
- **Invoice assinado** é obrigatório para concluir o cadastro (etapa 1); pode ser anexado na criação ou depois, na tela do invoice.
  Na criação há dois campos: **Arquivo 1** (obrigatório) e **Arquivo 2** (opcional).
- Na seção **Pagamentos** do invoice há o botão **Abrir** do invoice assinado, para o Financeiro conferir antes de pagar.
- Quem anexa pode excluir: Importação (invoice assinado, DUIMP), Financeiro (Swift), quem anexou ou o Administrador (lançamentos).
- Os caminhos na pasta da rede cadastrados nas versões 4.27 a 4.30 continuam aparecendo (somente consulta, com **Copiar**),
  mas não geram mais pendência de "arquivo não encontrado".
- O espaço ocupado pelos anexos aparece em **Sistema**.

### Invoices da carga de histórico (v4.51)
Não geram pendências. As etapas até a chegada (cadastro, coleta, embarque, desembaraço e transporte) são puladas (aparecem como
concluídas no Painel). A inspeção e o desconto em diante são preenchidos na tela do invoice (a inspeção pode ser registrada
mesmo sem data de chegada). Na carga, se não for informado desconto, a inspeção fica para a Engenharia registrar.
Invoices cadastrados pelo fluxo normal também podem ser **tratados como histórico** (v4.52): em Sistema → "Tratar invoices como
histórico" (vários números de uma vez) ou no invoice → Ações do administrador → "Tratar como histórico" (com "Voltar ao fluxo
normal" para desfazer).

### Fator de importação (v4.44)
**Fator = 1 + custos não recuperáveis ÷ FOB em R$** (mesma conta da planilha de Follow Up de Importação).
- **FOB**: valor final do invoice (depois das deduções de desconto), convertido pelo **câmbio dos pagamentos** registrados
  (média ponderada das cotações). Enquanto não há pagamento com cotação, usa a **taxa da DUIMP** como estimativa.
- **Custos**: lançamentos financeiros do invoice, por **categoria fixa** (aba Lançamentos Financeiros): II, Taxa Siscomex, frete
  internacional + taxas, AFRMM, armazenagens, fretes de remoção e nacional, GRU/Anuência, LI/LPCO, sindicato, honorários e ISS do
  despachante, multa. **IPI, PIS, COFINS e ICMS são recuperáveis** e não entram no fator (aparecem como % do FOB).
- **DUIMP**: ao anexar o PDF da DUIMP, o sistema lê número, data de registro, taxa de câmbio, FOB, frete, valor aduaneiro e os
  tributos e abre a tela **Dados da DUIMP** para a Importação conferir. Ao salvar, os tributos viram lançamentos financeiros (sem
  duplicar: se já existir, oferece atualizar). Os tributos federais vêm do "Demonstrativo dos Débitos" (extrato completo da
  DUIMP); taxa, frete e ICMS vêm do texto do despachante (modelos M3 e Fischer/FER já reconhecidos). O que não for lido é digitado.
- O fator aparece na seção **Fator de importação** do invoice (com o detalhamento) e na coluna **Fator** do Painel: em negrito
  verde quando final (invoice concluído, DUIMP conferida, pagamentos com cotação e todos os custos com categoria), em itálico
  quando provisório.
- Lançamentos antigos com tipo livre foram convertidos para a categoria correspondente quando possível; os demais (ex.:
  Capatazia, Seguro) ficam marcados para escolher a categoria e não entram no fator até isso.

### Inspeção em duas partes (v4.45)
A Engenharia de Processos registra primeiro só a **data do fim da inspeção**, com "Tem desconto?" = **A definir**. Enquanto o
resultado não for definido, fica a pendência **"Relatório de Inspeção Pendente"** para a Engenharia (o invoice continua em
"Aguardando inspeção"). Depois, em "Definir o resultado da inspeção", escolhe Não (pagamento integral) ou Sim (valor, data do
relatório e relatório em Excel). Sem a data, a pendência é "Registrar o fim da inspeção".

### Envio do relatório ao fornecedor (v4.46)
Ao cadastrar o desconto, a **data do relatório** e a **aprovação do fornecedor** vêm em branco. Sem aprovação escolhida, o desconto
fica como **"Relatório pendente de envio"** (pendência da Engenharia: enviar o relatório ao fornecedor). O botão **"Marcar relatório
como enviado ao fornecedor"** passa para "Enviado" e, depois, "Marcar desconto como aprovado". A aba **Descontos** tem filtros
por coluna, mostra também os invoices com **relatório de inspeção pendente** (fim da inspeção registrado, resultado a definir) e
filtros rápidos para cada situação.

### Relatório de inspeção obrigatório (v4.41)
- Ao registrar a inspeção **com desconto**, é obrigatório anexar o **relatório de inspeção em Excel** (.xlsx, .xlsm ou .xls);
  sem ele o desconto não é cadastrado.
- Ao **revalidar** o desconto para um valor diferente de 0, além do motivo é obrigatório anexar a **revisão do relatório**
  em Excel. Revalidar para 0 dispensa o arquivo.
- Os relatórios ficam na seção Inspeção do invoice (relatório do desconto e, na tabela de revalidações, o relatório de cada
  revisão), abrem na tela de visualização e não podem ser excluídos (fazem parte do histórico). Descontos registrados antes
  desta versão mostram "Não anexado" e aceitam o relatório em "Editar inspeção".

### Descontos avulsos (v4.38)
Descontos para um fornecedor **sem invoice de origem** (acordo comercial, crédito de frete etc.), cadastrados **somente pela
Engenharia de Processos** na aba **Descontos → Descontos avulsos**: fornecedor, moeda, valor e **justificativa obrigatória**
(do que se trata).
- Aparecem para a Importação na janela de descontos ao cadastrar o próximo invoice do fornecedor (mesma moeda), junto com os
  descontos aprovados de invoices, e também em "Descontos deduzidos neste invoice" para deduzir depois. Podem ser usados
  total ou parcialmente, em um ou mais invoices.
- O histórico fica na aba: valor, quem cadastrou e quando, cada dedução (invoice, valor, data) e o saldo. No invoice, a dedução
  mostra a justificativa e fica no histórico do invoice.
- A Engenharia pode **encerrar o saldo** (com motivo): o restante deixa de aparecer para dedução; as deduções já feitas permanecem.
- Não têm valor segurado nem liberação (não há invoice de origem retido).

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
| Pagamento programado | Parcela antecipada (conclusão do cadastro + 5 dias úteis) ou parcela após a HAWB fica com data e valor definidos | Financeiro |
| Pagamento alterado | Muda a data ou o valor de uma parcela já programada (HAWB corrigida, itens, dedução…) ou o invoice é cancelado | Financeiro |
| Impostos de nacionalização | ETA planejado cadastrado/alterado (pagar entre ETA −1 e +3 dias úteis) e ETA real (ETA real a +2 dias úteis) | Financeiro |
| Pagamento liberado pela Engenharia de Processos | Engenharia libera o pagamento das parcelas após a HAWB | Financeiro |
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
