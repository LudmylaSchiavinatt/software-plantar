"""Testes dos endpoints da API Flask."""
import json


class TestPaginas:
    """Testa se as páginas HTML carregam."""

    def test_landing_retorna_200(self, app_client_com_dados):
        resp = app_client_com_dados.get('/')
        assert resp.status_code == 200
        assert b'PLANTAR' in resp.data

    def test_graficos_retorna_200(self, app_client_com_dados):
        resp = app_client_com_dados.get('/painel/graficos')
        assert resp.status_code == 200
        assert b'chart-prefeituras' in resp.data

    def test_tabelas_retorna_200(self, app_client_com_dados):
        resp = app_client_com_dados.get('/painel/tabelas')
        assert resp.status_code == 200
        assert b'tbl-pref' in resp.data

    def test_cadastros_retorna_200(self, app_client_com_dados):
        resp = app_client_com_dados.get('/painel/cadastros')
        assert resp.status_code == 200
        assert b'formNota' in resp.data


class TestAPIResumo:
    """Testa endpoint /api/resumo."""

    def test_resumo_retorna_json(self, app_client_com_dados):
        resp = app_client_com_dados.get('/api/resumo')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert 'totais' in data
        assert 'ultimas_notas' in data
        assert 'ultimos_pagamentos' in data

    def test_resumo_totais_corretos(self, app_client_com_dados):
        resp = app_client_com_dados.get('/api/resumo')
        data = json.loads(resp.data)
        # NF-TEST-001 (10000 pendente) + NF-TEST-003 (15000 pendente) = 25000
        assert data['totais']['valor_a_receber'] == 25000.0
        # 1 pagamento pendente (2490)
        assert data['totais']['valor_a_pagar'] == 2490.0
        assert data['totais']['notas_pendentes'] == 2


class TestAPIListas:
    """Testa endpoints de listas para dropdowns."""

    def test_clientes_lista(self, app_client_com_dados):
        resp = app_client_com_dados.get('/api/clientes_lista')
        data = json.loads(resp.data)
        assert len(data) == 2
        assert any(c['tipo'] == 'prefeitura' for c in data)

    def test_fornecedores_lista(self, app_client_com_dados):
        resp = app_client_com_dados.get('/api/fornecedores_lista')
        data = json.loads(resp.data)
        assert len(data) == 2

    def test_notas_lista(self, app_client_com_dados):
        resp = app_client_com_dados.get('/api/notas_lista')
        data = json.loads(resp.data)
        assert len(data) == 3


class TestAPINotas:
    """Testa CRUD de notas fiscais."""

    def test_listar_notas(self, app_client_com_dados):
        resp = app_client_com_dados.get('/api/notas')
        data = json.loads(resp.data)
        assert len(data) == 3
        assert all('cliente_nome' in n for n in data)
        assert all('total_custos' in n for n in data)
        assert all('valor_liquido_nota' in n for n in data)

    def test_nota_com_custos_tem_liquido_correto(self, app_client_com_dados):
        resp = app_client_com_dados.get('/api/notas')
        data = json.loads(resp.data)
        nf001 = next(n for n in data if n['numero_nota'] == 'NF-TEST-001')
        # custos = 2490 + 1660 = 4150; liquido = 10000 - 4150 = 5850
        assert nf001['total_custos'] == 4150.0
        assert nf001['valor_liquido_nota'] == 5850.0

    def test_criar_nota_sucesso(self, app_client_com_dados):
        resp = app_client_com_dados.post('/api/notas',
            data=json.dumps({
                'numero_nota': 'NF-NEW-001',
                'data_emissao': '2025-04-01',
                'cliente_id': 1,
                'valor_total': 5000,
                'peso_liquido': 200,
                'status': 'emitida',
            }),
            content_type='application/json')
        assert resp.status_code == 201
        data = json.loads(resp.data)
        assert data['id'] > 0
        assert 'sucesso' in data['mensagem']

    def test_criar_nota_duplicada_erro(self, app_client_com_dados):
        resp = app_client_com_dados.post('/api/notas',
            data=json.dumps({
                'numero_nota': 'NF-TEST-001',
                'data_emissao': '2025-04-01',
                'cliente_id': 1,
                'valor_total': 5000,
            }),
            content_type='application/json')
        assert resp.status_code == 400

    def test_criar_nota_sem_campos_obrigatorios(self, app_client_com_dados):
        resp = app_client_com_dados.post('/api/notas',
            data=json.dumps({'numero_nota': 'NF-X'}),
            content_type='application/json')
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert 'obrigatório' in data['erro']


