# app.py
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
import sqlite3
import pandas as pd
from datetime import datetime

from processador_coaipro import (
    is_primeira_execucao,
    carregar_planilhas_modelo,
    garantir_banco_criado,
)
from seed_loader import migrar_seed_tabelas

app = Flask(__name__)
CORS(app)
DATABASE = 'coaipro.db'


def _inicializar_primeira_execucao():
    garantir_banco_criado(DATABASE)
    if is_primeira_execucao(DATABASE):
        migrar_seed_tabelas(DATABASE)
        carregar_planilhas_modelo(DATABASE)


_inicializar_primeira_execucao()


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Páginas
# ---------------------------------------------------------------------------
@app.route('/')
def landing():
    return render_template('landing.html')


@app.route('/painel/graficos')
def graficos():
    return render_template('graficos.html')


@app.route('/painel/tabelas')
def tabelas():
    return render_template('tabelas.html')


@app.route('/painel/cadastros')
def cadastros():
    return render_template('cadastros.html')


@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')


# ---------------------------------------------------------------------------
# Listas para dropdowns dos formulários
# ---------------------------------------------------------------------------
@app.route('/api/clientes_lista')
def api_clientes_lista():
    conn = get_db()
    rows = conn.execute("SELECT id, nome, tipo FROM clientes WHERE ativo = 1 ORDER BY nome").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/fornecedores_lista')
def api_fornecedores_lista():
    conn = get_db()
    rows = conn.execute("SELECT id, nome FROM fornecedores WHERE ativo = 1 ORDER BY nome").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/notas_lista')
def api_notas_lista():
    """Notas para dropdown do formulário de custos."""
    conn = get_db()
    rows = conn.execute("""
        SELECT nf.id, nf.numero_nota, c.nome as cliente_nome, nf.valor_total
        FROM notas_fiscais nf
        JOIN clientes c ON nf.cliente_id = c.id
        ORDER BY nf.data_emissao DESC
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ---------------------------------------------------------------------------
# Resumo dashboard
# ---------------------------------------------------------------------------
@app.route('/api/resumo')
def api_resumo():
    conn = get_db()
    totais = conn.execute("""
        SELECT
            (SELECT COUNT(*) FROM notas_fiscais WHERE status IN ('pendente','emitida')) as notas_pendentes,
            (SELECT COUNT(*) FROM pagamentos_fornecedores WHERE status = 'pendente') as pagamentos_pendentes,
            (SELECT IFNULL(SUM(valor_total), 0) FROM notas_fiscais WHERE status IN ('pendente','emitida')) as valor_a_receber,
            (SELECT IFNULL(SUM(valor_liquido), 0) FROM pagamentos_fornecedores WHERE status = 'pendente') as valor_a_pagar
    """).fetchone()

    ultimas_notas = conn.execute("""
        SELECT nf.numero_nota, c.nome as cliente, nf.valor_total, nf.status, nf.data_emissao
        FROM notas_fiscais nf
        JOIN clientes c ON nf.cliente_id = c.id
        ORDER BY nf.data_emissao DESC LIMIT 10
    """).fetchall()

    ultimos_pagamentos = conn.execute("""
        SELECT pf.id, f.nome as fornecedor, pf.valor_liquido, pf.status, pf.data_emissao
        FROM pagamentos_fornecedores pf
        JOIN fornecedores f ON pf.fornecedor_id = f.id
        ORDER BY pf.data_emissao DESC LIMIT 10
    """).fetchall()

    conn.close()
    return jsonify({
        'totais': dict(totais),
        'ultimas_notas': [dict(r) for r in ultimas_notas],
        'ultimos_pagamentos': [dict(r) for r in ultimos_pagamentos]
    })


# ---------------------------------------------------------------------------
# Prefeituras (view com custos agregados)
# ---------------------------------------------------------------------------
@app.route('/api/prefeituras')
def api_prefeituras():
    conn = get_db()
    df = pd.read_sql_query("SELECT * FROM vw_resultados_por_prefeitura", conn)
    conn.close()
    return jsonify(df.to_dict(orient='records'))


# ---------------------------------------------------------------------------
# Fornecedores resumo
# ---------------------------------------------------------------------------
@app.route('/api/fornecedores')
def api_fornecedores():
    conn = get_db()
    df = pd.read_sql_query("SELECT * FROM vw_pagamentos_resumo", conn)
    conn.close()
    return jsonify(df.to_dict(orient='records'))


# ---------------------------------------------------------------------------
# Notas Fiscais (CRUD)
# ---------------------------------------------------------------------------
@app.route('/api/notas')
def api_notas():
    conn = get_db()
    notas = conn.execute("""
        SELECT nf.*, c.nome as cliente_nome,
               IFNULL(cn.total_liquido_fornecedores, 0) as total_custos,
               (nf.valor_total - IFNULL(cn.total_liquido_fornecedores, 0)) as valor_liquido_nota
        FROM notas_fiscais nf
        JOIN clientes c ON nf.cliente_id = c.id
        LEFT JOIN vw_custos_por_nota cn ON cn.nota_id = nf.id
        ORDER BY nf.data_emissao DESC
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in notas])


