"""Testes do schema SQL e integridade do banco de dados."""
import sqlite3


class TestSchemaCreation:
    """Verifica se todas as tabelas e views são criadas corretamente."""

    def test_tabela_fornecedores_existe(self, db_conn):
        cur = db_conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='fornecedores'")
        assert cur.fetchone() is not None

    def test_tabela_clientes_existe(self, db_conn):
        cur = db_conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='clientes'")
        assert cur.fetchone() is not None

    def test_tabela_produtos_existe(self, db_conn):
        cur = db_conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='produtos'")
        assert cur.fetchone() is not None

    def test_tabela_notas_fiscais_existe(self, db_conn):
        cur = db_conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='notas_fiscais'")
        assert cur.fetchone() is not None

    def test_tabela_nota_itens_existe(self, db_conn):
        cur = db_conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='nota_itens'")
        assert cur.fetchone() is not None

    def test_tabela_pagamentos_existe(self, db_conn):
        cur = db_conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='pagamentos_fornecedores'")
        assert cur.fetchone() is not None

    def test_view_custos_por_nota_existe(self, db_conn):
        cur = db_conn.execute("SELECT name FROM sqlite_master WHERE type='view' AND name='vw_custos_por_nota'")
        assert cur.fetchone() is not None

    def test_view_resultados_prefeitura_existe(self, db_conn):
        cur = db_conn.execute("SELECT name FROM sqlite_master WHERE type='view' AND name='vw_resultados_por_prefeitura'")
        assert cur.fetchone() is not None

    def test_view_pagamentos_resumo_existe(self, db_conn):
        cur = db_conn.execute("SELECT name FROM sqlite_master WHERE type='view' AND name='vw_pagamentos_resumo'")
        assert cur.fetchone() is not None

    def test_schema_idempotente(self, db_conn):
        """Executar o schema duas vezes não deve dar erro."""
        from processador_coaipro import CoaiproProcessor
        p = CoaiproProcessor('test_coaipro.db')
        p.close()
        cur = db_conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")
        assert cur.fetchone()[0] >= 6


class TestConstraints:
    """Testa constraints e integridade referencial."""

    def test_numero_nota_unico(self, db_conn):
        cur = db_conn.cursor()
        cur.execute("INSERT INTO clientes (nome) VALUES ('Teste')")
        cur.execute("INSERT INTO notas_fiscais (numero_nota, data_emissao, cliente_id, valor_total) VALUES ('NF-1', '2025-01-01', 1, 100)")
        db_conn.commit()
        try:
            cur.execute("INSERT INTO notas_fiscais (numero_nota, data_emissao, cliente_id, valor_total) VALUES ('NF-1', '2025-01-02', 1, 200)")
            db_conn.commit()
            assert False, "Deveria ter dado IntegrityError"
        except sqlite3.IntegrityError:
            pass

    def test_fornecedor_insert_sucesso(self, db_conn):
        cur = db_conn.cursor()
        cur.execute("INSERT INTO fornecedores (nome, cidade) VALUES ('Teste Fornecedor', 'Cidade')")
        db_conn.commit()
        cur.execute("SELECT nome FROM fornecedores WHERE nome = 'Teste Fornecedor'")
        assert cur.fetchone() is not None
