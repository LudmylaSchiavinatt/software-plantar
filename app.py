from flask import Flask, jsonify, render_template, request, send_file, redirect, url_for
from flask_cors import CORS
import sqlite3
import pandas as pd
import os
import shutil
import io
import re
import unicodedata
from datetime import date

from processador_coaipro import (
    is_primeira_execucao,
    carregar_planilhas_modelo,
    garantir_banco_criado,
)
from seed_loader import migrar_seed_tabelas

app = Flask(__name__)
CORS(app)
app.config['SECRET_KEY'] = 'coaipro'
app.config['CODIGO_CADASTRO_COOPERATIVA'] = 'coaipro'

from flask_login import LoginManager
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

DATABASE = 'coaipro.db'

# ===========================================================================
# FUNÇÃO INTELIGENTE DE LIMPEZA DE TEXTO E AUTO-CADASTRO
# ===========================================================================
def simplificar_texto(texto):
    """Tira acentos, espaços, traços e deixa tudo minúsculo para facilitar a busca"""
    if not texto: return ""
    texto_sem_acento = unicodedata.normalize('NFKD', str(texto)).encode('ASCII', 'ignore').decode('utf-8')
    return re.sub(r'[^a-z0-9]', '', texto_sem_acento.lower())

def _inicializar_primeira_execucao():
    garantir_banco_criado(DATABASE)
    if is_primeira_execucao(DATABASE):
        migrar_seed_tabelas(DATABASE)
        carregar_planilhas_modelo(DATABASE)

_inicializar_primeira_execucao()

