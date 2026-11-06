"""
persistencia_danfe.py

Modulo responsavel por gravar no PostgreSQL os dados ja extraidos e
reconciliados pelo processador_danfe.py (funcao reconciliar_notas()).

IMPORTANTE - premissas sobre o schema REAL do banco (confirmadas via
diagnostico_schema.py), que diferem do schema_postgres.sql original:

    - notas_fiscais NAO tem coluna chave_acesso
        -> nunca referenciamos essa coluna aqui.
    - clientes NAO tem coluna cpf_cnpj
        -> a prefeitura/cliente e' localizada por NOME normalizado,
           nunca por CNPJ.
    - fornecedores.cpf_cnpj e produtos.nome NAO tem constraint UNIQUE
        -> nao da' pra usar "INSERT ... ON CONFLICT". Em vez disso,
           fazemos um SELECT de checagem antes de cada INSERT
           (find-or-create manual).
    - contrato_cooperado e pagamentos_fornecedores.cliente_id batem
        com o que o schema_postgres.sql descreve, entao essa parte
        (calculo de cota) nao precisou de ajuste aqui.

Se no futuro voces rodarem uma migration adicionando essas UNIQUE
constraints, as funcoes get_or_create_* abaixo podem ser trocadas por
um INSERT ... ON CONFLICT DO NOTHING/UPDATE mais simples e com menos
risco de race condition. Por enquanto, o find-or-create manual e' a
opcao que funciona sem tocar no banco.
"""

import logging
from typing import Optional, Dict, Any, List

from processador_danfe import normalizar_texto

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fornecedores (cooperados)
# ---------------------------------------------------------------------------

def get_or_create_fornecedor(cur, cpf_cnpj: str, nome: str) -> Optional[int]:
    """
    Busca um fornecedor pelo cpf_cnpj. Se nao existir, cria.
    Como fornecedores.cpf_cnpj NAO tem UNIQUE no banco real, fazemos
    SELECT antes do INSERT em vez de ON CONFLICT.
    """
    if not cpf_cnpj:
        logger.warning("Fornecedor sem cpf_cnpj (nome=%s) - nao foi possivel localizar/criar.", nome)
        return None

    cur.execute(
        "SELECT id FROM fornecedores WHERE cpf_cnpj = %s LIMIT 1",
        (cpf_cnpj,)
    )
    row = cur.fetchone()
    if row:
        return row['id'] if isinstance(row, dict) else row[0]

    cur.execute(
        """
        INSERT INTO fornecedores (nome, cpf_cnpj, ativo)
        VALUES (%s, %s, TRUE)
        RETURNING id
        """,
        (nome or cpf_cnpj, cpf_cnpj)
    )
    row = cur.fetchone()
    novo_id = row['id'] if isinstance(row, dict) else row[0]
    logger.info("Fornecedor criado: id=%s cpf_cnpj=%s nome=%s", novo_id, cpf_cnpj, nome)
    return novo_id


# ---------------------------------------------------------------------------
# Clientes (prefeituras) - localizados por NOME, nunca por CNPJ
# ---------------------------------------------------------------------------

def find_cliente_by_nome(cur, nome: str) -> Optional[int]:
    """
    Localiza um cliente/prefeitura pelo nome, usando comparacao
    normalizada (maiusculas, sem acento) via ILIKE.
    Nao cria automaticamente: como nao existe cpf_cnpj em clientes
    para conferir unicidade com seguranca, criar cliente novo aqui
    poderia duplicar prefeitura por causa de pequenas variacoes no
    texto do DANFE. Melhor cadastrar clientes manualmente e so casar
    aqui.
    """
    if not nome:
        return None

    nome_norm = normalizar_texto(nome)
    cur.execute(
        "SELECT id, nome FROM clientes WHERE ativo = TRUE"
    )
    candidatos = cur.fetchall()
    for c in candidatos:
        nome_cli = c['nome'] if isinstance(c, dict) else c[1]
        if normalizar_texto(nome_cli) == nome_norm:
            return c['id'] if isinstance(c, dict) else c[0]

    # fallback: tenta um match parcial (contido) caso o nome do DANFE
    # venha com sufixo/prefixo diferente (ex.: "PREFEITURA MUNICIPAL DE X")
    for c in candidatos:
        nome_cli = c['nome'] if isinstance(c, dict) else c[1]
        nome_cli_norm = normalizar_texto(nome_cli)
        if nome_cli_norm in nome_norm or nome_norm in nome_cli_norm:
            logger.info("Cliente casado por match parcial: '%s' ~ '%s'", nome, nome_cli)
            return c['id'] if isinstance(c, dict) else c[0]

    logger.warning("Cliente/prefeitura nao encontrado para nome='%s'. Cadastre manualmente.", nome)
    return None


# ---------------------------------------------------------------------------
# Produtos
# ---------------------------------------------------------------------------

def get_or_create_produto(cur, nome_produto: str) -> Optional[int]:
    """
    Busca produto pelo nome (normalizado). Se nao existir, cria.
    produtos.nome NAO tem UNIQUE no banco real -> checagem manual.
    """
    if not nome_produto:
        return None

    nome_norm = normalizar_texto(nome_produto)

    cur.execute("SELECT id, nome FROM produtos")
    candidatos = cur.fetchall()
    for p in candidatos:
        nome_p = p['nome'] if isinstance(p, dict) else p[1]
        if normalizar_texto(nome_p) == nome_norm:
            return p['id'] if isinstance(p, dict) else p[0]

    cur.execute(
        """
        INSERT INTO produtos (nome, unidade_medida)
        VALUES (%s, 'kg')
        RETURNING id
        """,
        (nome_produto,)
    )
    row = cur.fetchone()
    novo_id = row['id'] if isinstance(row, dict) else row[0]
    logger.info("Produto criado: id=%s nome=%s", novo_id, nome_produto)
    return novo_id


