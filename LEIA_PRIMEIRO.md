# Atualização: Minha conta e código de segurança

## Arquivos obrigatórios desta atualização

Substitua app.py e coloque conta_seguranca.py ao lado dele. Copie todos os HTML da pasta templates do pacote para a pasta templates do projeto, preservando os demais arquivos. A atualização inclui a nova minha_conta.html e uma landing.html com o cadastro atualizado. Reinicie o servidor.

Não copie estrutura1.sql por cima do schema.sql: a exportação é uma referência da estrutura, não uma migração idempotente.

O backend adiciona automaticamente duas colunas de segurança em cooperativas (hash e data de alteração) e duas em usuarios (tentativas e bloqueio), sem apagar dados. Não é preciso definir código no .env. Se a conta do banco não tiver permissão de ALTER TABLE, a inicialização falhará e será necessário aplicar a migração com uma conta autorizada.

## Fluxo implementado

- No cadastro Sou Cooperativa: defina o código e confirme. Use 8 a 64 caracteres, sem espaços e diferentes da senha. O campo fornecido pela equipe foi substituído: não há mais código de convite global. O cadastro de novas cooperativas deixa de depender de autorização por código da equipe.
- O código é compartilhado no nível da cooperativa; cada administrador mantém sua senha individual. Não é solicitado no login nem é apresentado novamente.
- Em Minha conta, dados da cooperativa (nome/CNPJ) e meu e-mail exigem o código atual. Esses dados da cooperativa são compartilhados entre seus administradores.
- Definir/trocar o código exige minha senha atual; isso também permite recuperar o uso quando esquecer o código.
- Alterar minha senha exige minha senha atual e o código. Depois, a sessão atual é encerrada para um novo login. Outras sessões já abertas não são revogadas automaticamente nesta atualização.
- Cooperativas existentes sem código definem o primeiro em Minha conta → Segurança, confirmando a senha atual. Até isso acontecer, alterações dos dados e senha por esse painel ficam bloqueadas.
- Cinco erros de senha/código nesse painel geram bloqueio de 5 minutos por usuário; o controle fica no banco e não é removido ao fechar o navegador.
- Formulários de cadastro e Minha conta usam token de proteção CSRF. Apenas administrador autenticado, ativo e aprovado pode usar Minha conta. Os IDs usados são os da sessão, nunca os enviados no formulário.
- Código e senha são armazenados como hashes com salt, usando Werkzeug, sem texto recuperável.

A confirmação por código foi aplicada às ações da Minha conta. Cadastros de contratos/notas, importações e repasses mantêm seu funcionamento anterior e não passam a exigir esse código.

## Testes desta atualização

15 testes HTTP com Flask e banco simulado: acesso anônimo/cooperado negado, CSRF, cadastro com hash, confirmação divergente, troca/primeiro código, atualização dos dados protegida, isolamento por cooperativa, bloqueio de tentativas, troca de senha e encerramento da sessão atual, link visível para administrador. Mais 7 verificações anteriores e sintaxe Python/JavaScript.

Execute no ambiente do projeto: `python testar_minha_conta.py` e `python verificar_correcoes.py`. O servidor PostgreSQL real não estava disponível neste ambiente. Não houve inspeção visual em navegador; valide o layout e a migração em uma cópia do banco antes de usar em produção.

---

# Correções KOKUE — mantendo o padrão existente

Este pacote é uma atualização parcial do projeto existente, não uma instalação completa. Preserve o restante do projeto e faça backup antes de substituir arquivos.

## Instalação

1. Substitua `app.py` na raiz do seu projeto.
2. Substitua `templates/dashboard.html`, `templates/tabelas.html` e `templates/graficos.html`.
3. Reinicie o servidor. O app acrescenta, se necessário, 14 colunas numéricas de descontos já presentes no dashboard, sem apagar dados. A conta do banco precisa de permissão para ALTER TABLE. Também é removida a restrição global antiga de número da nota; a unicidade por cooperativa é mantida.
4. Entre como administrador da cooperativa e confira os fluxos abaixo em uma cópia do banco.

O arquivo dashboard(1).html enviado é igual a dashboard.html; a aplicação utiliza templates/dashboard.html. A tela de entrada landing.html foi derivada do HTML cooperativa_visao_cooperado.html enviado, preservando seus estilos e os fluxos existentes. Faça backup de sua landing.html atual antes de substituir, pois ela não foi enviada como arquivo separado.

## O que foi corrigido