def get_db():
    # check_same_thread=False previne erros de concorrência no Flask multithread
    conn = sqlite3.connect(DATABASE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

# ===========================================================================
# AUTENTICAÇÃO (LOGIN / CADASTRO)
# ===========================================================================
from flask_login import UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash

class Usuario(UserMixin):
    def __init__(self, id, email, tipo, fornecedor_id, aprovado):
        self.id = id
        self.email = email
        self.tipo = tipo
        self.fornecedor_id = fornecedor_id
        self.aprovado = aprovado

@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM usuarios WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if row:
        return Usuario(row['id'], row['email'], row['tipo'], row['fornecedor_id'], row['aprovado'])
    return None

# ===========================================================================
# PÁGINAS (HTML)
# ===========================================================================

@app.route('/')
def landing():
    return render_template('landing.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        senha = request.form['senha']

        conn = get_db()
        row = conn.execute("SELECT * FROM usuarios WHERE email = ?", (email,)).fetchone()
        conn.close()

        if not row or not check_password_hash(row['senha_hash'], senha):
            # Reabre o modal de Login na própria landing, com a mensagem de erro
            return render_template('landing.html', erro_login='Email ou senha inválidos')

        usuario = Usuario(row['id'], row['email'], row['tipo'], row['fornecedor_id'], row['aprovado'])
        login_user(usuario)

        if usuario.tipo == 'cooperativa':
            return redirect(url_for('tabelas'))
        return redirect(url_for('meu_extrato'))

    # GET /login: não há mais página própria, os modais vivem na landing
    return redirect(url_for('landing'))
@app.route('/cadastro', methods=['GET', 'POST'])
def cadastro():
    if request.method == 'POST':
        tipo = request.form.get('tipo', 'cooperado')
        email = request.form['email']
        senha = request.form['senha']

        conn = get_db()

        existente = conn.execute("SELECT id FROM usuarios WHERE email = ?", (email,)).fetchone()
        if existente:
            conn.close()
            return render_template('landing.html', erro_cadastro='Esse email já está cadastrado.')

        if tipo == 'cooperativa':
            codigo = request.form.get('codigo_acesso', '')
            if codigo != app.config['CODIGO_CADASTRO_COOPERATIVA']:
                conn.close()
                return render_template('landing.html', erro_cadastro='Código de acesso inválido.')

            conn.execute("""
                INSERT INTO usuarios (email, senha_hash, tipo, fornecedor_id, aprovado, ativo)
                VALUES (?, ?, 'cooperativa', NULL, 1, 1)
            """, (email, generate_password_hash(senha)))
            conn.commit()
            conn.close()
            return render_template('landing.html', sucesso_cadastro='Conta da cooperativa criada! Já pode fazer login.')

        else:  # cooperado
            cpf_cnpj = request.form.get('cpf_cnpj', '')
            fornecedor = conn.execute("SELECT id FROM fornecedores WHERE cpf_cnpj = ?", (cpf_cnpj,)).fetchone()

            if not fornecedor:
                conn.close()
                return render_template('landing.html', erro_cadastro='CPF/CNPJ não encontrado. Fale com a cooperativa.')

            conn.execute("""
                INSERT INTO usuarios (email, senha_hash, tipo, fornecedor_id, aprovado)
                VALUES (?, ?, 'cooperado', ?, 1)
            """, (email, generate_password_hash(senha), fornecedor['id']))
            conn.commit()
            conn.close()
            return render_template('landing.html', sucesso_cadastro='Cadastro realizado com sucesso! Você já pode fazer login.')

    # GET /cadastro: não há mais página própria, os modais vivem na landing
    return redirect(url_for('landing'))
@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/painel/graficos')
@login_required
def graficos():
    if current_user.tipo != 'cooperativa':
        return redirect(url_for('meu_extrato'))
    return render_template('graficos.html')

@app.route('/painel/cadastros')
@login_required
def cadastros():
    if current_user.tipo != 'cooperativa':
        return redirect(url_for('meu_extrato'))
    return render_template('cadastros.html')

@app.route('/painel/visualizador')
@login_required
def visualizador():
    if current_user.tipo != 'cooperativa':
        return redirect(url_for('meu_extrato'))
    return render_template('visualizador.html')

@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')
# ===========================================================================
# ÁREA DO COOPERADO
# ===========================================================================
@app.route('/cooperativa')
@login_required
def painel_cooperativa():
    if current_user.tipo != 'cooperado':
        return redirect(url_for('tabelas'))

    conn = get_db()
    fornecedor = conn.execute(
        "SELECT nome FROM fornecedores WHERE id = ?", (current_user.fornecedor_id,)
    ).fetchone()
    conn.close()

    nome_cooperado = fornecedor['nome'] if fornecedor else 'Cooperado'

    cotacoes_do_dia = {
        'soja': {'preco': 128.50, 'variacao': '+1.2%', 'tendencia': 'alta'},
        'milho': {'preco': 58.30, 'variacao': '-0.5%', 'tendencia': 'baixa'},
        'cafe': {'preco': 1145.00, 'variacao': '+2.3%', 'tendencia': 'alta'}
    }
    
    avisos = [
        {'data': '18/07/2026', 'titulo': 'Horário Estendido', 'texto': 'Durante a próxima semana, a balança funcionará até as 20h.'},
        {'data': '15/07/2026', 'titulo': 'Reunião de Safra', 'texto': 'Convidamos todos para a apresentação dos resultados do semestre.'}
    ]
    
    return render_template('cooperativa_visao_cooperado.html', 
                           cotacoes=cotacoes_do_dia, avisos=avisos, nome_cooperado=nome_cooperado)

@app.route('/meu-extrato')
@login_required
def meu_extrato():
    # Redireciona usuários administrativos para o painel deles
    if current_user.tipo != 'cooperado':
        return redirect(url_for('tabelas'))

    # Pegamos o ID do fornecedor vinculado ao usuário logado
    fornecedor_id = current_user.fornecedor_id
    
    if not fornecedor_id:
        return "Perfil de cooperado incompleto (Sem vínculo de fornecedor).", 400

    conn = get_db()
    try:
        # Busca o resumo financeiro exclusivo deste fornecedor
        resumo = conn.execute(
            "SELECT * FROM vw_pagamentos_resumo WHERE id = ?", 
            (fornecedor_id,)
        ).fetchone()

        # Busca a lista de pagamentos/entregas apenas deste fornecedor
        pagamentos = conn.execute("""
            SELECT pf.data_emissao, pf.produto_descricao, nf.numero_nota as nota_vinculada, 
                   pf.valor_compra, pf.valor_liquido, pf.status
            FROM pagamentos_fornecedores pf
            LEFT JOIN notas_fiscais nf ON pf.nota_id = nf.id
            WHERE pf.fornecedor_id = ?
            ORDER BY pf.data_emissao DESC
        """, (fornecedor_id,)).fetchall()
    finally:
        conn.close()

    # Se não tiver resumo (novo cooperado sem notas), manda um dict vazio ou com zeros
    if not resumo:
        resumo = {'cota': 0, 'saldo': 0, 'total_pago': 0, 'pendente': 0}

    return render_template('meu_extrato.html', 
                           resumo=dict(resumo), 
                           pagamentos=[dict(p) for p in pagamentos])

@app.route('/meu-perfil', methods=['GET', 'POST'])
@login_required
def meu_perfil():
    if current_user.tipo != 'cooperado':
        return redirect(url_for('tabelas'))

    fornecedor_id = current_user.fornecedor_id
    if not fornecedor_id:
        return "Perfil de cooperado incompleto (Sem vínculo de fornecedor).", 400

    conn = get_db()
    try:
        if request.method == 'POST':
            nome = request.form.get('nome', '').strip()
            cidade = request.form.get('cidade', '').strip()
            email = request.form.get('email', '').strip()
            nova_senha = request.form.get('nova_senha', '').strip()

            erro = None
            if not nome:
                erro = 'Nome é obrigatório.'
            elif not email:
                erro = 'Email é obrigatório.'
            else:
                email_existente = conn.execute(
                    "SELECT id FROM usuarios WHERE email = ? AND id != ?",
                    (email, current_user.id)
                ).fetchone()
                if email_existente:
                    erro = 'Esse email já está em uso por outra conta.'

            if erro:
                fornecedor = conn.execute("SELECT * FROM fornecedores WHERE id = ?", (fornecedor_id,)).fetchone()
                return render_template('meu_perfil.html', fornecedor=dict(fornecedor), email=current_user.email, erro=erro)

            conn.execute("UPDATE fornecedores SET nome = ?, cidade = ? WHERE id = ?", (nome, cidade or None, fornecedor_id))

            if nova_senha:
                conn.execute("UPDATE usuarios SET email = ?, senha_hash = ? WHERE id = ?",
                             (email, generate_password_hash(nova_senha), current_user.id))
            else:
                conn.execute("UPDATE usuarios SET email = ? WHERE id = ?", (email, current_user.id))

            conn.commit()
            fornecedor = conn.execute("SELECT * FROM fornecedores WHERE id = ?", (fornecedor_id,)).fetchone()
            return render_template('meu_perfil.html', fornecedor=dict(fornecedor), email=email, sucesso='Dados atualizados com sucesso!')

        fornecedor = conn.execute("SELECT * FROM fornecedores WHERE id = ?", (fornecedor_id,)).fetchone()
        return render_template('meu_perfil.html', fornecedor=dict(fornecedor) if fornecedor else {}, email=current_user.email)
    finally:
        conn.close()
        
@app.route('/painel/tabelas')
@login_required
def tabelas():
    if current_user.tipo != 'cooperativa':
        return redirect(url_for('meu_extrato'))
    conn = get_db()
    try:
        prefs_df = pd.read_sql_query("SELECT * FROM vw_resultados_por_prefeitura", conn)
        prefs_df = prefs_df.where(pd.notna(prefs_df), None)
        prefeituras = prefs_df.to_dict(orient='records')
        
        forns_df = pd.read_sql_query("SELECT * FROM vw_pagamentos_resumo", conn)
        forns_df = forns_df.where(pd.notna(forns_df), None)
        fornecedores = forns_df.to_dict(orient='records')
        
        notas = conn.execute("""
            SELECT nf.*, c.nome as cliente_nome,
                   IFNULL(cn.total_liquido_fornecedores, 0) as total_custos,
                   (nf.valor_total - IFNULL(cn.total_liquido_fornecedores, 0)) as valor_liquido_nota
            FROM notas_fiscais nf
            JOIN clientes c ON nf.cliente_id = c.id
            LEFT JOIN vw_custos_por_nota cn ON cn.nota_id = nf.id
            ORDER BY nf.data_emissao DESC
        """).fetchall()
        notas = [dict(n) for n in notas]
        
        pagamentos = conn.execute("""
            SELECT pf.*, f.nome as fornecedor_nome,
                   nf.numero_nota as nota_vinculada
            FROM pagamentos_fornecedores pf
            JOIN fornecedores f ON pf.fornecedor_id = f.id
            LEFT JOIN notas_fiscais nf ON pf.nota_id = nf.id
            ORDER BY pf.data_emissao DESC
        """).fetchall()
        pagamentos = [dict(p) for p in pagamentos]
    finally:
        conn.close()
        
    return render_template('tabelas.html', prefeituras=prefeituras, fornecedores=fornecedores, notas=notas, pagamentos=pagamentos)

@app.route('/api/notas/<int:id>/relatorio')
def relatorio_nota(id):
    conn = get_db()
    try:
        nota = conn.execute("""
            SELECT nf.*, c.nome as cliente_nome, c.cidade as cliente_cidade
            FROM notas_fiscais nf JOIN clientes c ON nf.cliente_id = c.id WHERE nf.id = ?
        """, (id,)).fetchone()
        
        custos = conn.execute("""
            SELECT pf.*, f.nome as fornecedor_nome
            FROM pagamentos_fornecedores pf JOIN fornecedores f ON pf.fornecedor_id = f.id WHERE pf.nota_id = ?
        """, (id,)).fetchall()
    finally:
        conn.close()

    if not nota:
        return "Nota não encontrada", 404

    dados_emitente = {
        "nome_fantasia": "COOAIPRO",
        "razao_social": "COOPERATIVA DE PRODUTORES FAMILIARES DE SANTA ISABEL",
        "cnpj": "24.466.458/0001-07",
        "ie": "616047540117",
        "endereco_linha1": "RUA PRESIDENTE CASTELO BRANCO, 687 - VILA OSIRIS",
        "endereco_linha2": "Santa Isabel, SP - CEP: 07500-000"
    }
    
    return render_template('relatorio_nota.html', nota=dict(nota), custos=[dict(c) for c in custos], emitente=dados_emitente)

@app.route('/api/fornecedores/<int:id>/relatorio')
def relatorio_cooperado(id):
    conn = get_db()
    try:
        fornecedor = conn.execute(
            "SELECT * FROM fornecedores WHERE id = ?", (id,)
        ).fetchone()

        if not fornecedor:
            return "Cooperado não encontrado", 404

        pagamentos = conn.execute("""
            SELECT pf.*, nf.numero_nota as nota_vinculada, c.nome as cliente_nome
            FROM pagamentos_fornecedores pf
            LEFT JOIN notas_fiscais nf ON pf.nota_id = nf.id
            LEFT JOIN clientes c ON nf.cliente_id = c.id
            WHERE pf.fornecedor_id = ?
            ORDER BY pf.data_emissao
        """, (id,)).fetchall()

        resumo = conn.execute(
            "SELECT * FROM vw_pagamentos_resumo WHERE id = ?", (id,)
        ).fetchone()
    finally:
        conn.close()

    dados_emitente = {
        "nome_fantasia": "COOAIPRO",
        "razao_social": "COOPERATIVA DE PRODUTORES FAMILIARES DE SANTA ISABEL",
        "cnpj": "24.466.458/0001-07",
        "ie": "616047540117",
        "endereco_linha1": "RUA PRESIDENTE CASTELO BRANCO, 687 - VILA OSIRIS",
        "endereco_linha2": "Santa Isabel, SP - CEP: 07500-000"
    }

    return render_template('relatorio_cooperado.html',
                            fornecedor=dict(fornecedor),
                            pagamentos=[dict(p) for p in pagamentos],
                            resumo=dict(resumo) if resumo else {'total_bruto': 0, 'total_liquido': 0, 'total_descontos': 0, 'total_a_pagar': 0, 'cota': 0},
                            emitente=dados_emitente,
                            data_emissao=date.today().strftime('%d/%m/%Y'),
                            periodo=None)

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
    conn = get_db()
    rows = conn.execute("""
        SELECT nf.id, nf.numero_nota, c.nome as cliente_nome, nf.valor_total
        FROM notas_fiscais nf
        JOIN clientes c ON nf.cliente_id = c.id
        ORDER BY nf.data_emissao DESC
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

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
        'totais': dict(totais) if totais else {"notas_pendentes": 0, "pagamentos_pendentes": 0, "valor_a_receber": 0, "valor_a_pagar": 0},
        'ultimas_notas': [dict(r) for r in ultimas_notas],
        'ultimos_pagamentos': [dict(r) for r in ultimos_pagamentos]
    })

@app.route('/api/prefeituras')
def api_prefeituras():
    conn = get_db()
    df = pd.read_sql_query("SELECT * FROM vw_resultados_por_prefeitura", conn)
    conn.close()
    
    # Converte para dicionários nativos do Python primeiro
    dados = df.to_dict(orient='records')
    
    # Limpa os NaNs usando compreensão de dicionário nativa do Python
    import math
    dados_limpos = [
        {
            k: (None if isinstance(v, float) and math.isnan(v) else v) 
            for k, v in registro.items()
        } 
        for registro in dados
    ]
    
    return jsonify(dados_limpos)

@app.route('/api/fornecedores')
def api_fornecedores():
    conn = get_db()
    df = pd.read_sql_query("SELECT * FROM vw_pagamentos_resumo", conn)
    conn.close()
    df = df.where(pd.notna(df), None)
    return jsonify(df.to_dict(orient='records'))

# ===========================================================================
# CLIENTES (PREFEITURAS) — CRUD
# ===========================================================================
@app.route('/api/clientes', methods=['POST'])
def api_criar_cliente():
    d = request.json
    if not d: return jsonify({'erro': 'Dados não enviados'}), 400
    nome = str(d.get('nome') or '').strip()
    if not nome: return jsonify({'erro': 'Nome do cliente é obrigatório'}), 400

    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO clientes (nome, tipo, cidade, prazo_pagamento_dias, valor_total_contrato, ativo)
            VALUES (?, ?, ?, ?, ?, 1)
        """, (
            nome,
            d.get('tipo') or 'prefeitura',
            (d.get('cidade') or '').strip() or None,
            int(d.get('prazo_pagamento_dias') or 30),
            float(d.get('valor_total_contrato') or 0)
        ))
        conn.commit()
        cliente_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return jsonify({'id': cliente_id, 'mensagem': 'Cliente cadastrado com sucesso'}), 201
    except Exception as e:
        return jsonify({'erro': str(e)}), 400
    finally:
        conn.close()

@app.route('/api/clientes/<int:id>', methods=['GET'])
def api_obter_cliente(id):
    conn = get_db()
    cliente = conn.execute("SELECT * FROM clientes WHERE id = ?", (id,)).fetchone()
    conn.close()
    if cliente: return jsonify(dict(cliente))
    return jsonify({'erro': 'Cliente não encontrado'}), 404

@app.route('/api/clientes/<int:id>', methods=['PUT'])
def api_atualizar_cliente(id):
    d = request.json
    if not d: return jsonify({'erro': 'Dados não enviados'}), 400
    nome = str(d.get('nome') or '').strip()
    if not nome: return jsonify({'erro': 'Nome do cliente é obrigatório'}), 400

    conn = get_db()
    try:
        conn.execute("""
            UPDATE clientes
            SET nome = ?, tipo = ?, cidade = ?, prazo_pagamento_dias = ?, valor_total_contrato = ?
            WHERE id = ?
        """, (
            nome,
            d.get('tipo') or 'prefeitura',
            (d.get('cidade') or '').strip() or None,
            int(d.get('prazo_pagamento_dias') or 30),
            float(d.get('valor_total_contrato') or 0),
            id
        ))
        conn.commit()
        return jsonify({'mensagem': 'Cliente atualizado com sucesso'})
    except Exception as e:
        return jsonify({'erro': str(e)}), 400
    finally:
        conn.close()

@app.route('/api/clientes/<int:id>', methods=['DELETE'])
def api_excluir_cliente(id):
    conn = get_db()
    try:
        qtd_notas = conn.execute("SELECT COUNT(*) FROM notas_fiscais WHERE cliente_id = ?", (id,)).fetchone()[0]
        if qtd_notas > 0:
            return jsonify({'erro': f'Não é possível excluir: existem {qtd_notas} nota(s) fiscal(is) vinculada(s) a este cliente.'}), 400

        conn.execute("DELETE FROM clientes WHERE id = ?", (id,))
        conn.commit()
        return jsonify({'mensagem': 'Cliente excluído com sucesso'})
    except Exception as e:
        return jsonify({'erro': str(e)}), 400
    finally:
        conn.close()

def _migrar_coluna_valor_contrato():
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute("ALTER TABLE clientes ADD COLUMN valor_total_contrato DECIMAL(10,2) DEFAULT 0")
        conn.commit()
        print("Coluna valor_total_contrato adicionada.")
    except sqlite3.OperationalError:
        pass  # coluna já existe, ignora
    finally:
        conn.close()

_migrar_coluna_valor_contrato()
_inicializar_primeira_execucao()
# ===========================================================================
# FORNECEDORES (COOPERADOS) — CRUD
# ===========================================================================

@app.route('/api/fornecedores', methods=['POST'])
def api_criar_fornecedor():
    d = request.json
    if not d: return jsonify({'erro': 'Dados não enviados'}), 400
    nome = str(d.get('nome') or '').strip()
    if not nome: return jsonify({'erro': 'Nome do cooperado é obrigatório'}), 400

    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO fornecedores (nome, cpf_cnpj, cidade, ativo)
            VALUES (?, ?, ?, 1)
        """, (
            nome,
            (d.get('cpf_cnpj') or '').strip() or None,
            (d.get('cidade') or '').strip() or None
        ))
        conn.commit()
        fornecedor_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return jsonify({'id': fornecedor_id, 'mensagem': 'Cooperado cadastrado com sucesso'}), 201
    except Exception as e:
        return jsonify({'erro': str(e)}), 400
    finally:
        conn.close()

@app.route('/api/fornecedores/<int:id>', methods=['GET'])
def api_obter_fornecedor(id):
    conn = get_db()
    fornecedor = conn.execute("SELECT * FROM fornecedores WHERE id = ?", (id,)).fetchone()
    conn.close()
    if fornecedor: return jsonify(dict(fornecedor))
    return jsonify({'erro': 'Cooperado não encontrado'}), 404

@app.route('/api/fornecedores/<int:id>', methods=['PUT'])
def api_atualizar_fornecedor(id):
    d = request.json
    if not d: return jsonify({'erro': 'Dados não enviados'}), 400
    nome = str(d.get('nome') or '').strip()
    if not nome: return jsonify({'erro': 'Nome do cooperado é obrigatório'}), 400

    conn = get_db()
    try:
        conn.execute("""
            UPDATE fornecedores
            SET nome = ?, cpf_cnpj = ?, cidade = ?
            WHERE id = ?
        """, (
            nome,
            (d.get('cpf_cnpj') or '').strip() or None,
            (d.get('cidade') or '').strip() or None,
            id
        ))
        conn.commit()
        return jsonify({'mensagem': 'Cooperado atualizado com sucesso'})
    except Exception as e:
        return jsonify({'erro': str(e)}), 400
    finally:
        conn.close()

@app.route('/api/fornecedores/<int:id>', methods=['DELETE'])
def api_excluir_fornecedor(id):
    conn = get_db()
    try:
        qtd_pag = conn.execute("SELECT COUNT(*) FROM pagamentos_fornecedores WHERE fornecedor_id = ?", (id,)).fetchone()[0]
        if qtd_pag > 0:
            return jsonify({'erro': f'Não é possível excluir: existem {qtd_pag} pagamento(s) vinculado(s) a este cooperado.'}), 400

        qtd_user = conn.execute("SELECT COUNT(*) FROM usuarios WHERE fornecedor_id = ?", (id,)).fetchone()[0]
        if qtd_user > 0:
            return jsonify({'erro': 'Não é possível excluir: existe um usuário de login vinculado a este cooperado.'}), 400

        conn.execute("DELETE FROM fornecedores WHERE id = ?", (id,))
        conn.commit()
        return jsonify({'mensagem': 'Cooperado excluído com sucesso'})
    except Exception as e:
        return jsonify({'erro': str(e)}), 400
    finally:
        conn.close()

# ===========================================================================
# NOTAS FISCAIS (CRUD E IMPORTAÇÃO INTELIGENTE)
# ===========================================================================

@app.route('/api/notas', methods=['GET'])
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
    if not d: return jsonify({'erro': 'Dados não enviados'}), 400
    for c in ['numero_nota', 'data_emissao', 'cliente_id', 'valor_total']:
        if not d.get(c): return jsonify({'erro': f'Campo obrigatório: {c}'}), 400

    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO notas_fiscais (
                numero_nota, data_emissao, cliente_id, peso_liquido, quantidade_cx,
                valor_total, valor_frete, previsao_pagamento, data_pagamento, status, observacoes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            d['numero_nota'], d['data_emissao'], int(d['cliente_id']), float(d.get('peso_liquido') or 0),
            int(d.get('quantidade_cx') or 0) or None, float(d['valor_total']), float(d.get('valor_frete') or 0),
            d.get('previsao_pagamento') or None, d.get('data_pagamento') or None, d.get('status', 'emitida'), d.get('observacoes') or None,
        ))
        conn.commit()
        nota_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return jsonify({'id': nota_id, 'mensagem': 'Nota cadastrada com sucesso'}), 201
    except sqlite3.IntegrityError as e:
        return jsonify({'erro': f'Nota já existe ou dados inválidos: {e}'}), 400
    finally:
        conn.close()

@app.route('/api/notas/<int:id>', methods=['DELETE'])
def deletar_nota(id):
    conn = get_db()
    conn.execute('DELETE FROM notas_fiscais WHERE id = ?', (id,))
    conn.commit()
    conn.close()
    return jsonify({'mensagem': 'Nota excluída'})

@app.route('/api/notas/<int:id>', methods=['PUT'])
def atualizar_nota(id):
    d = request.json
    if not d: return jsonify({'erro': 'Dados não enviados'}), 400
    conn = get_db()
    try:
        conn.execute("""
            UPDATE notas_fiscais
            SET numero_nota = ?, data_emissao = ?, cliente_id = ?, valor_total = ?, status = ?
            WHERE id = ?
        """, (d['numero_nota'], d['data_emissao'], int(d['cliente_id']), float(d['valor_total']), d['status'], id))
        conn.commit()
        return jsonify({'mensagem': 'Nota atualizada'})
    except Exception as e:
        return jsonify({'erro': str(e)}), 400
    finally:
        conn.close()

@app.route('/api/notas/<int:id>', methods=['GET'])
def get_nota(id):
    conn = get_db()
    nota = conn.execute("SELECT * FROM notas_fiscais WHERE id = ?", (id,)).fetchone()
    conn.close()
    if nota: return jsonify(dict(nota))
    return jsonify({'erro': 'Nota não encontrada'}), 404

@app.route('/api/notas/importar', methods=['POST'])
def api_importar_notas():
    dados = request.json
    if not dados or not isinstance(dados, list): return jsonify({'erro': 'Nenhum dado válido recebido.'}), 400

    conn = get_db()
    sucesso = 0
    erros = []
    
    clientes_db_raw = conn.execute("SELECT id, nome FROM clientes").fetchall()
    clientes_db = [{'id': c['id'], 'nome': c['nome']} for c in clientes_db_raw]

    try:
        for idx, d in enumerate(dados):
            linha = idx + 2
            numero_nota = str(d.get('numero_nota') or '').strip()
            cliente_nome_planilha = str(d.get('cliente') or d.get('cliente_nome') or '').strip()
            
            if not numero_nota or not cliente_nome_planilha:
                erros.append(f"Linha {linha}: Número da nota e Cliente são obrigatórios.")
                continue

            nome_limpo_planilha = simplificar_texto(cliente_nome_planilha)
            cliente_id = None
            
            for c in clientes_db:
                nome_limpo_db = simplificar_texto(c['nome'])
                if nome_limpo_planilha in nome_limpo_db or nome_limpo_db in nome_limpo_planilha:
                    cliente_id = c['id']
                    break
            
            # Auto-Cadastro
            if not cliente_id:
                conn.execute("INSERT INTO clientes (nome, tipo) VALUES (?, 'prefeitura')", (cliente_nome_planilha,))
                cliente_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                clientes_db.append({'id': cliente_id, 'nome': cliente_nome_planilha})

            try:
                valor_str = str(d.get('valor_total') or '0').replace('R$', '').replace(' ', '')
                if ',' in valor_str and '.' in valor_str: valor_str = valor_str.replace('.', '').replace(',', '.') 
                elif ',' in valor_str: valor_str = valor_str.replace(',', '.') 
                    
                valor_total = float(valor_str)
                data_emissao = str(d.get('data_emissao') or '')
                status = str(d.get('status') or 'emitida').strip().lower()

                conn.execute("""
                    INSERT INTO notas_fiscais (numero_nota, data_emissao, cliente_id, valor_total, status)
                    VALUES (?, ?, ?, ?, ?)
                """, (numero_nota, data_emissao, cliente_id, valor_total, status))
                sucesso += 1
            except sqlite3.IntegrityError:
                erros.append(f"Linha {linha}: A nota '{numero_nota}' já existe.")
            except Exception as e:
                erros.append(f"Linha {linha}: Valores inválidos ({str(e)}).")

        conn.commit()
    except Exception as e:
        conn.rollback()
        return jsonify({'erro': f'Erro fatal: {str(e)}'}), 500
    finally:
        conn.close()

    if erros: return jsonify({'erro': f"{sucesso} notas importadas. Falhas:\n" + "\n".join(erros)}), 400
    return jsonify({'mensagem': f'{sucesso} notas importadas com sucesso! Novos clientes foram cadastrados, se necessário.'}), 201

# ===========================================================================
# CUSTOS E PAGAMENTOS (CRUD E IMPORTAÇÃO INTELIGENTE)
# ===========================================================================

@app.route('/api/pagamentos')
def api_pagamentos():
    conn = get_db()
    pagamentos = conn.execute("""
        SELECT pf.*, f.nome as fornecedor_nome, nf.numero_nota as nota_vinculada
        FROM pagamentos_fornecedores pf
        JOIN fornecedores f ON pf.fornecedor_id = f.id
        LEFT JOIN notas_fiscais nf ON pf.nota_id = nf.id
        ORDER BY pf.data_emissao DESC
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in pagamentos])

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

