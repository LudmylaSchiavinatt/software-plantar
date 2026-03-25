# seed_loader.py
"""
Migra dados já disponíveis em tabelas (CSV/Excel) para construir o banco seed.
Na primeira execução, os arquivos em dados_seed/ são lidos e inseridos no BD.
"""

import sqlite3
import pandas as pd
from pathlib import Path
from datetime import datetime

PASTA_DADOS_SEED = 'dados_seed'


def _dir_base():
    return Path(__file__).resolve().parent


def _caminho_seed(pasta=None):
    p = Path(pasta or PASTA_DADOS_SEED)
    if not p.is_absolute():
        p = _dir_base() / p
    return p


def _parse_date(val):
    if pd.isna(val) or val is None or val == '':
        return None
    if isinstance(val, (datetime, pd.Timestamp)):
        return val.date().isoformat() if hasattr(val, 'date') else str(val)[:10]
    s = str(val).strip()[:10]
    if not s:
        return None
    return s


def _parse_float(val):
    if pd.isna(val) or val is None or val == '':
        return 0.0
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _carregar_df(pasta, nome, extensoes=('csv', 'xlsx')):
    """Carrega um DataFrame por nome de entidade (ex: clientes). Procura nome.csv ou aba com esse nome."""
    # CSV: clientes.csv, fornecedores.csv, etc.
    fcsv = pasta / f'{nome}.csv'
    if fcsv.exists():
        return pd.read_csv(fcsv, encoding='utf-8-sig')
    # Excel: seed.xlsx ou qualquer .xlsx com aba igual a nome ou Nome
    for x in sorted(pasta.glob('*.xlsx')):
        for sheet in (nome, nome.capitalize(), nome.replace('_', ' ').title()):
            try:
                return pd.read_excel(x, sheet_name=sheet)
            except Exception:
                continue
    return None


