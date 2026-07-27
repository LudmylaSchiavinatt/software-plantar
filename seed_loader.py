import sqlite3
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
    """Converte valores da planilha para o formato de data do SQLite (YYYY-MM-DD)."""
    if pd.isna(valor) or valor == "":
        return None
    try:
        return pd.to_datetime(valor).strftime('%Y-%m-%d')
    except Exception:
        return None

def migrar_seed_tabelas(db_path='coaipro.db', pasta=None):
    pasta = _caminho_seed(pasta)

    if not pasta.exists():
        print("❌ Pasta não encontrada:", pasta)
        return False

    conn = sqlite3.connect(db_path)
    # Ativa chaves estrangeiras no SQLite (opcional, mas recomendado para integridade)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    carregou = False

    # ================================
    # 1. 📦 FORNECEDORES (Tabela Base)
    # ================================
    df = _carregar_df(pasta, 'fornecedores')
    if df is not None and not df.empty:
        for _, row in df.iterrows():
            nome = str(row.get('nome') or "").strip()
            if not nome or nome.lower() == 'nan':
                continue

            cur.execute("""
                INSERT OR IGNORE INTO fornecedores (nome, cpf_cnpj, cidade, ativo)
                VALUES (?, ?, ?, ?)
            """, (
                nome,
                str(row.get('cpf_cnpj') or None),
                str(row.get('cidade') or None),
                1 if str(row.get('ativo', 1)).lower() not in ('0', 'false') else 0,
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

            cur.execute("""
                INSERT OR IGNORE INTO clientes (
                    nome, tipo, cidade, prazo_pagamento_dias, ativo
                ) VALUES (?, ?, ?, ?, ?)
            """, (
                nome,
                str(row.get('tipo') or 'prefeitura'),
                str(row.get('cidade') or None),
                int(_parse_float(row.get('prazo_pagamento_dias') or 30)),
                1 if str(row.get('ativo', 1)).lower() not in ('0', 'false') else 0,
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
                    INSERT OR REPLACE INTO notas_fiscais (
                        numero_nota, data_emissao, cliente_id,
                        peso_liquido, quantidade_cx,
                        valor_total, valor_frete,
                        previsao_pagamento, data_pagamento,
                        status, observacoes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    str(row.get("status") or "emitida"),
                    str(row.get("observacoes") or None),
                ))
            conn.commit()
            print("✅ À Receber importado")
            carregou = True

        except Exception as e:
            if "No sheet named" not in str(e):
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

                valor_compra = _parse_float(row.get("valor_total"))
                valor_liquido = valor_compra

                cur.execute("""
                    INSERT INTO pagamentos_fornecedores (
                        fornecedor_id, nota_id, data_emissao,
                        valor_compra, valor_liquido, status
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    fornecedor_id,
                    None,  # ainda sem vínculo com nota
                    _parse_date(row.get("data_emissao")),
                    valor_compra,
                    valor_liquido,
                    str(row.get("status") or "pendente"),
                ))
            conn.commit()
            print("✅ À Pagar importado")
            carregou = True

        except Exception as e:
            if "No sheet named" not in str(e):
                print("❌ ERRO À PAGAR:", e)

    conn.close()
    return carregou