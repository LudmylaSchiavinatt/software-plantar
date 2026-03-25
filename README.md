# PLANTAR (Gestão de Contratos)

Aplicação web (Flask) para gerenciar `clientes` (prefeituras/conab), `fornecedores`, `notas_fiscais` e `pagamentos_fornecedores`, com relatórios agregados via *views* no SQLite.

## Visão geral

- Backend: `app.py` (API + páginas HTML em `templates/`)
- Banco: SQLite (`coaipro.db`) com schema em `schema.sql`
- Inicialização e carga de dados:
  - `processador_coaipro.py` (primeira execução: lê `planilhas_modelo/` e carrega no BD)
  - `seed_loader.py` (primeira execução: lê `dados_seed/` e migra tabelas seed)
- Build de executável: `build_exe.py` (empacota com PyInstaller e encripta o código-fonte)

## Requisitos

- Python 3.9+ (recomendado)
- Dependências em `requirements.txt`

## Rodando localmente

1. Criar e ativar ambiente virtual

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Instalar dependências

```powershell
pip install -r requirements.txt
```

3. Iniciar a aplicação

```powershell
python app.py
```

Atenção: execute a partir da raiz do projeto (para funcionar com os paths relativos de `templates/`, `schema.sql`, `planilhas_modelo/` e `coaipro.db`).

A API fica disponível em `http://127.0.0.1:5000/`.

## Inicialização do banco (primeira execução)

Na importação do `app.py`, a função `_inicializar_primeira_execucao()` é executada:

1. Garante a criação/aplicação do schema (`schema.sql`) via `processador_coaipro.garantir_banco_criado()`.
2. Detecta primeira execução com `processador_coaipro.is_primeira_execucao()` (BD inexistente ou sem dados em `notas_fiscais` e `pagamentos_fornecedores`).
3. Se for primeira execução:
   - Executa `seed_loader.migrar_seed_tabelas()` (lê `dados_seed/` e popula as tabelas seed)
   - Executa `processador_coaipro.carregar_planilhas_modelo()` (lê `planilhas_modelo/*.xlsx` e concilia/migra)

Arquivos esperados:

- `dados_seed/` (CSV/Excel): `clientes`, `fornecedores`, `produtos`, `notas_fiscais`, `pagamentos_fornecedores`
- `planilhas_modelo/` (XLSX): planilhas usadas na conciliação inicial

## Endpoints (rotas principais)

Páginas (HTML):

- `/` -> `templates/landing.html`
- `/painel/graficos` -> `templates/graficos.html`
- `/painel/tabelas` -> `templates/tabelas.html`
- `/painel/cadastros` -> `templates/cadastros.html`
- `/painel/visualizador` -> `templates/visualizador.html`
- `/dashboard` -> `templates/dashboard.html`

API (JSON):

- `GET /api/resumo`
- `GET /api/prefeituras`
- `GET /api/fornecedores`
- `GET /api/clientes_lista`
- `GET /api/fornecedores_lista`
- `GET /api/notas_lista`
- `GET /api/notas`
- `POST /api/notas` (criar nota)
- `GET /api/custos/<nota_id>`
- `POST /api/custos` (criar custo/desconto vinculado)
- `GET /api/pagamentos`
- Banco/Exportação/Importação:
  - `GET /api/banco/download`
  - `POST /api/banco/upload` (substitui `coaipro.db`)
  - `GET /api/banco/excel` (exporta todas as tabelas e views)
  - `GET /api/banco/tabelas` (lista tabelas/views + contagem)
  - `GET /api/banco/tabela/<nome>` (dados completos de uma tabela/view)

## Testes

Executar a suíte com `pytest`:

```powershell
pytest
```

Observação: os testes criam e descartam um banco temporário `test_coaipro.db`.

## Build do executável (standalone + embaçamento)

O `build_exe.py` usa uma **chave de distribuição** (para embaçamento/ofuscação). O build **exige** que você forneça uma chave própria.

Defina a chave assim (prioridade):

1. Variável de ambiente `PLANTAR_DIST_KEY`
2. Arquivo `distribution.key` na raiz do projeto

Gera `dist/plantar.exe` e `dist/plantar.key`:

```powershell
python build_exe.py
```

O build:

- encripta `app.py`, `processador_coaipro.py` e `seed_loader.py`
- empacota com PyInstaller
- ao executar o `.exe`, abre o navegador em `http://127.0.0.1:5000`

## Instruções de distribuição (programador)

### Opção 1: distribuir como app Python

1. Entregar o projeto com:
   - `app.py`, `processador_coaipro.py`, `seed_loader.py`, `schema.sql`
   - pastas `templates/`, `dados_seed/`, `planilhas_modelo/`
   - `requirements.txt`
2. No ambiente de destino:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

### Opção 2: distribuir com embaçamento (executável)

1. Definir chave única da sua distribuição:

```powershell
$env:PLANTAR_DIST_KEY="SUA_CHAVE_FERNET_VALIDA_AQUI"
```

Alternativa: criar o arquivo `distribution.key` na raiz do projeto (pode copiar `distribution.key.example` e substituir pelo valor real).

Importante: não compartilhe `distribution.key` nem `dist/plantar.key` entre desenvolvedores; cada dev deve usar seu próprio valor.

Para gerar uma chave Fernet válida (exige `cryptography`):

```powershell
pip install cryptography
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

2. Gerar o executável:

```powershell
python build_exe.py
```

3. Entregar ao usuário final:
   - `dist/plantar.exe`
   - `dist/plantar.key`

Observações:

- A chave neste fluxo serve para **embaçamento/ofuscação** do código empacotado.
- Não trate esse mecanismo como proteção criptográfica forte contra engenharia reversa.

## Handoff para outro desenvolvedor (recomendado)

1. Remover artefatos locais antes de passar o projeto:
   - `dist/` (contém `plantar.key` do build anterior)
   - `build_temp/` (artefatos intermediários do build)
2. Entregar o projeto com código e dados de runtime:
   - `app.py`, `processador_coaipro.py`, `seed_loader.py`, `schema.sql`
   - `templates/`, `dados_seed/`, `planilhas_modelo/`
   - `requirements.txt`
3. Cada dev (ou build) deve gerar sua própria chave:
   - via `PLANTAR_DIST_KEY` ou `distribution.key`

## Onde mexer (guia rápido)

- Estrutura do banco e *views*: `schema.sql`
- Regras de carga a partir de Excel: `processador_coaipro.py`
- Migração seed (CSV/Excel -> BD): `seed_loader.py`
- API + rotas Flask: `app.py`
- UI (HTML/JS): `templates/`
- Testes: `tests/`

