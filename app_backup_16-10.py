from flask import Flask, jsonify, render_template, request, send_file, redirect, url_for
from flask_cors import CORS
import psycopg2
import psycopg2.extras
import subprocess
import pandas as pd
import os
import io
import hmac
from functools import wraps
import re
import unicodedata
import tempfile
import xml.etree.ElementTree as ET
from datetime import date
from werkzeug.utils import secure_filename
import warnings
from processador_danfe import parse_danfe_pdf, reconciliar_notas

# Suprime o aviso do pandas para manter o terminal limpo
warnings.filterwarnings('ignore', category=UserWarning, module='pandas')

# Carrega variáveis de ambiente
from dotenv import load_dotenv
load_dotenv(encoding='utf-8')

# Importa o leitor de PDF
import pdfplumber

from processador_coaipro import (
    is_primeira_execucao,
    carregar_planilhas_modelo,
    garantir_banco_criado,
)
from seed_loader import migrar_seed_tabelas
from cotacoes_service import get_todas_cotacoes

from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash

# Inicializa o app Flask
app = Flask(__name__)
CORS(app)

# Segredos vêm do .env (nunca deixar fixos no código)
_secret = os.environ.get('SECRET_KEY')
if not _secret:
    import secrets as _secrets
    _secret = _secrets.token_hex(32)
    print("AVISO: SECRET_KEY não definida no .env. Usando chave temporária "
          "(todos os logins caem a cada reinício do servidor).")
app.config['SECRET_KEY'] = _secret
# Se vazio, o cadastro de novas contas de cooperativa fica desativado
app.config['CODIGO_CADASTRO_COOPERATIVA'] = os.environ.get('CODIGO_CADASTRO_COOPERATIVA', '')

# Configuração de Autenticação
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Configuração do Banco de Dados
DB_CONFIG = {
    'host': os.environ.get('DB_HOST', 'localhost'),
    'port': os.environ.get('DB_PORT', '5432'),
    'dbname': os.environ.get('DB_NAME', 'coaipro'),
    'user': os.environ.get('DB_USER', 'postgres'),
    'password': os.environ.get('DB_PASSWORD', ''),
}
DATABASE = DB_CONFIG['dbname']

# ===========================================================================
# CLASSES DE CONEXÃO COM O BANCO
# ===========================================================================
class _PGCursorWrapper:
    def __init__(self, cur):
        self._cur = cur
    def fetchone(self):
        return self._cur.fetchone()
    def fetchall(self):
        return self._cur.fetchall()
    def __getattr__(self, name):
        return getattr(self._cur, name)

class PGConnection:
    def __init__(self, conn):
        self._conn = conn
    def execute(self, query, params=()):
        query = query.replace('?', '%s')
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(query, params)
        return _PGCursorWrapper(cur)
    def commit(self):
        self._conn.commit()
    def rollback(self):
        self._conn.rollback()
    def close(self):
        self._conn.close()
    def raw(self):
        return self._conn

def get_db():
    conn = psycopg2.connect(**DB_CONFIG)
    return PGConnection(conn)

# ===========================================================================
# AUTENTICAÇÃO E USUÁRIOS
# ===========================================================================
class Usuario(UserMixin):
    def __init__(self, id, email, tipo, cooperativa_id, fornecedor_id, aprovado):
        self.id = id
        self.email = email
        self.tipo = tipo
        self.cooperativa_id = cooperativa_id
        self.fornecedor_id = fornecedor_id
        self.aprovado = aprovado

@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM usuarios WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if row:
        return Usuario(row['id'], row['email'], row['tipo'], row['cooperativa_id'],
                       row['fornecedor_id'], row['aprovado'])
    return None

