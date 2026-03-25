"""Testes da migração de dados seed."""
import os
import sqlite3


class TestSeedLoader:
    """Testa a carga de CSVs seed para o BD."""

    def test_migrar_seed_carrega_clientes(self, db_conn):
        from seed_loader import migrar_seed_tabelas
        result = migrar_seed_tabelas('test_coaipro.db')
        assert result is True
        cur = db_conn.execute("SELECT COUNT(*) FROM clientes")
        assert cur.fetchone()[0] >= 12

    def test_migrar_seed_carrega_fornecedores(self, db_conn):
        from seed_loader import migrar_seed_tabelas
        migrar_seed_tabelas('test_coaipro.db')
        cur = db_conn.execute("SELECT COUNT(*) FROM fornecedores")
        assert cur.fetchone()[0] >= 15

    def test_migrar_seed_carrega_produtos(self, db_conn):
        from seed_loader import migrar_seed_tabelas
        migrar_seed_tabelas('test_coaipro.db')
        cur = db_conn.execute("SELECT COUNT(*) FROM produtos")
        assert cur.fetchone()[0] >= 20

    def test_migrar_seed_carrega_notas(self, db_conn):
        from seed_loader import migrar_seed_tabelas
        migrar_seed_tabelas('test_coaipro.db')
        cur = db_conn.execute("SELECT COUNT(*) FROM notas_fiscais")
        assert cur.fetchone()[0] >= 25

    def test_migrar_seed_carrega_pagamentos(self, db_conn):
        from seed_loader import migrar_seed_tabelas
        migrar_seed_tabelas('test_coaipro.db')
        cur = db_conn.execute("SELECT COUNT(*) FROM pagamentos_fornecedores")
        assert cur.fetchone()[0] >= 30

    def test_pasta_inexistente_retorna_false(self, db_conn):
        from seed_loader import migrar_seed_tabelas
        result = migrar_seed_tabelas('test_coaipro.db', pasta='pasta_que_nao_existe')
        assert result is False


class TestPrimeiraExecucao:
    """Testa detecção de primeira execução."""

    def test_bd_inexistente_e_primeira(self):
        from processador_coaipro import is_primeira_execucao
        assert is_primeira_execucao('nao_existe.db') is True

    def test_bd_vazio_e_primeira(self, db_conn):
        from processador_coaipro import is_primeira_execucao
        assert is_primeira_execucao('test_coaipro.db') is True

    def test_bd_com_dados_nao_e_primeira(self, db_com_dados):
        from processador_coaipro import is_primeira_execucao
        assert is_primeira_execucao('test_coaipro.db') is False