def migrar_seed_tabelas(db_path='coaipro.db', pasta=None):
    """
    Migra dados das tabelas em dados_seed/ para o BD, construindo o banco seed.
    Ordem: fornecedores -> clientes -> produtos -> notas_fiscais -> pagamentos_fornecedores.
    Retorna True se pelo menos uma tabela foi carregada.
    """
    pasta = _caminho_seed(pasta)
    if not pasta.exists():
        return False

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    carregou = False

    # 1) Fornecedores (sem FK)
    df = _carregar_df(pasta, 'fornecedores')
    if df is not None and not df.empty:
        for _, row in df.iterrows():
            nome = str(row.get('nome', '')).strip()
            if not nome or nome == 'nan':
                continue
            cur.execute(
                "INSERT OR IGNORE INTO fornecedores (nome, cpf_cnpj, cidade, ativo) VALUES (?, ?, ?, ?)",
                (
                    nome,
                    str(row.get('cpf_cnpj', '') or '').strip() or None,
                    str(row.get('cidade', '') or '').strip() or None,
                    1 if str(row.get('ativo', 1)).strip() not in ('0', 'false', 'False') else 0,
                ),
            )
        conn.commit()
        carregou = True

    # 2) Clientes (sem FK)
    df = _carregar_df(pasta, 'clientes')
    if df is not None and not df.empty:
        for _, row in df.iterrows():
            nome = str(row.get('nome', '')).strip()
            if not nome or nome == 'nan':
                continue
            cur.execute(
                """INSERT OR IGNORE INTO clientes (nome, tipo, cidade, prazo_pagamento_dias, ativo)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    nome,
                    str(row.get('tipo', 'prefeitura') or 'prefeitura').strip() or 'prefeitura',
                    str(row.get('cidade', '') or '').strip() or None,
                    int(_parse_float(row.get('prazo_pagamento_dias', 30))),
                    1 if str(row.get('ativo', 1)).strip() not in ('0', 'false', 'False') else 0,
                ),
            )
        conn.commit()
        carregou = True

    # 3) Produtos (opcional)
    df = _carregar_df(pasta, 'produtos')
    if df is not None and not df.empty:
        for _, row in df.iterrows():
            nome = str(row.get('nome', '')).strip()
            if not nome or nome == 'nan':
                continue
            cur.execute(
                """INSERT OR IGNORE INTO produtos (nome, variedade, unidade_medida, preco_referencia)
                   VALUES (?, ?, ?, ?)""",
                (
                    nome,
                    str(row.get('variedade', '') or '').strip() or None,
                    str(row.get('unidade_medida', 'kg') or 'kg').strip() or 'kg',
                    _parse_float(row.get('preco_referencia')),
                ),
            )
        conn.commit()
        carregou = True

    # 4) Notas Fiscais (exige cliente_id; aceita coluna cliente_nome para buscar)
    df = _carregar_df(pasta, 'notas_fiscais')
    if df is not None and not df.empty:
        for _, row in df.iterrows():
            numero_nota = str(row.get('numero_nota', '')).strip()
            if not numero_nota or numero_nota == 'nan':
                continue
            cliente_id = None
            if 'cliente_id' in row and pd.notna(row.get('cliente_id')):
                try:
                    cliente_id = int(row['cliente_id'])
                except (TypeError, ValueError):
                    pass
            if cliente_id is None and 'cliente_nome' in row:
                nome_cli = str(row.get('cliente_nome', '')).strip()
                if nome_cli:
                    cur.execute("SELECT id FROM clientes WHERE nome = ? OR nome LIKE ?", (nome_cli, f'%{nome_cli}%'))
                    r = cur.fetchone()
                    if r:
                        cliente_id = r[0]
            if cliente_id is None:
                continue
            data_emissao = _parse_date(row.get('data_emissao'))
            if not data_emissao:
                continue
            cur.execute(
                """INSERT OR REPLACE INTO notas_fiscais (
                       numero_nota, data_emissao, cliente_id, peso_liquido, quantidade_cx,
                       valor_total, valor_frete, previsao_pagamento, data_pagamento, status, observacoes
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    numero_nota,
                    data_emissao,
                    cliente_id,
                    _parse_float(row.get('peso_liquido')),
                    int(_parse_float(row.get('quantidade_cx', 0))) if pd.notna(row.get('quantidade_cx')) else None,
                    _parse_float(row.get('valor_total', 0)),
                    _parse_float(row.get('valor_frete', 0)),
                    _parse_date(row.get('previsao_pagamento')),
                    _parse_date(row.get('data_pagamento')),
                    str(row.get('status', 'emitida') or 'emitida').strip(),
                    str(row.get('observacoes', '') or '').strip() or None,
                ),
            )
        conn.commit()
        carregou = True

    # 5) Pagamentos a Fornecedores (exige fornecedor_id; aceita fornecedor_nome)
    df = _carregar_df(pasta, 'pagamentos_fornecedores')
    if df is not None and not df.empty:
        for _, row in df.iterrows():
            fornecedor_id = None
            if 'fornecedor_id' in row and pd.notna(row.get('fornecedor_id')):
                try:
                    fornecedor_id = int(row['fornecedor_id'])
                except (TypeError, ValueError):
                    pass
            if fornecedor_id is None and 'fornecedor_nome' in row:
                nome_forn = str(row.get('fornecedor_nome', '')).strip()
                if nome_forn:
                    cur.execute("SELECT id FROM fornecedores WHERE nome = ? OR nome LIKE ?", (nome_forn, f'%{nome_forn}%'))
                    r = cur.fetchone()
                    if r:
                        fornecedor_id = r[0]
            if fornecedor_id is None:
                continue
            data_emissao = _parse_date(row.get('data_emissao'))
            if not data_emissao:
                continue
            valor_liquido = _parse_float(row.get('valor_liquido', 0))
            if valor_liquido == 0:
                valor_liquido = _parse_float(row.get('valor_compra', 0))
            cur.execute(
                """INSERT INTO pagamentos_fornecedores (
                       fornecedor_id, data_emissao, prefeitura, produto_descricao, numero_nota_fornecedor,
                       peso_liquido, valor_compra, funrural, taxa_cooperativa, valor_liquido, status
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fornecedor_id,
                    data_emissao,
                    str(row.get('prefeitura', '') or '').strip() or None,
                    str(row.get('produto_descricao', '') or '').strip() or None,
                    str(row.get('numero_nota_fornecedor', '') or '').strip() or None,
                    _parse_float(row.get('peso_liquido')),
                    _parse_float(row.get('valor_compra', 0)),
                    _parse_float(row.get('funrural', 0)),
                    _parse_float(row.get('taxa_cooperativa', 0)),
                    valor_liquido,
                    str(row.get('status', 'pendente') or 'pendente').strip(),
                ),
            )
        conn.commit()
        carregou = True

    conn.close()
    return carregou