@app.route('/api/notas', methods=['POST'])
def api_criar_nota():
    d = request.json
    if not d:
        return jsonify({'erro': 'Dados não enviados'}), 400

    campos = ['numero_nota', 'data_emissao', 'cliente_id', 'valor_total']
    for c in campos:
        if not d.get(c):
            return jsonify({'erro': f'Campo obrigatório: {c}'}), 400

    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO notas_fiscais (
                numero_nota, data_emissao, cliente_id, peso_liquido, quantidade_cx,
                valor_total, valor_frete, previsao_pagamento, data_pagamento, status, observacoes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            d['numero_nota'],
            d['data_emissao'],
            int(d['cliente_id']),
            float(d.get('peso_liquido') or 0),
            int(d.get('quantidade_cx') or 0) or None,
            float(d['valor_total']),
            float(d.get('valor_frete') or 0),
            d.get('previsao_pagamento') or None,
            d.get('data_pagamento') or None,
            d.get('status', 'emitida'),
            d.get('observacoes') or None,
        ))
        conn.commit()
        nota_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        return jsonify({'id': nota_id, 'mensagem': 'Nota cadastrada com sucesso'}), 201
    except sqlite3.IntegrityError as e:
        conn.close()
        return jsonify({'erro': f'Nota já existe ou dados inválidos: {e}'}), 400


# ---------------------------------------------------------------------------
# Custos / Descontos por Nota (CRUD)
# ---------------------------------------------------------------------------
@app.route('/api/custos/<int:nota_id>')
def api_custos_por_nota(nota_id):
    conn = get_db()
    custos = conn.execute("""
        SELECT pf.*, f.nome as fornecedor_nome
        FROM pagamentos_fornecedores pf
        JOIN fornecedores f ON pf.fornecedor_id = f.id
        WHERE pf.nota_id = ?
        ORDER BY pf.data_emissao DESC
    """, (nota_id,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in custos])