@app.route('/api/custos/detalhe/<int:id>', methods=['GET', 'PUT', 'DELETE'])
def api_custo_crud(id):
    conn = get_db()
    try:
        if request.method == 'GET':
            custo = conn.execute("SELECT * FROM pagamentos_fornecedores WHERE id = ?", (id,)).fetchone()
            if custo: 
                return jsonify(dict(custo))
            return jsonify({'erro': 'Custo não encontrado'}), 404

        elif request.method == 'DELETE':
            conn.execute('DELETE FROM pagamentos_fornecedores WHERE id = ?', (id,))
            conn.commit()
            return jsonify({'mensagem': 'Excluído com sucesso'})
            
        elif request.method == 'PUT':
            d = request.json
            if not d: return jsonify({'erro': 'Dados não enviados'}), 400
            
            v_compra = float(d.get('valor_compra') or 0)
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
            
            total_descontos = sum([
                funrural, taxa_coop, desconto_aipro, pg_caixa_entrega, pg_frete_coop, 
                pg_caixa_papelao, frete_cooaipro, pg_extra_paa, pg_frete_kits_mogi, 
                pg_ref_uso_hr, pg_ref_uso_ford, ref_frete_entrega_poa, pg_afranio, 
                pg_frete_mogi, vlr_pago_combustivel, frete_entrega_sao_jose, 
                pg_uso_camara_fria, pg_doacao_produtor, pg_extra, adiantamento, descontos_outros
            ])
            
            v_liq = v_compra - total_descontos

            conn.execute("""
                UPDATE pagamentos_fornecedores SET
                    nota_id = ?, fornecedor_id = ?, data_emissao = ?,
                    numero_nota_fornecedor = ?, status = ?, produto_descricao = ?,
                    peso_liquido = ?, valor_compra = ?, funrural = ?,
                    taxa_cooperativa = ?, desconto_aipro = ?, pg_caixa_entrega = ?,
                    pg_frete_coop = ?, pg_caixa_papelao = ?, frete_cooaipro = ?,
                    pg_extra_paa = ?, pg_frete_kits_mogi = ?, pg_ref_uso_hr = ?,
                    pg_ref_uso_ford = ?, ref_frete_entrega_poa = ?, pg_afranio = ?,
                    pg_frete_mogi = ?, vlr_pago_combustivel = ?, frete_entrega_sao_jose = ?,
                    pg_uso_camara_fria = ?, pg_doacao_produtor = ?, pg_extra = ?,
                    adiantamento = ?, descontos_outros = ?, valor_liquido = ?
                WHERE id = ?
            """, (
                int(d['nota_id']), int(d['fornecedor_id']), d['data_emissao'],
                d.get('numero_nota_fornecedor'), d.get('status', 'pendente'),
                d.get('produto_descricao'), float(d.get('peso_liquido') or 0),
                v_compra, funrural, taxa_coop, desconto_aipro, pg_caixa_entrega,
                pg_frete_coop, pg_caixa_papelao, frete_cooaipro, pg_extra_paa,
                pg_frete_kits_mogi, pg_ref_uso_hr, pg_ref_uso_ford, ref_frete_entrega_poa,
                pg_afranio, pg_frete_mogi, vlr_pago_combustivel, frete_entrega_sao_jose,
                pg_uso_camara_fria, pg_doacao_produtor, pg_extra, adiantamento,
                descontos_outros, v_liq, id
            ))
            conn.commit()
            return jsonify({'mensagem': 'Atualizado com sucesso'})
    except Exception as e:
        conn.rollback()
        return jsonify({'erro': str(e)}), 400
    finally:
        conn.close()