class TestAPICustos:
    """Testa CRUD de custos/descontos vinculados a notas."""

    def test_custos_por_nota(self, app_client_com_dados):
        resp = app_client_com_dados.get('/api/custos/1')
        data = json.loads(resp.data)
        assert len(data) == 2  # 2 custos vinculados à nota 1

    def test_custos_nota_sem_custos(self, app_client_com_dados):
        resp = app_client_com_dados.get('/api/custos/3')
        data = json.loads(resp.data)
        assert len(data) == 0

    def test_criar_custo_com_descontos(self, app_client_com_dados):
        resp = app_client_com_dados.post('/api/custos',
            data=json.dumps({
                'nota_id': 3,
                'fornecedor_id': 1,
                'data_emissao': '2025-03-05',
                'valor_compra': 5000,
                'funrural': 75,
                'taxa_cooperativa': 775,
                'produto_descricao': 'Banana Nanica',
                'status': 'pendente',
            }),
            content_type='application/json')
        assert resp.status_code == 201
        data = json.loads(resp.data)
        # liquido = 5000 - 75 - 775 = 4150
        assert data['valor_liquido'] == 4150.0

    def test_criar_custo_sem_nota_erro(self, app_client_com_dados):
        resp = app_client_com_dados.post('/api/custos',
            data=json.dumps({
                'fornecedor_id': 1,
                'data_emissao': '2025-03-05',
                'valor_compra': 1000,
            }),
            content_type='application/json')
        assert resp.status_code == 400

    def test_custo_desconta_do_liquido_nota(self, app_client_com_dados):
        # Antes: nota 3 sem custos
        resp = app_client_com_dados.get('/api/notas')
        nf3_antes = next(n for n in json.loads(resp.data) if n['numero_nota'] == 'NF-TEST-003')
        assert nf3_antes['total_custos'] == 0

        # Adicionar custo
        app_client_com_dados.post('/api/custos',
            data=json.dumps({
                'nota_id': 3, 'fornecedor_id': 2, 'data_emissao': '2025-03-05',
                'valor_compra': 4000, 'funrural': 60, 'taxa_cooperativa': 620,
            }),
            content_type='application/json')

        # Depois: nota 3 tem custos descontados
        resp = app_client_com_dados.get('/api/notas')
        nf3_depois = next(n for n in json.loads(resp.data) if n['numero_nota'] == 'NF-TEST-003')
        assert nf3_depois['total_custos'] > 0
        assert nf3_depois['valor_liquido_nota'] < 15000


class TestAPIPrefeituras:
    """Testa agregação por prefeitura com custos descontados."""

    def test_prefeituras_retorna_dados(self, app_client_com_dados):
        resp = app_client_com_dados.get('/api/prefeituras')
        data = json.loads(resp.data)
        assert len(data) >= 1

    def test_prefeitura_com_custos_descontados(self, app_client_com_dados):
        resp = app_client_com_dados.get('/api/prefeituras')
        data = json.loads(resp.data)
        pref = next((p for p in data if 'Santa Isabel' in str(p.get('prefeitura', ''))), None)
        assert pref is not None
        # NF-TEST-001 tem custos: 2490 + 1660 = 4150
        assert pref['total_custos'] == 4150.0
        # Contratos: 10000 + 6000 = 16000; resultado = 16000 - 4150 = 11850
        assert pref['resultado_liquido'] == 11850.0

    def test_prefeitura_sem_custos(self, app_client_com_dados):
        resp = app_client_com_dados.get('/api/prefeituras')
        data = json.loads(resp.data)
        # Conab não é prefeitura, mas se retornar, custos devem ser 0 na nota 3
        conab = next((p for p in data if 'Conab' in str(p.get('prefeitura', ''))), None)
        if conab:
            assert conab['total_custos'] == 0


class TestAPIPagamentos:
    """Testa listagem geral de pagamentos."""

    def test_pagamentos_lista(self, app_client_com_dados):
        resp = app_client_com_dados.get('/api/pagamentos')
        data = json.loads(resp.data)
        assert len(data) == 2
        assert all('fornecedor_nome' in p for p in data)

    def test_fornecedores_resumo(self, app_client_com_dados):
        resp = app_client_com_dados.get('/api/fornecedores')
        data = json.loads(resp.data)
        assert len(data) == 2
        assert all('total_bruto' in f for f in data)
