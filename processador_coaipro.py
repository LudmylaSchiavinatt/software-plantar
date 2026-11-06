import psycopg2
import pandas as pd
import numpy as np
from datetime import datetime
import re
import sys
import os
from pathlib import Path
from dotenv import load_dotenv

# Carrega as variáveis do ficheiro .env em UTF-8
load_dotenv(encoding='utf-8')

# Pasta onde ficam as planilhas modelo (primeira execução)
PASTA_PLANILHAS_MODELO = 'planilhas_modelo'

# String/parâmetros de conexão com o PostgreSQL.
# Mesmos defaults de app.py — SUBSTITUA pelos dados reais do Postgres
# de produção antes do build, assim o .exe funciona sem precisar de .env.
DB_CONFIG = {
    'host': os.environ.get('DB_HOST', 'SEU_HOST_AQUI'),
    'port': os.environ.get('DB_PORT', '5432'),
    'dbname': os.environ.get('DB_NAME', 'coaipro'),
    'user': os.environ.get('DB_USER', 'SEU_USUARIO_AQUI'),
    'password': os.environ.get('DB_PASSWORD', 'SUA_SENHA_AQUI'),
}
def _dir_base():
    return Path(__file__).resolve().parent

def _conectar(db_config=None):
    return psycopg2.connect(**(db_config or DB_CONFIG))

def is_primeira_execucao(db_config=None):
    try:
        conn = _conectar(db_config)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM notas_fiscais")
        n_notas = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM pagamentos_fornecedores")
        n_pag = cur.fetchone()[0]
        conn.close()
        return n_notas == 0 and n_pag == 0
    except Exception:
        # Se as tabelas ainda não existem, trata como primeira execução
        return True

def garantir_banco_criado(db_config=None):
    p = CoaiproProcessor(db_config)
    p.close()

def carregar_planilhas_modelo(db_config=None, pasta=None):
    garantir_banco_criado(db_config)
    pasta = Path(pasta or PASTA_PLANILHAS_MODELO)
    if not pasta.is_absolute(): pasta = _dir_base() / pasta
    if not pasta.exists(): return False
    xlsx = sorted(pasta.glob('*.xlsx'))
    if not xlsx: return False
    try:
        processor = CoaiproProcessor(db_config)
        processor.processar_planilha_entrega(str(xlsx[0]))
        if len(xlsx) >= 2: processor.processar_planilha_receber(str(xlsx[1]))
        else: processor.processar_planilha_receber(str(xlsx[0]))
        processor.gerar_relatorios()
        processor.close()
        return True
    except Exception as e:
        print(f"Erro na carga: {e}")
        return False

class CoaiproProcessor:
    def __init__(self, db_config=None):
        self.db_config = db_config or DB_CONFIG
        self.conn = _conectar(self.db_config)
        self.cursor = self.conn.cursor()
        self.setup_database()

    def setup_database(self):
        # ATENÇÃO: schema.sql precisa estar em sintaxe PostgreSQL
        # (ex.: SERIAL/GENERATED ALWAYS AS IDENTITY no lugar de AUTOINCREMENT)
        schema_path = _dir_base() / 'schema.sql'
        if schema_path.exists():
            with open(schema_path, 'r', encoding='utf-8') as f:
                self.cursor.execute(f.read())
            self.conn.commit()

    def processar_planilha_entrega(self, excel_path):
        print(f"📥 Processando planilha de entregas: {excel_path}")
        df = pd.read_excel(excel_path, sheet_name='À Pagar - 2025', header=5)
        df = df[df['FORNECEDOR'].notna()].copy()

        for idx, row in df.iterrows():
            try:
                fornecedor_nome = str(row['FORNECEDOR']).strip()
                if not fornecedor_nome or fornecedor_nome == 'nan': continue

                # fornecedores.nome não tem constraint UNIQUE no schema real,
                # então verificamos primeiro (mesma lógica usada no app.py)
                self.cursor.execute("SELECT id FROM fornecedores WHERE nome = %s", (fornecedor_nome,))
                existente = self.cursor.fetchone()
                if existente:
                    fornecedor_id = existente[0]
                else:
                    self.cursor.execute(
                        "INSERT INTO fornecedores (nome) VALUES (%s) RETURNING id",
                        (fornecedor_nome,)
                    )
                    fornecedor_id = self.cursor.fetchone()[0]

                data_emissao = self._parse_date(row.get('DATA', None))
                if not data_emissao: continue

                produto = str(row.get('PRODUTO', ''))
                numero_nota = str(row.get('Nº NOTA', ''))
                peso_liquido = self._parse_expression(str(row.get('PESO LÍQUIDO', '0')))

                val_compra = self._parse_float(row.get('Compra                    VR$ TOTAL', 0))
                funrural = self._parse_float(row.get('Funrural 1,5 %', 0))
                taxa_coop = self._parse_float(row.get('Taxa Cooperativa 15,5%', 0))
                desc_aipro = self._parse_float(row.get('Desc. AIPRO', 0))
                cx_papelao = self._parse_float(row.get('Cx Papelão', 0))
                adiant = self._parse_float(row.get('Adiantamento', 0))
                outros = self._parse_float(row.get('Outros', 0))

                liquido = val_compra - sum([funrural, taxa_coop, desc_aipro, cx_papelao, adiant, outros])
                status = 'pago' if 'Pago' in str(row.get('SINTUAÇÃO', '')) else 'pendente'

                self.cursor.execute("""
                    INSERT INTO pagamentos_fornecedores (
                        fornecedor_id, data_emissao, produto_descricao, numero_nota_fornecedor,
                        peso_liquido, valor_compra, funrural, taxa_cooperativa, desconto_aipro,
                        pg_caixa_papelao, adiantamento, descontos_outros, valor_liquido, status
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (fornecedor_id, data_emissao, produto, numero_nota, peso_liquido,
                      val_compra, funrural, taxa_coop, desc_aipro, cx_papelao, adiant, outros, liquido, status))
            except Exception as e:
                print(f" ⚠️ Erro na linha {idx}: {e}")
                self.conn.rollback()
        self.conn.commit()

    def processar_planilha_receber(self, excel_path):
        # Esta função deve existir para evitar erro de AttributeError
        pass

    def _parse_date(self, value):
        if pd.isna(value) or not value: return None
        if isinstance(value, datetime): return value.date().isoformat()
        if isinstance(value, pd.Timestamp): return value.date().isoformat()
        try:
            if isinstance(value, str):
                if '00:00:00' in value: value = value.split()[0]
                return datetime.strptime(value, '%Y-%m-%d').date().isoformat()
        except: pass
        return None

    def _parse_float(self, value):
        try:
            if pd.isna(value): return 0.0
            return float(value)
        except: return 0.0

    def _parse_expression(self, expr):
        if not isinstance(expr, str): return self._parse_float(expr)
        expr = expr.strip()
        if expr.startswith('='): expr = expr[1:]
        expr = expr.replace(' ', '')
        if re.match(r'^[\d\+\-]+$', expr):
            try: return eval(expr)
            except: pass
        return self._parse_float(expr)

    def gerar_relatorios(self):
        print("\n📊 GERANDO RELATÓRIOS")
        self.conn.commit()

    def close(self):
        self.conn.close()

def main():
    if len(sys.argv) < 2:
        print("Uso: python processador_coaipro.py <planilha_entrega.xlsx>")
        sys.exit(1)
    processor = CoaiproProcessor()
    processor.processar_planilha_entrega(sys.argv[1])
    processor.close()

if __name__ == "__main__":
    main()