@app.route('/api/custos', methods=['POST'])
def api_criar_custo():
    d = request.json
    if not d:
        return jsonify({'erro': 'Dados não enviados'}), 400

    for c in ['nota_id', 'fornecedor_id', 'data_emissao', 'valor_compra']:
        if not d.get(c):
            return jsonify({'erro': f'Campo obrigatório: {c}'}), 400

    valor_compra = float(d['valor_compra'])
    funrural = float(d.get('funrural') or 0)
    taxa_coop = float(d.get('taxa_cooperativa') or 0)
    desconto_aipro = float(d.get('desconto_aipro') or 0)
    pg_caixa_entrega = float(d.get('pg_caixa_entrega') or 0)
    pg_frete_coop = float(d.get('pg_frete_coop') or 0)
    pg_caixa_papelao = float(d.get('pg_caixa_papelao') or 0)
    frete_cooaipro = float(d.get('frete_cooaipro') or 0)
    pg_extra_paa = float(d.get('pg_extra_paa') or 0)
    pg_frete_kits_mogi = float(d.get('pg_frete_kits_mogi') or 0)
    pg_ref_uso_hr = float(d.get('pg_ref_uso_hr') or 0)
    pg_ref_uso_ford = float(d.get('pg_ref_uso_ford') or 0)
    ref_frete_entrega_poa = float(d.get('ref_frete_entrega_poa') or 0)
    pg_afranio = float(d.get('pg_afranio') or 0)
    pg_frete_mogi = float(d.get('pg_frete_mogi') or 0)
    vlr_pago_combustivel = float(d.get('vlr_pago_combustivel') or 0)
    frete_entrega_sao_jose = float(d.get('frete_entrega_sao_jose') or 0)
    pg_uso_camara_fria = float(d.get('pg_uso_camara_fria') or 0)
    pg_doacao_produtor = float(d.get('pg_doacao_produtor') or 0)
    pg_extra = float(d.get('pg_extra') or 0)
    adiantamento = float(d.get('adiantamento') or 0)
    descontos_outros = float(d.get('descontos_outros') or 0)

    total_descontos = (funrural + taxa_coop + desconto_aipro + pg_caixa_entrega +
                       pg_frete_coop + pg_caixa_papelao + frete_cooaipro + pg_extra_paa +
                       pg_frete_kits_mogi + pg_ref_uso_hr + pg_ref_uso_ford +
                       ref_frete_entrega_poa + pg_afranio + pg_frete_mogi +
                       vlr_pago_combustivel + frete_entrega_sao_jose +
                       pg_uso_camara_fria + pg_doacao_produtor + pg_extra +
                       adiantamento + descontos_outros)
    valor_liquido = valor_compra - total_descontos

    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO pagamentos_fornecedores (
                nota_id, fornecedor_id, data_emissao, prefeitura, produto_descricao,
                numero_nota_fornecedor, peso_liquido, valor_compra,
                funrural, taxa_cooperativa, desconto_aipro,
                pg_caixa_entrega, pg_frete_coop, pg_caixa_papelao, frete_cooaipro,
                pg_extra_paa, pg_frete_kits_mogi, pg_ref_uso_hr, pg_ref_uso_ford,
                ref_frete_entrega_poa, pg_afranio, pg_frete_mogi, vlr_pago_combustivel,
                frete_entrega_sao_jose, pg_uso_camara_fria, pg_doacao_produtor,
                pg_extra, adiantamento, descontos_outros,
                valor_liquido, status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            int(d['nota_id']),
            int(d['fornecedor_id']),
            d['data_emissao'],
            d.get('prefeitura') or None,
            d.get('produto_descricao') or None,
            d.get('numero_nota_fornecedor') or None,
            float(d.get('peso_liquido') or 0),
            valor_compra,
            funrural, taxa_coop, desconto_aipro,
            pg_caixa_entrega, pg_frete_coop, pg_caixa_papelao, frete_cooaipro,
            pg_extra_paa, pg_frete_kits_mogi, pg_ref_uso_hr, pg_ref_uso_ford,
            ref_frete_entrega_poa, pg_afranio, pg_frete_mogi, vlr_pago_combustivel,
            frete_entrega_sao_jose, pg_uso_camara_fria, pg_doacao_produtor,
            pg_extra, adiantamento, descontos_outros,
            valor_liquido,
            d.get('status', 'pendente'),
        ))
        conn.commit()
        custo_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        return jsonify({'id': custo_id, 'valor_liquido': valor_liquido, 'mensagem': 'Custo cadastrado com sucesso'}), 201
    except Exception as e:
        conn.close()
        return jsonify({'erro': str(e)}), 400