@app.route('/api/custos', methods=['POST'])
def api_criar_custo():
    d = request.json
    if not d: return jsonify({'erro': 'Dados não enviados'}), 400

    for c in ['nota_id', 'fornecedor_id', 'data_emissao', 'valor_compra']:
        if not d.get(c): return jsonify({'erro': f'Campo obrigatório: {c}'}), 400

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

    total_descontos = sum([funrural, taxa_coop, desconto_aipro, pg_caixa_entrega, pg_frete_coop, pg_caixa_papelao, frete_cooaipro, pg_extra_paa, pg_frete_kits_mogi, pg_ref_uso_hr, pg_ref_uso_ford, ref_frete_entrega_poa, pg_afranio, pg_frete_mogi, vlr_pago_combustivel, frete_entrega_sao_jose, pg_uso_camara_fria, pg_doacao_produtor, pg_extra, adiantamento, descontos_outros])
    valor_liquido = valor_compra - total_descontos

    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO pagamentos_fornecedores (
                nota_id, fornecedor_id, data_emissao, prefeitura, produto_descricao,
                numero_nota_fornecedor, peso_liquido, valor_compra, funrural, taxa_cooperativa, desconto_aipro,
                pg_caixa_entrega, pg_frete_coop, pg_caixa_papelao, frete_cooaipro, pg_extra_paa, pg_frete_kits_mogi, pg_ref_uso_hr, pg_ref_uso_ford, ref_frete_entrega_poa, pg_afranio, pg_frete_mogi, vlr_pago_combustivel, frete_entrega_sao_jose, pg_uso_camara_fria, pg_doacao_produtor, pg_extra, adiantamento, descontos_outros, valor_liquido, status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            int(d['nota_id']), int(d['fornecedor_id']), d['data_emissao'], d.get('prefeitura'), d.get('produto_descricao'),
            d.get('numero_nota_fornecedor'), float(d.get('peso_liquido') or 0), valor_compra, funrural, taxa_coop, desconto_aipro, pg_caixa_entrega, pg_frete_coop, pg_caixa_papelao, frete_cooaipro, pg_extra_paa, d.get('pg_frete_kits_mogi') or 0, pg_ref_uso_hr, pg_ref_uso_ford, ref_frete_entrega_poa, pg_afranio, pg_frete_mogi, vlr_pago_combustivel, frete_entrega_sao_jose, pg_uso_camara_fria, pg_doacao_produtor, pg_extra, adiantamento, descontos_outros, valor_liquido, d.get('status', 'pendente')
        ))
        conn.commit()
        custo_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return jsonify({'id': custo_id, 'valor_liquido': valor_liquido, 'mensagem': 'Custo cadastrado com sucesso'}), 201
    except Exception as e:
        return jsonify({'erro': str(e)}), 400
    finally:
        conn.close()

