"""Testes das views SQL (lógica de negócio no banco)."""
import sqlite3


class TestViewCustosPorNota:
    """Testa vw_custos_por_nota."""

    def test_custos_agregados_corretamente(self, db_com_dados):
        cur = db_com_dados.execute("SELECT * FROM vw_custos_por_nota WHERE nota_id = 1")
        row = cur.fetchone()
        assert row is not None
        assert row['total_custos'] == 2
        assert row['total_valor_compra'] == 5000.0  # 3000 + 2000
        assert row['total_liquido_fornecedores'] == 4150.0  # 2490 + 1660

    def test_nota_sem_custos_nao_aparece(self, db_com_dados):
        cur = db_com_dados.execute("SELECT * FROM vw_custos_por_nota WHERE nota_id = 3")
        assert cur.fetchone() is None


class TestViewResultadosPrefeitura:
    """Testa vw_resultados_por_prefeitura com custos."""

    def test_prefeitura_contratos_e_custos(self, db_com_dados):
        cur = db_com_dados.execute("SELECT * FROM vw_resultados_por_prefeitura WHERE prefeitura = 'Pref Santa Isabel'")
        row = cur.fetchone()
        assert row is not None
        assert row['total_notas'] == 2
        assert row['valor_total_contratos'] == 16000.0
        assert row['valor_recebido'] == 6000.0  # NF-TEST-002 paga
        assert row['total_custos'] == 4150.0
        assert row['resultado_liquido'] == 11850.0  # 16000 - 4150

    def test_conab_sem_custos(self, db_com_dados):
        cur = db_com_dados.execute("SELECT * FROM vw_resultados_por_prefeitura WHERE prefeitura = 'Conab Iguatemi'")
        row = cur.fetchone()
        assert row is not None
        assert row['total_custos'] == 0
        assert row['resultado_liquido'] == 15000.0


class TestViewPagamentosResumo:
    """Testa vw_pagamentos_resumo."""

    def test_resumo_fornecedor_jose(self, db_com_dados):
        cur = db_com_dados.execute("SELECT * FROM vw_pagamentos_resumo WHERE fornecedor = 'José Silva'")
        row = cur.fetchone()
        assert row is not None
        assert row['total_pagamentos'] == 1
        assert row['total_bruto'] == 3000.0
        assert row['total_liquido'] == 2490.0

    def test_resumo_fornecedor_maria(self, db_com_dados):
        cur = db_com_dados.execute("SELECT * FROM vw_pagamentos_resumo WHERE fornecedor = 'Maria Santos'")
        row = cur.fetchone()
        assert row is not None
        assert row['total_bruto'] == 2000.0
        assert row['total_a_pagar'] == 0  # status = pago
