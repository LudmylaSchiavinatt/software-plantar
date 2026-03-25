"""Fixtures compartilhadas para todos os testes."""
import os
import sys
import pytest
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'test_coaipro.db')


@pytest.fixture(autouse=True)
def limpar_bd_teste():
    """Remove o BD de teste antes e depois de cada teste."""
    if os.path.exists(TEST_DB):
        try:
            os.remove(TEST_DB)
        except PermissionError:
            pass
    yield
    if os.path.exists(TEST_DB):
        try:
            os.remove(TEST_DB)
        except PermissionError:
            pass


@pytest.fixture
def db_conn():
    """Cria BD de teste com schema aplicado."""
    from processador_coaipro import CoaiproProcessor
    p = CoaiproProcessor(TEST_DB)
    p.close()
    conn = sqlite3.connect(TEST_DB)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def db_com_dados(db_conn):
    """BD com dados seed mínimos para testes."""
    cur = db_conn.cursor()
    cur.execute("INSERT INTO clientes (nome, tipo, cidade) VALUES ('Pref Santa Isabel', 'prefeitura', 'Santa Isabel')")
    cur.execute("INSERT INTO clientes (nome, tipo, cidade) VALUES ('Conab Iguatemi', 'conab', 'SP')")
    cur.execute("INSERT INTO fornecedores (nome, cidade) VALUES ('José Silva', 'Santa Isabel')")
    cur.execute("INSERT INTO fornecedores (nome, cidade) VALUES ('Maria Santos', 'Igaratá')")
    cur.execute("INSERT INTO produtos (nome, variedade, unidade_medida, preco_referencia) VALUES ('Alface', 'Crespa', 'un', 2.50)")
    cur.execute("""INSERT INTO notas_fiscais (numero_nota, data_emissao, cliente_id, peso_liquido, valor_total, valor_frete, status)
                   VALUES ('NF-TEST-001', '2025-01-15', 1, 500, 10000, 200, 'pendente')""")
    cur.execute("""INSERT INTO notas_fiscais (numero_nota, data_emissao, cliente_id, peso_liquido, valor_total, valor_frete, status)
                   VALUES ('NF-TEST-002', '2025-02-10', 1, 300, 6000, 150, 'paga')""")
    cur.execute("""INSERT INTO notas_fiscais (numero_nota, data_emissao, cliente_id, peso_liquido, valor_total, valor_frete, status)
                   VALUES ('NF-TEST-003', '2025-03-05', 2, 800, 15000, 350, 'pendente')""")
    cur.execute("""INSERT INTO pagamentos_fornecedores
                   (fornecedor_id, nota_id, data_emissao, produto_descricao, valor_compra, funrural, taxa_cooperativa, valor_liquido, status)
                   VALUES (1, 1, '2025-01-15', 'Alface Crespa', 3000, 45, 465, 2490, 'pendente')""")
    cur.execute("""INSERT INTO pagamentos_fornecedores
                   (fornecedor_id, nota_id, data_emissao, produto_descricao, valor_compra, funrural, taxa_cooperativa, valor_liquido, status)
                   VALUES (2, 1, '2025-01-15', 'Cenoura', 2000, 30, 310, 1660, 'pago')""")
    db_conn.commit()
    return db_conn


@pytest.fixture
def app_client(db_com_dados):
    """Client Flask para testes de API, com BD de teste populado."""
    import app as flask_app
    flask_app.DATABASE = TEST_DB
    flask_app.app.config['TESTING'] = True
    with flask_app.app.test_client() as client:
        yield client


@pytest.fixture
def app_client_com_dados(app_client):
    """Alias para compatibilidade."""
    return app_client
