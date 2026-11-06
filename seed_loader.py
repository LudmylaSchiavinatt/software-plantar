import psycopg2
import pandas as pd
from pathlib import Path


def _caminho_seed(pasta=None):
    if pasta:
        return Path(pasta)
    # pasta padrão: dados_seed dentro do projeto
    base = Path(__file__).parent
    return base / "dados_seed"


def _carregar_df(pasta, nome):
    """
    Tenta carregar um arquivo (csv ou xlsx) pelo nome dentro da pasta.
    Ex: fornecedores.xlsx, clientes.csv, etc.
    """
    # tenta xlsx
    caminho_xlsx = pasta / f"{nome}.xlsx"
    if caminho_xlsx.exists():
        return pd.read_excel(caminho_xlsx)

    # tenta csv
    caminho_csv = pasta / f"{nome}.csv"
    if caminho_csv.exists():
        return pd.read_csv(caminho_csv)

    print(f"⚠️ Arquivo não encontrado: {nome}")
    return None


def _parse_float(valor):
    """Converte valores da planilha para float de forma segura."""
    if pd.isna(valor) or valor == "":
        return 0.0
    try:
        if isinstance(valor, str):
            valor = valor.replace(',', '.')
        return float(valor)
    except (ValueError, TypeError):
        return 0.0


def _parse_date(valor):
    """Converte valores da planilha para o formato de data do Postgres (YYYY-MM-DD)."""
    if pd.isna(valor) or valor == "":
        return None
    try:
        return pd.to_datetime(valor).strftime('%Y-%m-%d')
    except Exception:
        return None


def _parse_bool(valor, default=True):
    """Converte valores tipo 1/0/'true'/'false' para bool nativo do Postgres."""
    if valor is None:
        return default
    return str(valor).strip().lower() not in ('0', 'false', 'nan', '')