# ===========================================================================
# FUNÇÕES AUXILIARES E PARSERS (XML e PDF)
# ===========================================================================
def cooperativa_required(f):
    """Exige administrador autenticado e associado a um tenant."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({'erro': 'Não autenticado. Faça login.'}), 401
        if current_user.tipo != 'admin' or not current_user.cooperativa_id:
            return jsonify({'erro': 'Acesso restrito ao administrador da cooperativa.'}), 403
        return f(*args, **kwargs)
    return wrapper

def _tenant_id():
    return current_user.cooperativa_id

def _tenant_exists(conn, table, row_id):
    """Confere que um ID recebido pela API pertence ao tenant atual."""
    allowed = {'clientes', 'fornecedores', 'produtos', 'notas_fiscais', 'pagamentos_fornecedores'}
    if table not in allowed:
        raise ValueError('Tabela não permitida')
    row = conn.execute(
        f"SELECT id FROM {table} WHERE id = ? AND cooperativa_id = ?",
        (row_id, _tenant_id())
    ).fetchone()
    return row is not None

def simplificar_texto(texto):
    if not texto: return ""
    texto_sem_acento = unicodedata.normalize('NFKD', str(texto)).encode('ASCII', 'ignore').decode('utf-8')
    texto_limpo = texto_sem_acento.lower()
    
    texto_limpo = texto_limpo.replace('pref.', 'prefeitura').replace('pref ', 'prefeitura ')
    texto_limpo = texto_limpo.replace('mun.', 'municipal').replace('mun ', 'municipal ')
    
    return re.sub(r'[^a-z0-9]', '', texto_limpo)

def truncar_para_coluna(texto, tamanho=100):
    if not texto:
        return texto
    texto = str(texto).strip()
    return texto[:tamanho].strip() if len(texto) > tamanho else texto

NFE_NS = {'nfe': 'http://www.portalfiscal.inf.br/nfe'}

def _txt(elemento, caminho, default=None):
    if elemento is None: return default
    achado = elemento.find(caminho, NFE_NS)
    return achado.text.strip() if achado is not None and achado.text else default

def parse_nfe_xml(caminho_arquivo):
    try:
        tree = ET.parse(caminho_arquivo)
    except ET.ParseError as e:
        raise ValueError(f'XML inválido ou corrompido: {e}')

    root = tree.getroot()
    inf_nfe = root.find('.//nfe:infNFe', NFE_NS)
    if inf_nfe is None:
        raise ValueError('Este arquivo não parece ser uma NF-e (tag infNFe não encontrada).')

    ide = inf_nfe.find('nfe:ide', NFE_NS)
    dest = inf_nfe.find('nfe:dest', NFE_NS)
    total = inf_nfe.find('nfe:total/nfe:ICMSTot', NFE_NS)
    transp = inf_nfe.find('nfe:transp/nfe:vol', NFE_NS)

    numero_nota = _txt(ide, 'nfe:nNF')
    if not numero_nota:
        raise ValueError('Número da nota (nNF) não encontrado no XML.')

    data_emissao_raw = _txt(ide, 'nfe:dhEmi') or _txt(ide, 'nfe:dEmi')
    data_emissao = data_emissao_raw[:10] if data_emissao_raw else None

    return {
        'chave_acesso': (inf_nfe.attrib.get('Id') or '').replace('NFe', ''),
        'numero_nota': numero_nota,
        'data_emissao': data_emissao,
        'cliente_nome': _txt(dest, 'nfe:xNome', default='Destinatário não identificado'),
        'valor_total': float(_txt(total, 'nfe:vNF', default='0') or 0),
        'valor_frete': float(_txt(total, 'nfe:vFrete', default='0') or 0),
        'peso_liquido': float(_txt(transp, 'nfe:pesoL', default='0') or 0),
    }

# ===========================================================================
# INICIALIZAÇÕES E MIGRAÇÕES
# ===========================================================================
def _inicializar_primeira_execucao():
    garantir_banco_criado(DB_CONFIG)
    if is_primeira_execucao(DB_CONFIG):
        migrar_seed_tabelas(DB_CONFIG)
        carregar_planilhas_modelo(DB_CONFIG)

def _migrar_cooperativas():
    conn = get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cooperativas (
                id SERIAL PRIMARY KEY,
                nome VARCHAR(150) NOT NULL,
                cnpj VARCHAR(20) UNIQUE,
                ativo BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS cooperativa_id INTEGER REFERENCES cooperativas(id)")
        conn.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS cooperativa_id INTEGER REFERENCES cooperativas(id)")
        conn.execute("ALTER TABLE fornecedores ADD COLUMN IF NOT EXISTS cooperativa_id INTEGER REFERENCES cooperativas(id)")
        conn.execute("ALTER TABLE produtos ADD COLUMN IF NOT EXISTS cooperativa_id INTEGER REFERENCES cooperativas(id)")
        conn.execute("ALTER TABLE notas_fiscais ADD COLUMN IF NOT EXISTS cooperativa_id INTEGER REFERENCES cooperativas(id)")
        conn.execute("ALTER TABLE pagamentos_fornecedores ADD COLUMN IF NOT EXISTS cooperativa_id INTEGER REFERENCES cooperativas(id)")
        conn.commit()
    finally:
        conn.close()

def _migrar_coluna_valor_contrato():
    conn = get_db()
    try:
        conn.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS valor_total_contrato DECIMAL(10,2) DEFAULT 0")
        conn.commit()
    finally:
        conn.close()

def _migrar_cliente_id_pagamentos():
    conn = get_db()
    try:
        conn.execute("ALTER TABLE pagamentos_fornecedores ADD COLUMN IF NOT EXISTS cliente_id INTEGER REFERENCES clientes(id)")
        conn.commit()
        conn.execute("""
            UPDATE pagamentos_fornecedores pf
            SET cliente_id = nf.cliente_id
            FROM notas_fiscais nf
            WHERE pf.nota_id = nf.id AND pf.cliente_id IS NULL
        """)
        conn.commit()
    finally:
        conn.close()

_inicializar_primeira_execucao()
_migrar_cooperativas()
_migrar_coluna_valor_contrato()
_migrar_cliente_id_pagamentos()

# ===========================================================================
# ROTAS FRONTEND (HTML)
# ===========================================================================
@app.route('/')
def landing():
    return render_template('landing.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        senha = request.form['senha']
        cooperativa_id = request.form.get('cooperativa_id') or None
        cooperativa_cnpj = request.form.get('cooperativa_cnpj', '').strip() or None

        conn = get_db()
        try:
            if cooperativa_id:
                row = conn.execute(
                    "SELECT * FROM usuarios WHERE email = ? AND cooperativa_id = ?",
                    (email, int(cooperativa_id))
                ).fetchone()
            elif cooperativa_cnpj:
                row = conn.execute(
                    """SELECT u.* FROM usuarios u
                       JOIN cooperativas c ON c.id = u.cooperativa_id
                       WHERE u.email = ? AND c.cnpj = ?""",
                    (email, cooperativa_cnpj)
                ).fetchone()
            else:
                rows = conn.execute(
                    "SELECT * FROM usuarios WHERE email = ? ORDER BY id",
                    (email,)
                ).fetchall()
                if len(rows) != 1:
                    return render_template(
                        'landing.html',
                        erro_login='Informe a cooperativa (ID ou CNPJ) para este email.'
                    )
                row = rows[0]
        finally:
            conn.close()

        if not row or not check_password_hash(row['senha_hash'], senha):
            return render_template('landing.html', erro_login='Email ou senha inválidos')

        usuario = Usuario(
            row['id'], row['email'], row['tipo'], row['cooperativa_id'],
            row['fornecedor_id'], row['aprovado']
        )
        login_user(usuario)
        return redirect(url_for('tabelas')) if usuario.tipo == 'admin' else redirect(url_for('meu_extrato'))
    return redirect(url_for('landing'))


@app.route('/cadastro', methods=['GET', 'POST'])
def cadastro():
    if request.method == 'POST':
        tipo = request.form.get('tipo', 'cooperado')
        if tipo == 'cooperativa':
            tipo = 'admin'

        email = request.form['email'].strip()
        senha = request.form['senha']
        conn = get_db()

        try:
            if tipo == 'admin':
                codigo_ok = app.config['CODIGO_CADASTRO_COOPERATIVA']
                if not codigo_ok or not hmac.compare_digest(
                    request.form.get('codigo_acesso', ''), codigo_ok
                ):
                    return render_template('landing.html', erro_cadastro='Código de acesso inválido.')

                nome_cooperativa = (
                    request.form.get('nome_cooperativa')
                    or request.form.get('nome')
                    or ''
                ).strip()
                cnpj = (
                    request.form.get('cnpj_cooperativa')
                    or request.form.get('cnpj')
                    or ''
                ).strip()

                if not nome_cooperativa or not cnpj:
                    return render_template(
                        'landing.html',
                        erro_cadastro='Nome e CNPJ da cooperativa são obrigatórios.'
                    )

                if conn.execute("SELECT 1 FROM cooperativas WHERE cnpj = ?", (cnpj,)).fetchone():
                    return render_template('landing.html', erro_cadastro='Esse CNPJ já está cadastrado.')

                coop = conn.execute(
                    """INSERT INTO cooperativas (nome, cnpj, ativo)
                       VALUES (?, ?, TRUE)
                       RETURNING id""",
                    (nome_cooperativa, cnpj)
                ).fetchone()
                cooperativa_id = coop['id']

                conn.execute(
                    """INSERT INTO usuarios
                       (cooperativa_id, email, senha_hash, tipo, fornecedor_id, aprovado, ativo)
                       VALUES (?, ?, ?, 'admin', NULL, TRUE, TRUE)""",
                    (cooperativa_id, email, generate_password_hash(senha))
                )
                conn.commit()
                return render_template('landing.html', sucesso_cadastro='Conta criada!')
            else:
                cpf_cnpj = request.form.get('cpf_cnpj', '').strip()
                cooperativa_id = request.form.get('cooperativa_id') or None
                cooperativa_cnpj = request.form.get('cnpj_cooperativa', '').strip() or None

                if not cpf_cnpj:
                    return render_template('landing.html', erro_cadastro='CPF/CNPJ é obrigatório.')

                if cooperativa_id:
                    fornecedor = conn.execute(
                        """SELECT id, cooperativa_id FROM fornecedores
                           WHERE cpf_cnpj = ? AND cooperativa_id = ?""",
                        (cpf_cnpj, int(cooperativa_id))
                    ).fetchone()
                elif cooperativa_cnpj:
                    fornecedor = conn.execute(
                        """SELECT f.id, f.cooperativa_id
                           FROM fornecedores f
                           JOIN cooperativas c ON c.id = f.cooperativa_id
                           WHERE f.cpf_cnpj = ? AND c.cnpj = ?""",
                        (cpf_cnpj, cooperativa_cnpj)
                    ).fetchone()
                else:
                    return render_template(
                        'landing.html',
                        erro_cadastro='Informe a cooperativa (ID ou CNPJ) para realizar o cadastro.'
                    )

                if not fornecedor:
                    return render_template('landing.html', erro_cadastro='CPF/CNPJ não encontrado nessa cooperativa.')

                if conn.execute(
                    """SELECT 1 FROM usuarios
                       WHERE email = ? AND cooperativa_id = ?""",
                    (email, fornecedor['cooperativa_id'])
                ).fetchone():
                    return render_template('landing.html', erro_cadastro='Esse email já está cadastrado nessa cooperativa.')

                conn.execute(
                    """INSERT INTO usuarios
                       (cooperativa_id, email, senha_hash, tipo, fornecedor_id, aprovado, ativo)
                       VALUES (?, ?, ?, 'cooperado', ?, TRUE, TRUE)""",
                    (fornecedor['cooperativa_id'], email, generate_password_hash(senha), fornecedor['id'])
                )
                conn.commit()
                return render_template('landing.html', sucesso_cadastro='Cadastro realizado com sucesso!')
        except Exception as e:
            conn.rollback()
            return render_template('landing.html', erro_cadastro=f'Erro ao realizar cadastro: {e}')
        finally:
            conn.close()
    return redirect(url_for('landing'))

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/painel/')
@login_required
def painel_raiz():
    if current_user.tipo != 'admin': return redirect(url_for('meu_extrato'))
    return redirect(url_for('tabelas'))

@app.route('/painel/tabelas')
@login_required
def tabelas():
    if current_user.tipo != 'admin': return redirect(url_for('meu_extrato'))
    conn = get_db()
    try:
        prefs_df = pd.read_sql_query("SELECT * FROM vw_resultados_por_prefeitura WHERE cooperativa_id = %s", conn.raw(), params=(current_user.cooperativa_id,))
        prefeituras = prefs_df.astype(object).where(pd.notna(prefs_df), None).to_dict(orient='records')

        coop_rows = conn.execute(
            "SELECT cc.cliente_id, f.nome, cc.cota FROM contrato_cooperado cc "
            "JOIN fornecedores f ON f.id = cc.fornecedor_id AND f.cooperativa_id = cc.cooperativa_id "
            "WHERE cc.cooperativa_id = ?",
            (current_user.cooperativa_id,)
        ).fetchall()
        coop_por_cliente = {}
        for r in coop_rows: coop_por_cliente.setdefault(r['cliente_id'], []).append({'nome': r['nome'], 'cota': float(r['cota'] or 0)})
        for p in prefeituras: p['cooperados'] = coop_por_cliente.get(p.get('id'), [])

        forns_df = pd.read_sql_query("SELECT * FROM vw_pagamentos_resumo WHERE cooperativa_id = %s", conn.raw(), params=(current_user.cooperativa_id,))
        fornecedores = forns_df.astype(object).where(pd.notna(forns_df), None).to_dict(orient='records')

        proj_rows = conn.execute("""
            SELECT cc.fornecedor_id, cc.cliente_id, c.nome as cliente_nome, cc.cota, COALESCE(SUM(pf.valor_compra), 0) as entregue
            FROM contrato_cooperado cc JOIN clientes c ON c.id = cc.cliente_id
            LEFT JOIN pagamentos_fornecedores pf ON pf.fornecedor_id = cc.fornecedor_id
                AND pf.cliente_id = cc.cliente_id AND pf.cooperativa_id = cc.cooperativa_id
            WHERE cc.cooperativa_id = ? AND c.cooperativa_id = ?
            GROUP BY cc.fornecedor_id, cc.cliente_id, c.nome, cc.cota
        """, (current_user.cooperativa_id, current_user.cooperativa_id)).fetchall()
        proj_por_fornecedor = {}
        for r in proj_rows: proj_por_fornecedor.setdefault(r['fornecedor_id'], []).append({'cliente_id': r['cliente_id'], 'cliente_nome': r['cliente_nome'], 'cota': float(r['cota'] or 0), 'falta': float(r['cota'] or 0) - float(r['entregue'] or 0)})
        for f in fornecedores: f['projetos'] = proj_por_fornecedor.get(f.get('id'), [])

        notas = conn.execute("""
            SELECT nf.*, c.nome as cliente_nome, COALESCE(cn.total_liquido_fornecedores, 0) as total_custos, (nf.valor_total - COALESCE(cn.total_liquido_fornecedores, 0)) as valor_liquido_nota
            FROM notas_fiscais nf JOIN clientes c ON nf.cliente_id = c.id AND c.cooperativa_id = nf.cooperativa_id
             LEFT JOIN vw_custos_por_nota cn ON cn.nota_id = nf.id AND cn.cooperativa_id = nf.cooperativa_id
             WHERE nf.cooperativa_id = ? ORDER BY nf.data_emissao DESC
        """, (current_user.cooperativa_id,)).fetchall()
        notas = [dict(n) for n in notas]

        pagamentos = conn.execute("""
            SELECT pf.*, f.nome as fornecedor_nome, nf.numero_nota as nota_vinculada
            FROM pagamentos_fornecedores pf JOIN fornecedores f ON pf.fornecedor_id = f.id AND f.cooperativa_id = pf.cooperativa_id
             LEFT JOIN notas_fiscais nf ON pf.nota_id = nf.id AND nf.cooperativa_id = pf.cooperativa_id
             WHERE pf.cooperativa_id = ? ORDER BY pf.data_emissao DESC
        """, (current_user.cooperativa_id,)).fetchall()
        pagamentos = [dict(p) for p in pagamentos]
    finally:
        conn.close()
    return render_template('tabelas.html', prefeituras=prefeituras, fornecedores=fornecedores, notas=notas, pagamentos=pagamentos)

@app.route('/painel/graficos')
@login_required
def graficos():
    if current_user.tipo != 'admin': return redirect(url_for('meu_extrato'))
    return render_template('graficos.html')

@app.route('/painel/visualizador')
@login_required
def visualizador():
    if current_user.tipo != 'admin': return redirect(url_for('meu_extrato'))
    return render_template('visualizador.html')

@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')

@app.route('/meu-extrato')
@login_required
def meu_extrato():
    if current_user.tipo != 'cooperado': return redirect(url_for('tabelas'))
    fornecedor_id = current_user.fornecedor_id
    if not fornecedor_id: return "Perfil incompleto", 400
    conn = get_db()
    try:
        resumo = conn.execute("SELECT * FROM vw_pagamentos_resumo WHERE id = ? AND cooperativa_id = ?", (fornecedor_id, current_user.cooperativa_id)).fetchone()
        pagamentos = conn.execute("""
            SELECT pf.data_emissao, pf.produto_descricao, nf.numero_nota as nota_vinculada, pf.valor_compra, pf.valor_liquido, pf.status
            FROM pagamentos_fornecedores pf LEFT JOIN notas_fiscais nf ON pf.nota_id = nf.id WHERE pf.fornecedor_id = ? AND pf.cooperativa_id = ? ORDER BY pf.data_emissao DESC
        """, (fornecedor_id, current_user.cooperativa_id)).fetchall()
    finally:
        conn.close()
    if not resumo: resumo = {'cota': 0, 'saldo': 0, 'total_pago': 0, 'pendente': 0}
    return render_template('meu_extrato.html', resumo=dict(resumo), pagamentos=[dict(p) for p in pagamentos])

@app.route('/cooperativa')
@login_required
def painel_cooperativa():
    if current_user.tipo != 'cooperado': return redirect(url_for('tabelas'))
    conn = get_db()
    fornecedor = conn.execute("SELECT nome FROM fornecedores WHERE id = ? AND cooperativa_id = ?", (current_user.fornecedor_id, current_user.cooperativa_id)).fetchone()
    conn.close()
    nome_cooperado = fornecedor['nome'] if fornecedor else 'Cooperado'
    cotacoes_do_dia = get_todas_cotacoes()
    avisos = []
    return render_template('cooperativa_visao_cooperado.html', cotacoes=cotacoes_do_dia, avisos=avisos, nome_cooperado=nome_cooperado)

# ===========================================================================
# API - CRUD CLIENTES E CONTRATOS
# ===========================================================================
@app.route('/api/clientes_lista')
@cooperativa_required
def api_clientes_lista():
    conn = get_db()
    rows = conn.execute("SELECT id, nome, tipo FROM clientes WHERE ativo = TRUE AND cooperativa_id = ? ORDER BY nome", (current_user.cooperativa_id,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/clientes', methods=['POST'])
@cooperativa_required
def api_criar_cliente():
    d = request.json
    nome = str(d.get('nome') or '').strip()
    if not nome: return jsonify({'erro': 'Nome é obrigatório'}), 400
    conn = get_db()
    try:
        row = conn.execute("INSERT INTO clientes (cooperativa_id, nome, tipo, cidade, prazo_pagamento_dias, valor_total_contrato, ativo) VALUES (?, ?, ?, ?, ?, ?, TRUE) RETURNING id",
            (current_user.cooperativa_id, nome, d.get('tipo', 'prefeitura'), d.get('cidade'), int(d.get('prazo_pagamento_dias') or 30), float(d.get('valor_total_contrato') or 0))).fetchone()
        conn.commit()
        return jsonify({'id': row['id'], 'mensagem': 'Cliente cadastrado com sucesso'}), 201
    except Exception as e: return jsonify({'erro': str(e)}), 400
    finally: conn.close()

@app.route('/api/clientes/<int:id>', methods=['PUT', 'DELETE', 'GET'])
@cooperativa_required
def api_cliente_crud(id):
    conn = get_db()
    try:
        if request.method == 'GET':
            return jsonify(dict(conn.execute("SELECT * FROM clientes WHERE id = ? AND cooperativa_id = ?", (id, current_user.cooperativa_id)).fetchone() or {}))
        elif request.method == 'DELETE':
            if conn.execute("SELECT COUNT(*) as t FROM notas_fiscais WHERE cliente_id = ? AND cooperativa_id = ?", (id, current_user.cooperativa_id)).fetchone()['t'] > 0:
                return jsonify({'erro': 'Existem notas vinculadas'}), 400
            conn.execute("DELETE FROM clientes WHERE id = ? AND cooperativa_id = ?", (id, current_user.cooperativa_id))
            conn.commit()
            return jsonify({'mensagem': 'Excluído com sucesso'})
        elif request.method == 'PUT':
            d = request.json
            conn.execute("UPDATE clientes SET nome=?, tipo=?, cidade=?, prazo_pagamento_dias=?, valor_total_contrato=? WHERE id=? AND cooperativa_id=?",
                         (d['nome'], d.get('tipo'), d.get('cidade'), int(d.get('prazo_pagamento_dias') or 30), float(d.get('valor_total_contrato') or 0), id, current_user.cooperativa_id))
            conn.commit()
            return jsonify({'mensagem': 'Atualizado com sucesso'})
    except Exception as e: return jsonify({'erro': str(e)}), 400
    finally: conn.close()

@app.route('/api/clientes/<int:id>/cooperados', methods=['GET', 'POST'])
@cooperativa_required
def api_contrato_cooperados(id):
    conn = get_db()
    try:
        if request.method == 'GET':
            rows = conn.execute("""
                SELECT cc.fornecedor_id, f.nome, cc.cota, COALESCE(SUM(pf.valor_compra), 0) as entregue
                FROM contrato_cooperado cc JOIN fornecedores f ON f.id = cc.fornecedor_id AND f.cooperativa_id = cc.cooperativa_id
                LEFT JOIN pagamentos_fornecedores pf ON pf.fornecedor_id = cc.fornecedor_id AND pf.cliente_id = cc.cliente_id AND pf.cooperativa_id = cc.cooperativa_id
                WHERE cc.cliente_id = ? AND cc.cooperativa_id = ? GROUP BY cc.fornecedor_id, f.nome, cc.cota
            """, (id, current_user.cooperativa_id)).fetchall()
            return jsonify([{'fornecedor_id': r['fornecedor_id'], 'nome': r['nome'], 'cota': float(r['cota'] or 0), 'entregue': float(r['entregue'] or 0), 'falta': float(r['cota'] or 0) - float(r['entregue'] or 0)} for r in rows])
        
        elif request.method == 'POST':
            d = request.json
            cota = float(d.get('cota') or 0)
            if not _tenant_exists(conn, 'fornecedores', d['fornecedor_id']):
                return jsonify({'erro': 'Fornecedor não pertence à cooperativa atual.'}), 400
            if not _tenant_exists(conn, 'clientes', id):
                return jsonify({'erro': 'Cliente não pertence à cooperativa atual.'}), 404
            conn.execute("INSERT INTO contrato_cooperado (cooperativa_id, cliente_id, fornecedor_id, cota) VALUES (?, ?, ?, ?) ON CONFLICT (cooperativa_id, cliente_id, fornecedor_id) DO UPDATE SET cota = EXCLUDED.cota", (current_user.cooperativa_id, id, d['fornecedor_id'], cota))
            conn.commit()
            return jsonify({'mensagem': 'Vinculado com sucesso'})
    except Exception as e: return jsonify({'erro': str(e)}), 400
    finally: conn.close()

@app.route('/api/clientes/<int:cliente_id>/cooperados/<int:fornecedor_id>', methods=['DELETE'])
@cooperativa_required
def api_remover_coop_contrato(cliente_id, fornecedor_id):
    conn = get_db()
    conn.execute("DELETE FROM contrato_cooperado WHERE cliente_id = ? AND fornecedor_id = ? AND cooperativa_id = ?", (cliente_id, fornecedor_id, current_user.cooperativa_id))
    conn.commit()
    conn.close()
    return jsonify({'mensagem': 'Removido do contrato'})

# ===========================================================================
# API - CRUD FORNECEDORES (COOPERADOS)
# ===========================================================================
@app.route('/api/fornecedores_lista')
@cooperativa_required
def api_fornecedores_lista():
    conn = get_db()
    rows = conn.execute("SELECT id, nome FROM fornecedores WHERE ativo = TRUE AND cooperativa_id = ? ORDER BY nome", (current_user.cooperativa_id,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/fornecedores', methods=['POST'])
@cooperativa_required
def api_criar_fornecedor():
    d = request.json
    if not d.get('nome'): return jsonify({'erro': 'Nome é obrigatório'}), 400
    conn = get_db()
    try:
        row = conn.execute("INSERT INTO fornecedores (cooperativa_id, nome, cpf_cnpj, cidade, ativo) VALUES (?, ?, ?, ?, TRUE) RETURNING id",
                           (current_user.cooperativa_id, d['nome'], d.get('cpf_cnpj'), d.get('cidade'))).fetchone()
        conn.commit()
        return jsonify({'id': row['id'], 'mensagem': 'Cadastrado com sucesso'}), 201
    except Exception as e: return jsonify({'erro': str(e)}), 400
    finally: conn.close()

@app.route('/api/fornecedores/<int:id>', methods=['GET', 'PUT', 'DELETE'])
@cooperativa_required
def api_fornecedor_crud(id):
    conn = get_db()
    try:
        if request.method == 'GET':
            return jsonify(dict(conn.execute("SELECT * FROM fornecedores WHERE id = ? AND cooperativa_id = ?", (id, current_user.cooperativa_id)).fetchone() or {}))
        elif request.method == 'DELETE':
            if conn.execute("SELECT COUNT(*) as t FROM pagamentos_fornecedores WHERE fornecedor_id = ? AND cooperativa_id = ?", (id, current_user.cooperativa_id)).fetchone()['t'] > 0:
                return jsonify({'erro': 'Existem pagamentos vinculados'}), 400
            conn.execute("DELETE FROM fornecedores WHERE id = ? AND cooperativa_id = ?", (id, current_user.cooperativa_id))
            conn.commit()
            return jsonify({'mensagem': 'Excluído'})
        elif request.method == 'PUT':
            d = request.json
            conn.execute("UPDATE fornecedores SET nome=?, cpf_cnpj=?, cidade=? WHERE id=? AND cooperativa_id=?", (d['nome'], d.get('cpf_cnpj'), d.get('cidade'), id, current_user.cooperativa_id))
            conn.commit()
            return jsonify({'mensagem': 'Atualizado'})
    except Exception as e: return jsonify({'erro': str(e)}), 400
    finally: conn.close()

@app.route('/api/fornecedores/<int:id>/contratos', methods=['GET'])
@cooperativa_required
def api_fornecedor_contratos(id):
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT cc.cliente_id, c.nome as cliente_nome, cc.cota, COALESCE(SUM(pf.valor_compra), 0) as entregue
            FROM contrato_cooperado cc JOIN clientes c ON c.id = cc.cliente_id AND c.cooperativa_id = cc.cooperativa_id
            LEFT JOIN pagamentos_fornecedores pf ON pf.fornecedor_id = cc.fornecedor_id AND pf.cliente_id = cc.cliente_id AND pf.cooperativa_id = cc.cooperativa_id
            WHERE cc.fornecedor_id = ? AND cc.cooperativa_id = ? GROUP BY cc.cliente_id, c.nome, cc.cota
        """, (id, current_user.cooperativa_id)).fetchall()
        return jsonify([{'cliente_id': r['cliente_id'], 'cliente_nome': r['cliente_nome'], 'cota': float(r['cota'] or 0), 'entregue': float(r['entregue'] or 0), 'falta': float(r['cota'] or 0) - float(r['entregue'] or 0)} for r in rows])
    except Exception as e:
        return jsonify({'erro': str(e)}), 400
    finally:
        conn.close()

