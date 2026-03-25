# processador_coaipro.py
"""
Processador de planilhas Excel da COOAIPRO para banco de dados SQLite
Uso: python processador_coaipro.py planilha_entrega.xlsx [planilha_receber.xlsx]
Na primeira execução do app, as planilhas em planilhas_modelo/ são conciliadas e migradas para o BD.
"""

import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime
import re
import sys
import os
from pathlib import Path

# Pasta onde ficam as planilhas modelo (primeira execução)
PASTA_PLANILHAS_MODELO = 'planilhas_modelo'


def _dir_base():
    """Diretório base do projeto (onde está schema.sql)"""
    return Path(__file__).resolve().parent


def is_primeira_execucao(db_path='coaipro.db'):
    """
    Retorna True se for primeira execução: BD não existe ou está vazio
    (nenhuma nota fiscal nem pagamento carregado).
    """
    path = Path(db_path)
    if not path.exists():
        return True
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM notas_fiscais")
        n_notas = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM pagamentos_fornecedores")
        n_pag = cur.fetchone()[0]
        conn.close()
        return n_notas == 0 and n_pag == 0
    except sqlite3.OperationalError:
        return True


def garantir_banco_criado(db_path='coaipro.db'):
    """Cria o BD e aplica o schema (tabelas/views). Sempre aplica o schema para garantir que as tabelas existam."""
    p = CoaiproProcessor(db_path)
    p.close()


def carregar_planilhas_modelo(db_path='coaipro.db', pasta=None):
    """
    Concilia e carrega dados das planilhas modelo na primeira execução.
    Procura .xlsx em pasta (default: planilhas_modelo/). Migra para o BD local.
    Retorna True se a carga foi executada, False se não havia planilhas ou deu erro.
    """
    garantir_banco_criado(db_path)
    pasta = Path(pasta or PASTA_PLANILHAS_MODELO)
    if not pasta.is_absolute():
        pasta = _dir_base() / pasta
    if not pasta.exists():
        return False
    xlsx = sorted(pasta.glob('*.xlsx'))
    if not xlsx:
        return False
    try:
        processor = CoaiproProcessor(db_path)
        # Um arquivo pode ter as duas abas; dois arquivos: primeiro = a pagar, segundo = a receber
        processor.processar_planilha_entrega(str(xlsx[0]))
        if len(xlsx) >= 2:
            processor.processar_planilha_receber(str(xlsx[1]))
        else:
            processor.processar_planilha_receber(str(xlsx[0]))
        processor.gerar_relatorios()
        processor.close()
        return True
    except Exception:
        return False