@app.route('/api/pagamentos/importar', methods=['POST'])
def api_importar_pagamentos():
    dados = request.json
    if not dados or not isinstance(dados, list): return jsonify({'erro': 'Nenhum dado válido recebido.'}), 400

    conn = get_db()
    sucesso = 0
    erros = []
    
    fornecedores_db_raw = conn.execute("SELECT id, nome FROM fornecedores").fetchall()
    fornecedores_db = [{'id': f['id'], 'nome': f['nome']} for f in fornecedores_db_raw]

    colunas_desconto = [
        'funrural', 'taxa_cooperativa', 'desconto_aipro', 'pg_caixa_entrega', 'pg_frete_coop', 'pg_caixa_papelao', 'frete_cooaipro', 'pg_extra_paa', 'pg_frete_kits_mogi', 'pg_ref_uso_hr', 'pg_ref_uso_ford', 'ref_frete_entrega_poa', 'pg_afranio', 'pg_frete_mogi', 'vlr_pago_combustivel', 'frete_entrega_sao_jose', 'pg_uso_camara_fria', 'pg_doacao_produtor', 'pg_extra', 'adiantamento', 'descontos_outros'
    ]

    def parse_moeda(valor):
        if not valor or pd.isna(valor): return 0.0
        v = str(valor).replace('R$', '').replace(' ', '')
        if ',' in v and '.' in v: v = v.replace('.', '').replace(',', '.')
        elif ',' in v: v = v.replace(',', '.')
        try: return float(v)
        except: return 0.0

    try:
        for idx, d in enumerate(dados):
            linha = idx + 2
            
            forn_nome_planilha = str(d.get('fornecedor') or d.get('fornecedor_nome') or '').strip()
            if not forn_nome_planilha:
                erros.append(f"Linha {linha}: O nome do fornecedor é obrigatório.")
                continue

            nome_limpo_planilha = simplificar_texto(forn_nome_planilha)
            fornecedor_id = None
            for f in fornecedores_db:
                nome_limpo_db = simplificar_texto(f['nome'])
                if nome_limpo_planilha in nome_limpo_db or nome_limpo_db in nome_limpo_planilha:
                    fornecedor_id = f['id']
                    break
                    
            # Auto-Cadastro
            if not fornecedor_id:
                conn.execute("INSERT INTO fornecedores (nome) VALUES (?)", (forn_nome_planilha,))
                fornecedor_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                fornecedores_db.append({'id': fornecedor_id, 'nome': forn_nome_planilha})

            nota_id = None
            num_nota = str(d.get('nota_vinculada') or d.get('numero_nota') or '').strip()
            if num_nota and num_nota.lower() != 'nan':
                nota_row = conn.execute("SELECT id FROM notas_fiscais WHERE numero_nota = ?", (num_nota,)).fetchone()
                if nota_row: nota_id = nota_row['id']

            data_emissao = str(d.get('data_emissao') or '')
            valor_compra = parse_moeda(d.get('valor_compra') or d.get('bruto'))
            status = str(d.get('status') or 'pendente').strip().lower()
            produto = str(d.get('produto') or d.get('produto_descricao') or '')

            if valor_compra <= 0:
                erros.append(f"Linha {linha}: Valor da compra não preenchido ou inválido.")
                continue

            valores_desc = {k: parse_moeda(d.get(k)) for k in colunas_desconto}
            total_descontos = sum(valores_desc.values())
            valor_liquido = valor_compra - total_descontos

            try:
                chaves = ', '.join(colunas_desconto)
                interrogacoes = ', '.join(['?'] * len(colunas_desconto))
                
                conn.execute(f"""
                    INSERT INTO pagamentos_fornecedores (
                        fornecedor_id, nota_id, data_emissao, produto_descricao, valor_compra,
                        valor_liquido, status, {chaves}
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, {interrogacoes})
                """, (fornecedor_id, nota_id, data_emissao, produto, valor_compra, valor_liquido, status, *valores_desc.values()))
                sucesso += 1
                
            except Exception as e:
                erros.append(f"Linha {linha}: Erro ao salvar ({str(e)}).")

        conn.commit()
    except Exception as e:
        conn.rollback()
        return jsonify({'erro': f'Erro fatal ao processar: {str(e)}'}), 500
    finally:
        conn.close()

    if erros: return jsonify({'erro': f"{sucesso} pagamentos importados. Falhas:\n" + "\n".join(erros)}), 400
    return jsonify({'mensagem': f'{sucesso} pagamentos importados com sucesso! Novos fornecedores foram cadastrados, se necessário.'}), 201