- Abrir os custos de uma nota no dashboard: caminho alinhado à API, com alias para compatibilidade.
- Consulta de notas para os formulários inclui valor_total, utilizado no seletor do dashboard.
- Proteção de acesso ao dashboard: exige login e encaminha cooperados ao próprio extrato.
- Descontos do dashboard antes ignorados passam a ser validados, salvos e considerados no líquido. Campos antigos omitidos pelo formulário de edição de tabelas são preservados.
- Validação de custos rejeita valores negativos, infinitos e NaN e descontos superiores à compra.
- Saldo a pagar e contagem de pendências no resumo descontam repasses parciais. A API de cooperados usa o mesmo saldo. Pagamentos antigos marcados como pagos sem histórico de repasses conservam esse tratamento.
- Rotas implementadas para importar notas de planilha, NF-e XML, lançamentos dos cooperados, consultar produtos de uma nota e extrair produtos de PDF.
- XML preserva produtos, quantidades e unidades informadas no documento.
- Importações de planilha/XML são transacionais: erro provoca rollback de todo o lote. Não há criação automática de clientes ou cooperados; o nome precisa corresponder a um único cadastro da cooperativa.
- Planilha/XML de notas: números de nota já existentes na cooperativa são ignorados, sem atualizar registros existentes.
- Planilha de pagamentos: exige numero_nota_fornecedor para controlar duplicidades; uma nota de cooperado já cadastrada é rejeitada. Consolide os itens da mesma nota em um lançamento antes de usar essa importação. Não importa automaticamente movimentações pagas: registre repasses com data e forma.
- Rateio dos lançamentos por produto em Tabelas mantém o total em centavos, alocando eventual diferença ao último produto.
- Consultas JSON verificam respostas HTTP; o dashboard avisa quando falha ao carregar.

## Formatos de importação

Notas: numero_nota, cliente (ou cliente_nome), data_emissao, valor_total; opcionais status, peso_liquido, valor_frete.

Pagamentos: fornecedor (ou fornecedor_nome), data_emissao, valor_compra (ou bruto), numero_nota_fornecedor; opcionais numero_nota (nota de venda vinculada), produto_descricao, peso_liquido e descontos com os nomes usados no app. Status deve ser pendente.

Datas: aaaa-mm-dd ou dd/mm/aaaa. Valores: 1234.56 ou 1.234,56.

## Verificação realizada

`python verificar_correcoes.py`: 7 verificações isoladas aprovadas (datas, números brasileiros, descontos, valores inválidos, XML com itens, cobertura das rotas fetch e preservação dos estilos).

Compilação Python e verificação de sintaxe JavaScript dos três templates aprovadas. CSS preservado. Nenhuma regra das views financeiras foi reescrita.

A exportação estrutura1.sql foi conferida: contém 12 tabelas e 4 views. As colunas usadas pelas novas rotas de importação, produtos e repasses existem nessa estrutura. Foram também recebidos e lidos seed_loader.py e cotacoes_service.py.

Não foi executada integração com PostgreSQL nem navegação em navegador. Foram executados testes HTTP com Flask e banco simulado; eles não substituem a validação das consultas no PostgreSQL real.

### Correção adicional após conferir a estrutura

A tabela notas_fiscais tinha, ao mesmo tempo, UNIQUE(numero_nota) e um índice único em (cooperativa_id, numero_nota). A primeira regra impedia números iguais entre cooperativas diferentes. O app mantém o índice composto e remove somente a constraint global antiga, dentro de uma transação, sem apagar registros. A checagem de duplicidade continua por cooperativa.

Na view vw_resultados_por_prefeitura, o campo valor_recebido soma notas não canceladas, independentemente do recebimento financeiro. Portanto ele representa faturamento, e valor_a_receber representa o saldo contratual a faturar. Esses cálculos e os rótulos das telas não foram alterados nesta atualização para preservar o padrão solicitado. Não interpretar esses campos como extrato de caixa.

## Pontos que ainda dependem do projeto completo

- Conferir as importações em uma cópia do banco, usando a estrutura real exportada em estrutura1.sql.
- O saldo contratual representa faturamento, conforme conferência da view. Os rótulos financeiros ainda precisam ser alinhados a esse significado em uma revisão autorizada da interface.
- A restauração/envio de banco SQLite já está desativada no backend PostgreSQL; botões antigos continuam sem essa capacidade. Backup completo envolve dados de outras cooperativas e precisa de fluxo administrativo próprio.
- processador_coaipro.py possui processar_planilha_receber sem implementação e carga inicial sem filtro de cooperativa. Não executar essa carga com dados de várias cooperativas antes de revisar schema e seed_loader. O arquivo não foi alterado neste pacote.
- persistencia_danfe.py tem consultas sem isolamento por cooperativa; o app enviado não importa esse módulo. Não o conecte ao fluxo atual sem adaptá-lo e testar.
- Na tela Tabelas, salvar vários produtos ainda envia uma requisição por produto; uma falha intermediária pode deixar lançamentos anteriores salvos. Confira os registros antes de repetir. Uma futura operação em lote deve gravar o conjunto numa única transação.
- A importação PDF existente depende do parser e do formato dos documentos; PDFs digitalizados sem texto podem exigir preenchimento manual.

O schema.sql enviado inicialmente cria apenas repasses_cooperados; estrutura1.sql contém a estrutura completa. A exportação completa não é idempotente: não a use como schema.sql de inicialização automática em um banco já existente.