def migrar_seed_tabelas(db_config=None, pasta=None):
    """
    db_config: dict com host/port/dbname/user/password (mesmo formato usado
    em app.py / processador_coaipro.py). Mantido o nome do parâmetro antigo
    (era db_path) só que agora recebe um dict, não um caminho de arquivo.
    """
    pasta = _caminho_seed(pasta)

    if not pasta.exists():
        print("❌ Pasta não encontrada:", pasta)
        return False

    conn = psycopg2.connect(**db_config) if db_config else psycopg2.connect()
    cur = conn.cursor()
    carregou = False

    try:
        # ================================
        # 1. 📦 FORNECEDORES (Tabela Base)
        # ================================
        df = _carregar_df(pasta, 'fornecedores')
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                nome = str(row.get('nome') or "").strip()
                if not nome or nome.lower() == 'nan':
                    continue

                # fornecedores.nome não tem constraint UNIQUE no schema,
                # então checamos existência manualmente (equivalente ao
                # INSERT OR IGNORE do SQLite)
                cur.execute("SELECT id FROM fornecedores WHERE nome = %s", (nome,))
                if cur.fetchone():
                    continue

                cur.execute("""
                    INSERT INTO fornecedores (nome, cpf_cnpj, cidade, ativo)
                    VALUES (%s, %s, %s, %s)
                """, (
                    nome,
                    str(row.get('cpf_cnpj')) if pd.notna(row.get('cpf_cnpj')) else None,
                    str(row.get('cidade')) if pd.notna(row.get('cidade')) else None,
                    _parse_bool(row.get('ativo', 1)),
                ))
            conn.commit()
            print("✅ Fornecedores importados")
            carregou = True

        # ================================
        # 2. 👥 CLIENTES (Tabela Base)
        # ================================
        df = _carregar_df(pasta, 'clientes')
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                nome = str(row.get('nome') or "").strip()
                if not nome or nome.lower() == 'nan':
                    continue

                cur.execute("SELECT id FROM clientes WHERE nome = %s", (nome,))
                if cur.fetchone():
                    continue

                cur.execute("""
                    INSERT INTO clientes (
                        nome, tipo, cidade, prazo_pagamento_dias, ativo
                    ) VALUES (%s, %s, %s, %s, %s)
                """, (
                    nome,
                    str(row.get('tipo')) if pd.notna(row.get('tipo')) else 'prefeitura',
                    str(row.get('cidade')) if pd.notna(row.get('cidade')) else None,
                    int(_parse_float(row.get('prazo_pagamento_dias') or 30)),
                    _parse_bool(row.get('ativo', 1)),
                ))
            conn.commit()
            print("✅ Clientes importados")
            carregou = True

        # ================================
        # 3. 🔥 IMPORTAÇÃO DE PLANILHAS (Movimentações)
        # ================================
        for xlsx in pasta.glob("*.xlsx"):
            # Ignora os arquivos que já foram processados como tabelas base
            if xlsx.stem.lower() in ['fornecedores', 'clientes', 'produtos']:
                continue

            print(f"\n📂 Processando: {xlsx.name}")

            # ---------- À RECEBER ----------
            try:
                df = pd.read_excel(xlsx, sheet_name="À Receber - 2025")
                df = df.drop(columns=["id", "created_at"], errors="ignore")

                for _, row in df.iterrows():
                    numero_nota = str(row.get("numero_nota") or "").strip()

                    if not numero_nota or numero_nota.lower() == "nan":
                        continue

                    cur.execute("""
                        INSERT INTO notas_fiscais (
                            numero_nota, data_emissao, cliente_id,
                            peso_liquido, quantidade_cx,
                            valor_total, valor_frete,
                            previsao_pagamento, data_pagamento,
                            status, observacoes
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (numero_nota) DO UPDATE SET
                            data_emissao = EXCLUDED.data_emissao,
                            cliente_id = EXCLUDED.cliente_id,
                            peso_liquido = EXCLUDED.peso_liquido,
                            quantidade_cx = EXCLUDED.quantidade_cx,
                            valor_total = EXCLUDED.valor_total,
                            valor_frete = EXCLUDED.valor_frete,
                            previsao_pagamento = EXCLUDED.previsao_pagamento,
                            data_pagamento = EXCLUDED.data_pagamento,
                            status = EXCLUDED.status,
                            observacoes = EXCLUDED.observacoes
                    """, (
                        numero_nota,
                        _parse_date(row.get("data_emissao")),
                        int(row.get("cliente_id")) if pd.notna(row.get("cliente_id")) else None,
                        _parse_float(row.get("peso_liquido")),
                        int(_parse_float(row.get("quantidade_cx") or 0)),
                        _parse_float(row.get("valor_total")),
                        _parse_float(row.get("valor_frete")),
                        _parse_date(row.get("previsao_pagamento")),
                        _parse_date(row.get("data_pagamento")),
                        str(row.get("status")) if pd.notna(row.get("status")) else "emitida",
                        str(row.get("observacoes")) if pd.notna(row.get("observacoes")) else None,
                    ))
                conn.commit()
                print("✅ À Receber importado")
                carregou = True

            except Exception as e:
                conn.rollback()
                if "No sheet named" not in str(e) and "does not exist" not in str(e):
                    print("❌ ERRO À RECEBER:", e)

            # ---------- À PAGAR ----------
            try:
                df = pd.read_excel(xlsx, sheet_name="À Pagar - 2025")
                df = df.drop(columns=["id", "created_at"], errors="ignore")

                for _, row in df.iterrows():
                    fornecedor_id = (
                        int(row.get("fornecedor_id"))
                        if pd.notna(row.get("fornecedor_id"))
                        else None
                    )

                    if fornecedor_id is None:
                        continue

                    valor_compra = _parse_float(row.get("valor_total"))
                    valor_liquido = valor_compra

                    cur.execute("""
                        INSERT INTO pagamentos_fornecedores (
                            fornecedor_id, nota_id, data_emissao,
                            valor_compra, valor_liquido, status
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                    """, (
                        fornecedor_id,
                        None,  # ainda sem vínculo com nota
                        _parse_date(row.get("data_emissao")),
                        valor_compra,
                        valor_liquido,
                        str(row.get("status")) if pd.notna(row.get("status")) else "pendente",
                    ))
                conn.commit()
                print("✅ À Pagar importado")
                carregou = True

            except Exception as e:
                conn.rollback()
                if "No sheet named" not in str(e) and "does not exist" not in str(e):
                    print("❌ ERRO À PAGAR:", e)

    finally:
        cur.close()
        conn.close()

    return carregou