# ---------------------------------------------------------------------------
# Notas fiscais (vendas para prefeituras) - sem chave_acesso
# ---------------------------------------------------------------------------

def get_or_create_nota_fiscal(cur, registro: Dict[str, Any], cliente_id: int) -> Optional[int]:
    """
    Cria (ou recupera, se ja existir) a nota fiscal de venda.
    numero_nota E' UNIQUE no schema real, entao podemos usar
    ON CONFLICT (numero_nota) com seguranca aqui.
    NUNCA gravamos chave_acesso - a coluna nao existe.
    """
    numero_nota = registro.get('numero_nota') or ''
    if not numero_nota:
        logger.warning("Registro de venda sem numero_nota - pulado. produto=%s", registro.get('produto'))
        return None

    cur.execute(
        """
        INSERT INTO notas_fiscais
            (numero_nota, data_emissao, cliente_id, valor_total, status)
        VALUES (%s, %s, %s, %s, 'emitida')
        ON CONFLICT (numero_nota) DO UPDATE
            SET valor_total = notas_fiscais.valor_total  -- no-op, so' pra permitir RETURNING
        RETURNING id
        """,
        (
            numero_nota,
            registro.get('data_emissao') or None,
            cliente_id,
            registro.get('valor_total') or 0,
        )
    )
    row = cur.fetchone()
    return row['id'] if isinstance(row, dict) else row[0]


def salvar_item_nota(cur, nota_id: int, produto_id: int, registro: Dict[str, Any]) -> None:
    cur.execute(
        """
        INSERT INTO nota_itens (nota_id, produto_id, quantidade, valor_unitario, valor_total)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            nota_id,
            produto_id,
            registro.get('quantidade') or 0,
            registro.get('valor_unitario') or 0,
            registro.get('valor_total') or 0,
        )
    )


# ---------------------------------------------------------------------------
# Pagamentos a fornecedores (compras) - sem chave_acesso, sem UNIQUE em cpf_cnpj
# ---------------------------------------------------------------------------

def salvar_pagamento_fornecedor(cur, fornecedor_id: int, registro: Dict[str, Any], nota_id: Optional[int] = None) -> int:
    cur.execute(
        """
        INSERT INTO pagamentos_fornecedores
            (fornecedor_id, nota_id, data_emissao, produto_descricao,
             numero_nota_fornecedor, valor_compra, funrural, valor_liquido, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pendente')
        RETURNING id
        """,
        (
            fornecedor_id,
            nota_id,
            registro.get('data_emissao') or None,
            registro.get('produto'),
            registro.get('numero_nota'),
            registro.get('valor_total') or 0,
            registro.get('funrural_valor') or 0,
            registro.get('valor_liquido_nota') or (registro.get('valor_total') or 0),
        )
    )
    row = cur.fetchone()
    return row['id'] if isinstance(row, dict) else row[0]


# ---------------------------------------------------------------------------
# Orquestracao: recebe o dict retornado por reconciliar_notas()
# ---------------------------------------------------------------------------

def persistir_resultado_reconciliacao(conn, resultado: Dict[str, Any]) -> Dict[str, Any]:
    """
    resultado = processador_danfe.reconciliar_notas(notas_extraidas)
    Formato esperado: {'compras': [...], 'vendas': [...], 'total_registros': N}

    Retorna um resumo com quantos registros foram salvos e quais
    falharam (por falta de cliente/produto/fornecedor identificavel),
    para exibir na tela de importacao.
    """
    salvos = {'notas_fiscais': 0, 'nota_itens': 0, 'pagamentos_fornecedores': 0}
    erros: List[str] = []

    cur = conn.cursor()
    try:
        # --- VENDAS -> notas_fiscais + nota_itens ---
        for registro in resultado.get('vendas', []):
            cliente_id = find_cliente_by_nome(cur, registro.get('contraparte_nome') or registro.get('cliente_destino_nome'))
            if not cliente_id:
                erros.append(
                    f"Venda nota {registro.get('numero_nota')}: cliente/prefeitura "
                    f"'{registro.get('contraparte_nome')}' nao encontrado - registro nao salvo."
                )
                continue

            produto_id = get_or_create_produto(cur, registro.get('produto'))
            if not produto_id:
                erros.append(f"Venda nota {registro.get('numero_nota')}: produto sem nome - item pulado.")
                continue

            nota_id = get_or_create_nota_fiscal(cur, registro, cliente_id)
            if not nota_id:
                continue

            salvar_item_nota(cur, nota_id, produto_id, registro)
            salvos['notas_fiscais'] += 1
            salvos['nota_itens'] += 1

        # --- COMPRAS -> pagamentos_fornecedores ---
        for registro in resultado.get('compras', []):
            fornecedor_id = get_or_create_fornecedor(
                cur,
                registro.get('emitente_doc'),
                registro.get('contraparte_nome')
            )
            if not fornecedor_id:
                erros.append(
                    f"Compra nota {registro.get('numero_nota')}: fornecedor sem CPF/CNPJ identificavel - registro nao salvo."
                )
                continue

            salvar_pagamento_fornecedor(cur, fornecedor_id, registro)
            salvos['pagamentos_fornecedores'] += 1

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()

    return {'salvos': salvos, 'erros': erros}