# ===========================================================================
# API - NOTAS FISCAIS (IMPORTAÇÃO E LISTAGEM)
# ===========================================================================
@app.route('/api/notas/importar_pdf', methods=['POST'])
@cooperativa_required
def api_importar_pdf_nota():
    arquivos = request.files.getlist('arquivos_pdf') or request.files.getlist('arquivo')
    arquivos = [f for f in arquivos if f and f.filename]

    if not arquivos:
        return jsonify({'erro': 'Nenhum arquivo enviado'}), 400

    todas_notas = []
    try:
        for file in arquivos:
            fd, temp_path = tempfile.mkstemp(suffix='.pdf')
            os.close(fd)
            try:
                file.save(temp_path)
                todas_notas.extend(parse_danfe_pdf(temp_path))
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)

        resultado_reconciliacao = reconciliar_notas(todas_notas)

        def agrupar_por_nota(registros):
            grupos = {}
            for registro in registros:
                chave = registro.get('chave_acesso') or registro.get('numero_nota') or f"sem-chave-{len(grupos)}"
                grupo = grupos.setdefault(chave, dict(registro, itens=[]))
                grupo['itens'].append({
                    'produto': registro.get('produto') or '',
                    'quantidade': registro.get('quantidade') or 0.0,
                    'valor_unitario': registro.get('valor_unitario') or 0.0,
                    'valor_total': registro.get('valor_total') or 0.0,
                })
            return grupos

        grupos_compra = agrupar_por_nota(resultado_reconciliacao['compras'])
        grupos_venda = agrupar_por_nota(resultado_reconciliacao['vendas'])

        conn = get_db()
        pagamentos_gravados = 0
        notas_gravadas = 0
        vinculos_cota = 0
        avisos = []

        def resolver_cliente_por_nome(nome_bruto):
            if not nome_bruto: return None
            alvo = simplificar_texto(nome_bruto)
            if not alvo: return None
            candidatos = conn.execute("SELECT id, nome FROM clientes WHERE ativo = TRUE AND cooperativa_id = ?", (current_user.cooperativa_id,)).fetchall()
            for c in candidatos:
                if simplificar_texto(c['nome']) == alvo: return c['id']
            for c in candidatos:
                nome_c = simplificar_texto(c['nome'])
                if alvo in nome_c or nome_c in alvo: return c['id']
            return None

        try:
            for grupo in grupos_venda.values():
                numero_nota = grupo.get('numero_nota') or ''
                nome_cliente_bruto = grupo.get('contraparte_nome') or ''

                if not numero_nota:
                    avisos.append("Venda: nota sem número, não foi gravada.")
                    continue
                if not nome_cliente_bruto:
                    avisos.append(f"Venda, nota {numero_nota}: sem nome do cliente/prefeitura no PDF, não foi gravada.")
                    continue

                try:
                    ja_existe = conn.execute(
                        "SELECT id FROM notas_fiscais WHERE numero_nota = ? AND cooperativa_id = ?",
                        (numero_nota, current_user.cooperativa_id)
                    ).fetchone()
                    if ja_existe:
                        avisos.append(f"Venda, nota {numero_nota}: já importada anteriormente, pulada.")
                        conn.rollback()
                        continue

                    nome_cliente = truncar_para_coluna(grupo.get('contraparte_nome') or 'Cliente não identificado (importado via PDF)')
                    cliente_id_encontrado = resolver_cliente_por_nome(nome_cliente)
                    if cliente_id_encontrado:
                        cliente_id = cliente_id_encontrado
                    else:
                        cliente = conn.execute(
                            """INSERT INTO clientes (cooperativa_id, nome, tipo, ativo)
                               VALUES (?, ?, 'prefeitura', TRUE)
                               RETURNING id""",
                            (current_user.cooperativa_id, nome_cliente)
                        ).fetchone()
                        cliente_id = cliente['id']

                    data_emissao = grupo.get('data_emissao') or date.today().isoformat()
                    valor_total_nota = sum(i['valor_total'] for i in grupo['itens'])

                    nota = conn.execute(
                        """INSERT INTO notas_fiscais
                            (cooperativa_id, numero_nota, data_emissao, cliente_id, valor_total, status)
                           VALUES (?, ?, ?, ?, ?, 'emitida') RETURNING id""",
                        (current_user.cooperativa_id, numero_nota, data_emissao, cliente_id, valor_total_nota)
                    ).fetchone()
                    nota_id = nota['id']

                    for item in grupo['itens']:
                        nome_produto = truncar_para_coluna(item['produto'] or 'Produto não identificado')
                        produto = conn.execute(
                            "SELECT id FROM produtos WHERE nome = ? AND cooperativa_id = ?", (nome_produto, current_user.cooperativa_id)
                        ).fetchone()
                        if not produto:
                            produto = conn.execute(
                                """INSERT INTO produtos (cooperativa_id, nome, unidade_medida)
                                   VALUES (?, ?, ?)
                                   RETURNING id""",
                                (current_user.cooperativa_id, nome_produto, 'kg')
                            ).fetchone()
                        conn.execute(
                            """INSERT INTO nota_itens (cooperativa_id, nota_id, produto_id, quantidade, valor_unitario, valor_total)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (current_user.cooperativa_id, nota_id, produto['id'], item['quantidade'], item['valor_unitario'], item['valor_total'])
                        )

                    conn.commit()
                    notas_gravadas += 1
                except Exception as e:
                    conn.rollback()
                    avisos.append(f"Venda, nota {numero_nota or '?'}: erro ao gravar ({e}).")

            for grupo in grupos_compra.values():
                numero_nota = grupo.get('numero_nota') or ''
                cpf_fornecedor = grupo.get('cooperado_cpf') or grupo.get('destinatario_doc')
                if not cpf_fornecedor:
                    avisos.append(f"Compra, nota {numero_nota or '?'}: sem CPF/CNPJ do cooperado, não foi gravada.")
                    continue
                try:
                    fornecedor = conn.execute(
                        "SELECT id FROM fornecedores WHERE cpf_cnpj = ? AND cooperativa_id = ?", (cpf_fornecedor, current_user.cooperativa_id)
                    ).fetchone()
                    if not fornecedor:
                        nome_fornecedor = truncar_para_coluna(grupo.get('contraparte_nome') or 'Cooperado não identificado (importado via PDF)')
                        fornecedor = conn.execute(
                            """INSERT INTO fornecedores (cooperativa_id, nome, cpf_cnpj, ativo)
                               VALUES (?, ?, ?, TRUE)
                               RETURNING id""",
                            (current_user.cooperativa_id, nome_fornecedor, cpf_fornecedor)
                        ).fetchone()
                    fornecedor_id = fornecedor['id']

                    ja_existe = conn.execute(
                        "SELECT id FROM pagamentos_fornecedores WHERE fornecedor_id = ? AND numero_nota_fornecedor = ? AND cooperativa_id = ?",
                        (fornecedor_id, numero_nota, current_user.cooperativa_id)
                    ).fetchone() if numero_nota else None
                    if ja_existe:
                        avisos.append(f"Compra, nota {numero_nota}: já importada anteriormente, pulada.")
                        conn.rollback()
                        continue

                    nome_prefeitura_destino = grupo.get('cliente_destino_nome') or ''
                    cliente_id_pf = resolver_cliente_por_nome(nome_prefeitura_destino)

                    valor_compra = sum(i['valor_total'] for i in grupo['itens'])
                    funrural = grupo.get('funrural_valor') or 0.0
                    valor_liquido = grupo.get('valor_liquido') or (valor_compra - funrural)
                    data_emissao = grupo.get('data_emissao') or date.today().isoformat()
                    produto_descricao = ", ".join(dict.fromkeys(i['produto'] for i in grupo['itens'] if i['produto']))[:500]
                    prefeitura_texto = nome_prefeitura_destino or grupo.get('destinatario_doc') or ''

                    nota_id_vinculada = None
                    if cliente_id_pf:
                        nota_proxima = conn.execute(
                            """SELECT id FROM notas_fiscais nf WHERE nf.cliente_id = ? AND nf.cooperativa_id = ?
                               AND NOT EXISTS (
                                   SELECT 1 FROM pagamentos_fornecedores pf
                                   WHERE pf.nota_id = nf.id AND pf.cooperativa_id = nf.cooperativa_id
                               )
                               ORDER BY ABS(nf.data_emissao - ?::date) ASC LIMIT 1""",
                            (cliente_id_pf, current_user.cooperativa_id, data_emissao)
                        ).fetchone()
                        if not nota_proxima:
                            nota_proxima = conn.execute(
                                """SELECT id FROM notas_fiscais WHERE cliente_id = ? AND cooperativa_id = ?
                                   ORDER BY ABS(data_emissao - ?::date) ASC LIMIT 1""",
                                (cliente_id_pf, current_user.cooperativa_id, data_emissao)
                            ).fetchone()
                        if nota_proxima:
                            nota_id_vinculada = nota_proxima['id']
                        vinculos_cota += 1
                    else:
                        avisos.append(f"Compra, nota {numero_nota or '?'}: prefeitura '{nome_prefeitura_destino or '?'}' não encontrada no cadastro — pagamento gravado, mas SEM vínculo de cota.")

                    conn.execute(
                        """INSERT INTO pagamentos_fornecedores
                            (cooperativa_id, fornecedor_id, cliente_id, nota_id, data_emissao, prefeitura, produto_descricao,
                             numero_nota_fornecedor, valor_compra, funrural, valor_liquido, status)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pendente')""",
                        (current_user.cooperativa_id, fornecedor_id, cliente_id_pf, nota_id_vinculada, data_emissao, prefeitura_texto,
                         produto_descricao, numero_nota, valor_compra, funrural, valor_liquido)
                    )
                    conn.commit()
                    pagamentos_gravados += 1
                except Exception as e:
                    conn.rollback()
                    avisos.append(f"Compra, nota {numero_nota or '?'}: erro ao gravar ({e}).")
        finally:
            conn.close()

        return jsonify({
            'sucesso': True,
            'mensagem': f'{len(arquivos)} arquivo(s) processado(s): {pagamentos_gravados} pagamento(s) e {notas_gravadas} nota(s) de venda gravada(s).',
            'avisos': avisos
        }), 200

    except Exception as e:
        return jsonify({'erro': f'Erro ao processar PDF: {str(e)}'}), 400