# ===========================================================================
# BANCO DE DADOS (DOWNLOAD E UPLOAD)
# ===========================================================================

@app.route('/api/banco/download')
@login_required
def api_banco_download():
    if current_user.tipo != 'cooperativa':
        return jsonify({'erro': 'Acesso restrito à cooperativa.'}), 403
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), DATABASE)
    if not os.path.exists(db_path): return jsonify({'erro': 'Banco não encontrado'}), 404
    return send_file(db_path, as_attachment=True, download_name='coaipro.db')

@app.route('/api/banco/upload', methods=['POST'])
@login_required
def api_banco_upload():
    if current_user.tipo != 'cooperativa':
        return jsonify({'erro': 'Acesso restrito à cooperativa.'}), 403
    if 'arquivo' not in request.files: return jsonify({'erro': 'Nenhum arquivo enviado'}), 400
    arquivo = request.files['arquivo']
    if arquivo.filename == '': return jsonify({'erro': 'Arquivo vazio'}), 400

    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), DATABASE)
    if os.path.exists(db_path): shutil.copy2(db_path, db_path + '.backup')

    try:
        arquivo.save(db_path)
        return jsonify({'mensagem': 'Banco de dados atualizado com sucesso!'}), 200
    except Exception as e:
        if os.path.exists(db_path + '.backup'): shutil.copy2(db_path + '.backup', db_path)
        return jsonify({'erro': str(e)}), 500