# ---------------------------------------------------------------------------
# Pagamentos (listagem geral)
# ---------------------------------------------------------------------------
@app.route('/api/pagamentos')
def api_pagamentos():
    conn = get_db()
    pagamentos = conn.execute("""
        SELECT pf.*, f.nome as fornecedor_nome,
               nf.numero_nota as nota_vinculada
        FROM pagamentos_fornecedores pf
        JOIN fornecedores f ON pf.fornecedor_id = f.id
        LEFT JOIN notas_fiscais nf ON pf.nota_id = nf.id
        ORDER BY pf.data_emissao DESC
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in pagamentos])


# ---------------------------------------------------------------------------
# Banco de dados: download / upload
# ---------------------------------------------------------------------------
@app.route('/api/banco/download')
def api_banco_download():
    """Download do arquivo coaipro.db."""
    import os
    from flask import send_file
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), DATABASE)
    if not os.path.exists(db_path):
        return jsonify({'erro': 'Banco de dados não encontrado'}), 404
    return send_file(db_path, as_attachment=True, download_name='coaipro.db')


@app.route('/api/banco/upload', methods=['POST'])
def api_banco_upload():
    """Upload para substituir o banco de dados local."""
    import os, shutil
    if 'arquivo' not in request.files:
        return jsonify({'erro': 'Nenhum arquivo enviado'}), 400

    arquivo = request.files['arquivo']
    if arquivo.filename == '':
        return jsonify({'erro': 'Arquivo vazio'}), 400

    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), DATABASE)

    # Backup do banco atual
    if os.path.exists(db_path):
        backup_path = db_path + '.backup'
        shutil.copy2(db_path, backup_path)

    try:
        arquivo.save(db_path)
        return jsonify({'mensagem': 'Banco de dados atualizado com sucesso! Recarregando...'}), 200
    except Exception as e:
        # Restaurar backup se falhou
        if os.path.exists(db_path + '.backup'):
            shutil.copy2(db_path + '.backup', db_path)
        return jsonify({'erro': str(e)}), 500


# ---------------------------------------------------------------------------
# Exportar BD como Excel e Visualizador
# ---------------------------------------------------------------------------
@app.route('/api/banco/excel')
def api_banco_excel():
    """Exporta todas as tabelas do BD como planilha Excel."""
    import os, io
    from flask import send_file
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), DATABASE)
    if not os.path.exists(db_path):
        return jsonify({'erro': 'Banco de dados não encontrado'}), 404

    conn = sqlite3.connect(db_path)
    tabelas = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()]
    views = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
    ).fetchall()]

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        for t in tabelas + views:
            try:
                df = pd.read_sql_query(f'SELECT * FROM "{t}"', conn)
                sheet = t[:31]  # Excel limita 31 chars
                df.to_excel(writer, sheet_name=sheet, index=False)
            except Exception:
                pass
    conn.close()
    output.seek(0)
    return send_file(output, as_attachment=True,
                     download_name='plantar_banco_completo.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/api/banco/tabelas')
def api_banco_tabelas():
    """Retorna lista de tabelas e views com contagem de registros."""
    import os
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), DATABASE)
    conn = sqlite3.connect(db_path)
    items = []
    for tipo in ['table', 'view']:
        rows = conn.execute(
            f"SELECT name FROM sqlite_master WHERE type='{tipo}' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        for r in rows:
            nome = r[0]
            try:
                cnt = conn.execute(f'SELECT COUNT(*) FROM "{nome}"').fetchone()[0]
            except Exception:
                cnt = 0
            items.append({'nome': nome, 'tipo': tipo, 'registros': cnt})
    conn.close()
    return jsonify(items)


@app.route('/api/banco/tabela/<nome>')
def api_banco_tabela_dados(nome):
    """Retorna dados completos de uma tabela ou view."""
    import os
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), DATABASE)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(f'SELECT * FROM "{nome}"').fetchall()
        if rows:
            colunas = list(rows[0].keys())
            dados = [dict(r) for r in rows]
        else:
            # Pegar colunas mesmo sem dados
            cur = conn.execute(f'PRAGMA table_info("{nome}")')
            info = cur.fetchall()
            colunas = [dict(c)['name'] for c in info] if info else []
            dados = []
        conn.close()
        return jsonify({'colunas': colunas, 'dados': dados, 'total': len(dados)})
    except Exception as e:
        conn.close()
        return jsonify({'erro': str(e)}), 400


@app.route('/painel/visualizador')
def visualizador():
    return render_template('visualizador.html')


if __name__ == '__main__':
    app.run(debug=True, port=5000)