class CoaiproProcessor:
    def __init__(self, db_path='coaipro.db'):
        """Inicializa o processador com conexão ao banco de dados"""
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()
        self.setup_database()
    
    def setup_database(self):
        """Cria as tabelas se não existirem"""
        schema_path = _dir_base() / 'schema.sql'
        with open(schema_path, 'r', encoding='utf-8') as f:
            self.conn.executescript(f.read())
        self.conn.commit()
    
    def processar_planilha_entrega(self, excel_path):
        """Processa a planilha de entregas (a pagar)"""
        print(f"📥 Processando planilha de entregas: {excel_path}")
        
        # Carregar a aba 'À Pagar - 2025'
        df = pd.read_excel(excel_path, sheet_name='À Pagar - 2025', header=5)
        
        # Limpar e filtrar dados
        df = df[df['FORNECEDOR'].notna()].copy()
        
        for idx, row in df.iterrows():
            try:
                # Extrair fornecedor
                fornecedor_nome = str(row['FORNECEDOR']).strip()
                if not fornecedor_nome or fornecedor_nome == 'nan':
                    continue
                
                # Inserir/obter fornecedor
                self.cursor.execute(
                    "INSERT OR IGNORE INTO fornecedores (nome) VALUES (?)",
                    (fornecedor_nome,)
                )
                self.cursor.execute(
                    "SELECT id FROM fornecedores WHERE nome = ?",
                    (fornecedor_nome,)
                )
                fornecedor_id = self.cursor.fetchone()[0]
                
                # Dados básicos
                data_emissao = self._parse_date(row.get('2025-01-20 00:00:00', None))
                if not data_emissao:
                    continue
                
                prefeitura = str(row.get('Prefeitura', ''))
                produto = str(row.get('PRODUTO', ''))
                numero_nota = str(row.get('Nº NOTA', ''))
                
                # Peso líquido - pode ser string com soma (ex: "=15+3+6")
                peso_str = str(row.get('PESO LÍQUIDO', '0'))
                peso_liquido = self._parse_expression(peso_str)
                
                # Valor da compra
                valor_compra = self._parse_float(row.get('Compra                    VR$ TOTAL', 0))
                
                # Descontos
                funrural = self._parse_float(row.get('Funrural 1,5 %', 0))
                taxa_coop = self._parse_float(row.get('Taxa Cooperativa 15,5%', 0))
                
                # Valor líquido
                valor_liquido = self._parse_float(row.get('Valor Liquido', 0))
                if valor_liquido == 0 and valor_compra > 0:
                    # Calcular aproximado se não tiver direto
                    valor_liquido = valor_compra - funrural - taxa_coop
                
                status = 'pago' if 'Pago' in str(row.get('SINTUAÇÃO', '')) else 'pendente'
                
                # Inserir pagamento
                self.cursor.execute("""
                    INSERT INTO pagamentos_fornecedores (
                        fornecedor_id, data_emissao, prefeitura, produto_descricao,
                        numero_nota_fornecedor, peso_liquido, valor_compra,
                        funrural, taxa_cooperativa, valor_liquido, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    fornecedor_id, data_emissao, prefeitura, produto,
                    numero_nota, peso_liquido, valor_compra,
                    funrural, taxa_coop, valor_liquido, status
                ))
                
            except Exception as e:
                print(f"  ⚠️ Erro na linha {idx}: {e}")
                continue
        
        self.conn.commit()
        print(f"✅ {df.shape[0]} registros processados")
    
    def processar_planilha_receber(self, excel_path):
        """Processa a planilha 'À Receber' (notas fiscais das prefeituras)"""
        print(f"📥 Processando planilha a receber: {excel_path}")
        
        df = pd.read_excel(excel_path, sheet_name='À Receber- 2025', header=4)
        df = df[df['Nº NOTA'].notna()].copy()
        
        # Criar clientes (prefeituras) padrão
        prefeituras = [
            'Pref Municipal de Santa Isabel',
            'Pref Municipal de Igarata',
            'Pref Municipal de São Jose dos Campos',
            'Pref Municipal de São Bernardo',
            'Pref Municipal de ferraz de Vasconcelo',
            'Pref Municipal de Guararema',
            'Pref Municipal de Itaqua',
            'Pref Municipal de  Poa',
            'Conab - Iguatemi',
            'Conab - Perus',
            'Conab - Montanhão',
            'Conab -Estadio'
        ]
        
        for pref in prefeituras:
            self.cursor.execute(
                "INSERT OR IGNORE INTO clientes (nome, tipo) VALUES (?, ?)",
                (pref, 'conab' if 'Conab' in pref else 'prefeitura')
            )
        
        for idx, row in df.iterrows():
            try:
                numero_nota = str(row.get('Nº NOTA', '')).strip()
                if not numero_nota or numero_nota == 'nan':
                    continue
                
                cliente_nome = str(row.get('CLIENTE', ''))
                if not cliente_nome or cliente_nome == 'nan':
                    continue
                
                # Buscar ID do cliente
                self.cursor.execute(
                    "SELECT id FROM clientes WHERE nome LIKE ?",
                    (f'%{cliente_nome}%',)
                )
                result = self.cursor.fetchone()
                if not result:
                    print(f"  ⚠️ Cliente não encontrado: {cliente_nome}")
                    continue
                
                cliente_id = result[0]
                
                # Dados da nota
                data_emissao = self._parse_date(row.get('DATA NF', None))
                peso_str = str(row.get('PESO LÍQUIDO', '0'))
                peso_liquido = self._parse_expression(peso_str)
                
                valor_total = self._parse_float(row.get('Compra                    VR$ TOTAL', 0))
                valor_frete = self._parse_float(row.get('Valor Frete', 0))
                
                previsao = self._parse_date(row.get('previsão de pagamento', None))
                data_pagamento = self._parse_date(row.get('DATA', None))
                
                status_raw = str(row.get('SINTUAÇÃO', '')).strip().lower()
                if 'pago' in status_raw:
                    status = 'paga'
                elif 'cancelada' in status_raw:
                    status = 'cancelada'
                else:
                    status = 'emitida'
                
                # Inserir nota fiscal
                self.cursor.execute("""
                    INSERT OR REPLACE INTO notas_fiscais (
                        numero_nota, data_emissao, cliente_id, peso_liquido,
                        valor_total, valor_frete, previsao_pagamento,
                        data_pagamento, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    numero_nota, data_emissao, cliente_id, peso_liquido,
                    valor_total, valor_frete, previsao, data_pagamento, status
                ))
                
            except Exception as e:
                print(f"  ⚠️ Erro na linha {idx}: {e}")
                continue
        
        self.conn.commit()
        print(f"✅ {df.shape[0]} notas processadas")
    
    def _parse_date(self, value):
        """Converte vários formatos de data para string ISO"""
        if pd.isna(value) or not value:
            return None
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, pd.Timestamp):
            return value.date().isoformat()
        try:
            # Tenta converter string
            if isinstance(value, str):
                # Formato Excel com timestamp
                if '00:00:00' in value:
                    value = value.split()[0]
                return datetime.strptime(value, '%Y-%m-%d').date().isoformat()
        except:
            pass
        return None
    
    def _parse_float(self, value):
        """Converte para float com segurança"""
        try:
            if pd.isna(value):
                return 0.0
            return float(value)
        except:
            return 0.0
    
    def _parse_expression(self, expr):
        """Avalia expressões simples como '=15+3+6'"""
        if not isinstance(expr, str):
            return self._parse_float(expr)
        
        expr = expr.strip()
        if expr.startswith('='):
            expr = expr[1:]
        
        # Remove espaços e avalia soma simples
        expr = expr.replace(' ', '')
        if re.match(r'^[\d\+\-]+$', expr):
            try:
                return eval(expr)
            except:
                pass
        
        # Fallback: tenta converter para float
        return self._parse_float(expr)
    
    def gerar_relatorios(self):
        """Gera relatórios agregados"""
        print("\n📊 GERANDO RELATÓRIOS")
        
        # Resumo por prefeitura
        df_resumo = pd.read_sql_query("""
            SELECT * FROM vw_resultados_por_prefeitura
            ORDER BY valor_total_contratos DESC
        """, self.conn)
        
        print("\n🏛️  CONTRATOS POR PREFEITURA:")
        print(df_resumo.to_string(index=False))
        
        # Salvar CSV
        df_resumo.to_csv('relatorio_prefeituras.csv', index=False)
        print("\n✅ Relatório salvo em 'relatorio_prefeituras.csv'")
        
        # Resumo de pagamentos a fornecedores
        df_pagamentos = pd.read_sql_query("""
            SELECT * FROM vw_pagamentos_resumo
            ORDER BY total_bruto DESC
            LIMIT 20
        """, self.conn)
        
        print("\n👨‍🌾 TOP 20 FORNECEDORES:")
        print(df_pagamentos.to_string(index=False))
        
        df_pagamentos.to_csv('relatorio_fornecedores.csv', index=False)
        
        # Totais gerais
        df_totais = pd.read_sql_query("""
            SELECT 
                (SELECT SUM(valor_liquido) FROM pagamentos_fornecedores WHERE status = 'pendente') as total_a_pagar,
                (SELECT SUM(valor_total) FROM notas_fiscais WHERE status = 'pendente') as total_a_receber,
                (SELECT SUM(valor_liquido) FROM pagamentos_fornecedores WHERE status = 'pago') as total_pago,
                (SELECT SUM(valor_total) FROM notas_fiscais WHERE status = 'paga') as total_recebido
        """, self.conn)
        
        print("\n💰 TOTAIS GERAIS:")
        print(df_totais.to_string(index=False))
    
    def close(self):
        self.conn.close()

def main():
    if len(sys.argv) < 2:
        print("Uso: python processador_coaipro.py <planilha_entrega.xlsx> [planilha_receber.xlsx]")
        sys.exit(1)
    
    processor = CoaiproProcessor()
    
    # Processar primeira planilha (entregas/a pagar)
    processor.processar_planilha_entrega(sys.argv[1])
    
    # Processar segunda planilha (a receber) se fornecida
    if len(sys.argv) > 2:
        processor.processar_planilha_receber(sys.argv[2])
    
    # Gerar relatórios
    processor.gerar_relatorios()
    processor.close()

if __name__ == "__main__":
    main()