@app.route('/api/notas_lista')
@cooperativa_required
def api_notas_lista():
    conn = get_db()
    rows = conn.execute("""
        SELECT nf.id, nf.numero_nota, c.nome as cliente_nome
        FROM notas_fiscais nf JOIN clientes c ON c.id = nf.cliente_id AND c.cooperativa_id = nf.cooperativa_id
        WHERE nf.cooperativa_id = ?
        ORDER BY nf.data_emissao DESC
    """, (current_user.cooperativa_id,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/notas', methods=['GET'])
@cooperativa_required
def api_listar_notas_detalhadas():
    conn = get_db()
    try:
        notas = conn.execute("""
            SELECT nf.*, c.nome as cliente_nome, 
                   COALESCE(cn.total_liquido_fornecedores, 0) as total_custos, 
                   (nf.valor_total - COALESCE(cn.total_liquido_fornecedores, 0)) as valor_liquido_nota
            FROM notas_fiscais nf 
            JOIN clientes c ON nf.cliente_id = c.id AND c.cooperativa_id = nf.cooperativa_id
            LEFT JOIN vw_custos_por_nota cn ON cn.nota_id = nf.id AND cn.cooperativa_id = nf.cooperativa_id
            WHERE nf.cooperativa_id = ? 
            ORDER BY nf.data_emissao DESC
        """, (current_user.cooperativa_id,)).fetchall()
        return jsonify(_limpar([dict(n) for n in notas]))
    finally:
        conn.close()

@app.route('/api/notas', methods=['POST'])
@cooperativa_required
def api_criar_nota():
    if current_user.tipo != 'admin': return jsonify({'erro': 'Acesso negado'}), 403
    d = request.json
    if not d.get('numero_nota') or not d.get('data_emissao') or not d.get('cliente_id') or not d.get('valor_total'):
        return jsonify({'erro': 'Preencha os campos obrigatórios'}), 400
    conn = get_db()
    try:
        if not _tenant_exists(conn, 'clientes', d['cliente_id']):
            return jsonify({'erro': 'Cliente não pertence à cooperativa atual.'}), 400
        row = conn.execute(
            """INSERT INTO notas_fiscais
                (cooperativa_id, numero_nota, data_emissao, cliente_id, valor_total, peso_liquido,
                 quantidade_cx, valor_frete, status, observacoes, previsao_pagamento)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               RETURNING id""",
            (current_user.cooperativa_id, d['numero_nota'], d['data_emissao'], d['cliente_id'], float(d.get('valor_total') or 0),
             float(d.get('peso_liquido') or 0), int(d.get('quantidade_cx') or 0),
             float(d.get('valor_frete') or 0), d.get('status', 'emitida'),
             d.get('observacoes'), d.get('previsao_pagamento') or None)
        ).fetchone()
        conn.commit()
        return jsonify({'id': row['id'], 'mensagem': 'Nota cadastrada com sucesso'}), 201
    except Exception as e:
        conn.rollback()
        return jsonify({'erro': str(e)}), 400
    finally:
        conn.close()

@app.route('/api/notas/<int:id>', methods=['GET', 'PUT', 'DELETE'])
@cooperativa_required
def api_nota_crud(id):
    if current_user.tipo != 'admin': return jsonify({'erro': 'Acesso negado'}), 403
    conn = get_db()
    try:
        if request.method == 'GET':
            row = conn.execute("SELECT * FROM notas_fiscais WHERE id = ? AND cooperativa_id = ?", (id, current_user.cooperativa_id)).fetchone()
            return jsonify(dict(row) if row else {})
        elif request.method == 'PUT':
            d = request.json
            if not _tenant_exists(conn, 'clientes', d['cliente_id']):
                return jsonify({'erro': 'Cliente não pertence à cooperativa atual.'}), 400
            conn.execute(
                """UPDATE notas_fiscais
                   SET numero_nota=?, data_emissao=?, cliente_id=?, valor_total=?, peso_liquido=?,
                       quantidade_cx=?, valor_frete=?, status=?, observacoes=?, previsao_pagamento=?
                   WHERE id=? AND cooperativa_id=?""",
                (d['numero_nota'], d['data_emissao'], d['cliente_id'], float(d.get('valor_total') or 0),
                 float(d.get('peso_liquido') or 0), int(d.get('quantidade_cx') or 0),
                 float(d.get('valor_frete') or 0), d.get('status'), d.get('observacoes'),
                 d.get('previsao_pagamento') or None, id, current_user.cooperativa_id)
            )
            conn.commit()
            return jsonify({'mensagem': 'Atualizado com sucesso'})
        elif request.method == 'DELETE':
            conn.execute("DELETE FROM nota_itens WHERE nota_id = ? AND cooperativa_id = ?", (id, current_user.cooperativa_id))
            conn.execute("UPDATE pagamentos_fornecedores SET nota_id = NULL WHERE nota_id = ? AND cooperativa_id = ?", (id, current_user.cooperativa_id))
            conn.execute("DELETE FROM notas_fiscais WHERE id = ? AND cooperativa_id = ?", (id, current_user.cooperativa_id))
            conn.commit()
            return jsonify({'mensagem': 'Nota fiscal excluída com sucesso'})
    except Exception as e:
        conn.rollback()
        return jsonify({'erro': str(e)}), 400
    finally:
        conn.close()

# ===========================================================================
# API - CRUD PAGAMENTOS/CUSTOS
# ===========================================================================
def _calcular_valor_liquido_custo(d) -> float:
    valor_compra = float(d.get('valor_compra') or 0)
    deducoes = sum(float(d.get(campo) or 0) for campo in (
        'funrural', 'taxa_cooperativa', 'desconto_aipro',
        'pg_frete_coop', 'pg_caixa_papelao', 'adiantamento', 'descontos_outros'
    ))
    return valor_compra - deducoes

@app.route('/api/custos', methods=['POST'])
@cooperativa_required
def api_criar_custo():
    if current_user.tipo != 'admin': return jsonify({'erro': 'Acesso negado'}), 403
    d = request.json
    if not d.get('nota_id') or not d.get('fornecedor_id') or not d.get('valor_compra'):
        return jsonify({'erro': 'Nota, fornecedor e valor da compra são obrigatórios'}), 400
    conn = get_db()
    try:
        nota = conn.execute("SELECT cliente_id FROM notas_fiscais WHERE id = ? AND cooperativa_id = ?", (d['nota_id'], current_user.cooperativa_id)).fetchone()
        if not nota: return jsonify({'erro': 'Nota não pertence à cooperativa atual.'}), 400
        if not _tenant_exists(conn, 'fornecedores', d['fornecedor_id']):
            return jsonify({'erro': 'Fornecedor não pertence à cooperativa atual.'}), 400
        cliente_id = nota['cliente_id']
        valor_liquido = _calcular_valor_liquido_custo(d)

        row = conn.execute(
            """INSERT INTO pagamentos_fornecedores
                (cooperativa_id, fornecedor_id, nota_id, cliente_id, data_emissao, numero_nota_fornecedor,
                 status, produto_descricao, peso_liquido, valor_compra, funrural,
                 taxa_cooperativa, desconto_aipro, pg_frete_coop, pg_caixa_papelao,
                 adiantamento, descontos_outros, valor_liquido)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               RETURNING id""",
            (current_user.cooperativa_id, d['fornecedor_id'], d['nota_id'], cliente_id, d.get('data_emissao'),
             d.get('numero_nota_fornecedor'), d.get('status', 'pendente'), d.get('produto_descricao'),
             float(d.get('peso_liquido') or 0), float(d.get('valor_compra') or 0), float(d.get('funrural') or 0),
             float(d.get('taxa_cooperativa') or 0), float(d.get('desconto_aipro') or 0),
             float(d.get('pg_frete_coop') or 0), float(d.get('pg_caixa_papelao') or 0),
             float(d.get('adiantamento') or 0), float(d.get('descontos_outros') or 0), valor_liquido)
        ).fetchone()
        conn.commit()
        return jsonify({'id': row['id'], 'valor_liquido': valor_liquido, 'mensagem': 'Custo lançado com sucesso'}), 201
    except Exception as e:
        conn.rollback()
        return jsonify({'erro': str(e)}), 400
    finally:
        conn.close()

@app.route('/api/custos/nota/<int:nota_id>', methods=['GET'])
@cooperativa_required
def api_custos_por_nota(nota_id):
    conn = get_db()
    try:
        custos = conn.execute("""
            SELECT pf.*, f.nome as fornecedor_nome
            FROM pagamentos_fornecedores pf
            JOIN fornecedores f ON pf.fornecedor_id = f.id AND f.cooperativa_id = pf.cooperativa_id
            WHERE pf.nota_id = ? AND pf.cooperativa_id = ?
            ORDER BY pf.data_emissao DESC
        """, (nota_id, current_user.cooperativa_id)).fetchall()
        return jsonify(_limpar([dict(c) for c in custos]))
    finally:
        conn.close()

@app.route('/api/custos/detalhe/<int:id>', methods=['GET', 'PUT', 'DELETE'])
@cooperativa_required
def api_custo_crud(id):
    if current_user.tipo != 'admin': return jsonify({'erro': 'Acesso negado'}), 403
    conn = get_db()
    try:
        if request.method == 'GET':
            row = conn.execute("SELECT * FROM pagamentos_fornecedores WHERE id = ? AND cooperativa_id = ?", (id, current_user.cooperativa_id)).fetchone()
            return jsonify(dict(row) if row else {})
        elif request.method == 'PUT':
            d = request.json
            if d.get('nota_id') and not _tenant_exists(conn, 'notas_fiscais', d['nota_id']):
                return jsonify({'erro': 'Nota não pertence à cooperativa atual.'}), 400
            if not _tenant_exists(conn, 'fornecedores', d['fornecedor_id']):
                return jsonify({'erro': 'Fornecedor não pertence à cooperativa atual.'}), 400
            valor_liquido = _calcular_valor_liquido_custo(d)
            conn.execute(
                """UPDATE pagamentos_fornecedores
                   SET nota_id=?, fornecedor_id=?, data_emissao=?, numero_nota_fornecedor=?, status=?,
                       produto_descricao=?, peso_liquido=?, valor_compra=?, funrural=?, taxa_cooperativa=?,
                       desconto_aipro=?, pg_frete_coop=?, pg_caixa_papelao=?, adiantamento=?,
                       descontos_outros=?, valor_liquido=?
                   WHERE id=? AND cooperativa_id=?""",
                (d.get('nota_id'), d.get('fornecedor_id'), d.get('data_emissao'), d.get('numero_nota_fornecedor'),
                 d.get('status'), d.get('produto_descricao'), float(d.get('peso_liquido') or 0),
                 float(d.get('valor_compra') or 0), float(d.get('funrural') or 0),
                 float(d.get('taxa_cooperativa') or 0), float(d.get('desconto_aipro') or 0),
                 float(d.get('pg_frete_coop') or 0), float(d.get('pg_caixa_papelao') or 0),
                 float(d.get('adiantamento') or 0), float(d.get('descontos_outros') or 0), valor_liquido, id, current_user.cooperativa_id)
            )
            conn.commit()
            return jsonify({'mensagem': 'Atualizado com sucesso'})
        elif request.method == 'DELETE':
            conn.execute("DELETE FROM pagamentos_fornecedores WHERE id = ? AND cooperativa_id = ?", (id, current_user.cooperativa_id))
            conn.commit()
            return jsonify({'mensagem': 'Excluído com sucesso'})
    except Exception as e:
        conn.rollback()
        return jsonify({'erro': str(e)}), 400
    finally:
        conn.close()

# ===========================================================================
# API DE RESUMO / GRÁFICOS
# ===========================================================================
from decimal import Decimal

def _limpar(v):
    if isinstance(v, Decimal): return float(v)
    if isinstance(v, dict): return {k: _limpar(x) for k, x in v.items()}
    if isinstance(v, list): return [_limpar(x) for x in v]
    return v

@app.route('/api/prefeituras')
@cooperativa_required
def api_prefeituras():
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM vw_resultados_por_prefeitura WHERE cooperativa_id = ?", (current_user.cooperativa_id,)).fetchall()
        return jsonify(_limpar([dict(r) for r in rows]))
    finally:
        conn.close()

@app.route('/api/fornecedores')
@cooperativa_required
def api_fornecedores():
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM vw_pagamentos_resumo WHERE cooperativa_id = ?", (current_user.cooperativa_id,)).fetchall()
        return jsonify(_limpar([dict(r) for r in rows]))
    finally:
        conn.close()

@app.route('/api/resumo')
@cooperativa_required
def api_resumo():
    conn = get_db()
    try:
        a_receber = conn.execute("SELECT COALESCE(SUM(valor_a_receber),0) AS v FROM vw_resultados_por_prefeitura WHERE cooperativa_id = ?", (current_user.cooperativa_id,)).fetchone()['v']
        a_pagar = conn.execute("SELECT COALESCE(SUM(valor_liquido),0) AS v FROM pagamentos_fornecedores WHERE status = 'pendente' AND cooperativa_id = ?", (current_user.cooperativa_id,)).fetchone()['v']
        notas_pend = conn.execute("SELECT COUNT(*) AS n FROM notas_fiscais WHERE status <> 'paga' AND cooperativa_id = ?", (current_user.cooperativa_id,)).fetchone()['n']
        pag_pend = conn.execute("SELECT COUNT(*) AS n FROM pagamentos_fornecedores WHERE status = 'pendente' AND cooperativa_id = ?", (current_user.cooperativa_id,)).fetchone()['n']
        return jsonify(_limpar({'totais': {
            'valor_a_receber': a_receber, 'valor_a_pagar': a_pagar,
            'notas_pendentes': notas_pend, 'pagamentos_pendentes': pag_pend}}))
    finally:
        conn.close()

# ===========================================================================
# FERRAMENTAS DE BANCO
# ===========================================================================
def _exige_cooperativa():
    if not current_user.is_authenticated or current_user.tipo != 'admin' or not current_user.cooperativa_id:
        return jsonify({'erro': 'Acesso restrito à cooperativa. Faça login.'}), 403
    return None

@app.route('/api/banco/tabelas', methods=['GET'])
@cooperativa_required
def api_banco_tabelas():
    tabelas_permitidas = [
        {'nome': 'vw_resultados_por_prefeitura', 'tipo': 'view', 'registros': '-'},
        {'nome': 'vw_pagamentos_resumo', 'tipo': 'view', 'registros': '-'},
        {'nome': 'clientes', 'tipo': 'table', 'registros': '-'},
        {'nome': 'fornecedores', 'tipo': 'table', 'registros': '-'},
        {'nome': 'notas_fiscais', 'tipo': 'table', 'registros': '-'},
        {'nome': 'pagamentos_fornecedores', 'tipo': 'table', 'registros': '-'}
    ]
    return jsonify(tabelas_permitidas)

@app.route('/api/banco/tabela/<nome>', methods=['GET'])
@cooperativa_required
def api_banco_tabela_dados(nome):
    permitidas = ['vw_resultados_por_prefeitura', 'vw_pagamentos_resumo', 'clientes', 'fornecedores', 'notas_fiscais', 'pagamentos_fornecedores']
    if nome not in permitidas:
        return jsonify({'erro': 'Tabela não permitida'}), 403
    conn = get_db()
    try:
        rows = conn.execute(f"SELECT * FROM {nome} WHERE cooperativa_id = ?", (current_user.cooperativa_id,)).fetchall()
        dados = _limpar([dict(r) for r in rows])
        colunas = list(dados[0].keys()) if dados else []
        return jsonify({'colunas': colunas, 'dados': dados, 'total': len(dados)})
    finally:
        conn.close()

@app.route('/api/banco/excel')
def api_banco_excel():
    negado = _exige_cooperativa()
    if negado: return negado
    consultas = {
        'Prefeituras': ("SELECT * FROM vw_resultados_por_prefeitura WHERE cooperativa_id = %s", (current_user.cooperativa_id,)),
        'Fornecedores': ("SELECT * FROM vw_pagamentos_resumo WHERE cooperativa_id = %s", (current_user.cooperativa_id,)),
        'Notas Fiscais': (
            "SELECT nf.*, c.nome AS cliente_nome FROM notas_fiscais nf "
            "JOIN clientes c ON c.id = nf.cliente_id AND c.cooperativa_id = nf.cooperativa_id "
            "WHERE nf.cooperativa_id = %s ORDER BY nf.data_emissao DESC",
            (current_user.cooperativa_id,)
        ),
        'Pagamentos': (
            "SELECT pf.*, f.nome AS fornecedor_nome FROM pagamentos_fornecedores pf "
            "JOIN fornecedores f ON f.id = pf.fornecedor_id AND f.cooperativa_id = pf.cooperativa_id "
            "WHERE pf.cooperativa_id = %s ORDER BY pf.data_emissao DESC",
            (current_user.cooperativa_id,)
        ),
    }
    conn = get_db()
    try:
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine='openpyxl') as writer:
            for aba, (sql, params) in consultas.items():
                pd.read_sql_query(sql, conn.raw(), params=params).to_excel(writer, sheet_name=aba, index=False)
        buf.seek(0)
        return send_file(buf, as_attachment=True,
                         download_name=f"kokue_{date.today().isoformat()}.xlsx",
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    except Exception as e:
        return jsonify({'erro': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/banco/download')
def api_banco_download():
    negado = _exige_cooperativa()
    if negado: return negado
    return jsonify({'erro': 'Backup completo do PostgreSQL é uma operação do sistema e não pode ser exposta a administradores de uma única cooperativa.'}), 403

@app.route('/api/banco/upload', methods=['POST'])
def api_banco_upload():
    negado = _exige_cooperativa()
    if negado: return negado
    return jsonify({'erro': 'Envio de banco desativado: o sistema agora usa PostgreSQL. Restaure backups (.sql) manualmente com psql.'}), 501


if __name__ == '__main__':
    app.run(debug=True)