@app.route('/api/banco/excel')
@login_required
def api_banco_excel():
    if current_user.tipo != 'cooperativa':
        return jsonify({'erro': 'Acesso restrito à cooperativa.'}), 403
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), DATABASE)
    conn = sqlite3.connect(db_path)
    try:
        tabelas = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
        views = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'").fetchall()]

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            for t in tabelas + views:
                try:
                    df = pd.read_sql_query(f'SELECT * FROM "{t}"', conn)
                    df.to_excel(writer, sheet_name=t[:31], index=False)
                except Exception: pass
        output.seek(0)
    finally:
        conn.close()
    return send_file(output, as_attachment=True, download_name='plantar_banco_completo.xlsx', mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

@app.route('/api/banco/tabelas')
@login_required
def api_banco_tabelas():
    if current_user.tipo != 'cooperativa':
        return jsonify({'erro': 'Acesso restrito à cooperativa.'}), 403
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), DATABASE)
    conn = sqlite3.connect(db_path)
    items = []
    try:
        for tipo in ['table', 'view']:
            rows = conn.execute(f"SELECT name FROM sqlite_master WHERE type='{tipo}' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
            for r in rows:
                nome = r[0]
                try: cnt = conn.execute(f'SELECT COUNT(*) FROM "{nome}"').fetchone()[0]
                except: cnt = 0
                items.append({'nome': nome, 'tipo': tipo, 'registros': cnt})
    finally:
        conn.close()
    return jsonify(items)

@app.route('/api/banco/tabela/<nome>')
@login_required
def api_banco_tabela_dados(nome):
    if current_user.tipo != 'cooperativa':
        return jsonify({'erro': 'Acesso restrito à cooperativa.'}), 403
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), DATABASE)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(f'SELECT * FROM "{nome}"').fetchall()
        if rows:
            colunas = list(rows[0].keys())
            dados = [dict(r) for r in rows]
        else:
            cur = conn.execute(f'PRAGMA table_info("{nome}")')
            info = cur.fetchall()
            colunas = [dict(c)['name'] for c in info] if info else []
            dados = []
        return jsonify({'colunas': colunas, 'dados': dados, 'total': len(dados)})
    except Exception as e:
        return jsonify({'erro': str(e)}), 400
    finally:
        conn.close()

if __name__ == '__main__':
   app.run(host="0.0.0.0", port=5000)