from numeros import numero_decimal, numero_float
from flask import Flask, jsonify, render_template, request, send_file, redirect, url_for
from flask_cors import CORS
import psycopg2
import psycopg2.extras
import psycopg2.errors
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
from datetime import date, datetime
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
from conta_seguranca import instalar_minha_conta, validar_codigo, conferir_csrf_conta

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
# O código de segurança é definido por cada cooperativa; não há código global de convite.

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
    'sslmode': os.environ.get('DB_SSLMODE', 'prefer'),
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
        'itens': [{'descricao': _txt(det, 'nfe:prod/nfe:xProd', ''),
                   'unidade': _txt(det, 'nfe:prod/nfe:uCom', ''),
                   'quantidade': _txt(det, 'nfe:prod/nfe:qCom', '0'),
                   'valor_unitario': _txt(det, 'nfe:prod/nfe:vUnCom', '0'),
                   'valor_total': _txt(det, 'nfe:prod/nfe:vProd', '0')}
                  for det in inf_nfe.findall('nfe:det', NFE_NS)],
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

def _migrar_unique_contrato_cooperado():
    conn = get_db()
    try:
        conn.execute("ALTER TABLE contrato_cooperado DROP CONSTRAINT IF EXISTS contrato_cooperado_cliente_id_fornecedor_id_key")
        conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'contrato_cooperado_coop_cliente_forn_key') THEN
                    ALTER TABLE contrato_cooperado
                    ADD CONSTRAINT contrato_cooperado_coop_cliente_forn_key
                    UNIQUE (cooperativa_id, cliente_id, fornecedor_id);
                END IF;
            END $$;
        """)
        conn.commit()
    finally:
        conn.close()

_inicializar_primeira_execucao()
_migrar_cooperativas()
_migrar_coluna_valor_contrato()
_migrar_cliente_id_pagamentos()
_migrar_unique_contrato_cooperado()


def _migrar_referencia_contrato_pdf():
    conn = get_db()
    try:
        conn.execute("ALTER TABLE notas_fiscais ADD COLUMN IF NOT EXISTS contrato_referencia TEXT")
        conn.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS contrato_referencia TEXT")
        conn.commit()
    finally:
        conn.close()


_migrar_referencia_contrato_pdf()


def _migrar_itens_compra_venda():
    """Cria o detalhamento por produto necessário para conciliar compras e vendas."""
    conn = get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pagamento_itens (
                id SERIAL PRIMARY KEY,
                cooperativa_id INTEGER NOT NULL REFERENCES cooperativas(id) ON DELETE CASCADE,
                pagamento_id INTEGER NOT NULL REFERENCES pagamentos_fornecedores(id) ON DELETE CASCADE,
                produto_descricao TEXT NOT NULL,
                quantidade DECIMAL(12,3) NOT NULL DEFAULT 0,
                valor_unitario DECIMAL(12,2) NOT NULL DEFAULT 0,
                valor_total DECIMAL(12,2) NOT NULL DEFAULT 0,
                nota_item_id INTEGER UNIQUE REFERENCES nota_itens(id) ON DELETE SET NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pagamento_itens_pagamento ON pagamento_itens(cooperativa_id, pagamento_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pagamento_itens_nota_item ON pagamento_itens(nota_item_id)")
        conn.commit()
    finally:
        conn.close()


def _migrar_repasses_cooperados():
    """Garante a tabela de repasses (baixas) e a coluna data_pagamento em pagamentos_fornecedores."""
    conn = get_db()
    try:
        conn.execute("ALTER TABLE pagamentos_fornecedores ADD COLUMN IF NOT EXISTS data_pagamento DATE")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS repasses_cooperados (
                id SERIAL PRIMARY KEY,
                cooperativa_id INTEGER NOT NULL REFERENCES cooperativas(id) ON DELETE CASCADE,
                pagamento_id INTEGER NOT NULL REFERENCES pagamentos_fornecedores(id) ON DELETE CASCADE,
                fornecedor_id INTEGER REFERENCES fornecedores(id),
                valor DECIMAL(12,2) NOT NULL,
                data_pagamento DATE NOT NULL,
                forma_pagamento TEXT NOT NULL,
                referencia TEXT,
                observacoes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_repasses_pagamento ON repasses_cooperados(cooperativa_id, pagamento_id)")
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[migração repasses_cooperados] {e}")
    finally:
        conn.close()


def _normalizar_produto_conciliacao(nome):
    """Normaliza acentos, espaços e pontuação sem usar data como critério."""
    return simplificar_texto(nome or '')


def _reconciliar_itens_compra_venda(conn, cooperativa_id):
    """Vincula uma linha comprada a uma linha vendida somente por prefeitura, produto e valor."""
    compras = conn.execute("""
        SELECT pi.id AS pagamento_item_id, pi.pagamento_id, pi.produto_descricao,
               pi.valor_total, pf.cliente_id
        FROM pagamento_itens pi
        JOIN pagamentos_fornecedores pf ON pf.id = pi.pagamento_id
             AND pf.cooperativa_id = pi.cooperativa_id
        WHERE pi.cooperativa_id = ?
        ORDER BY pi.id
    """, (cooperativa_id,)).fetchall()

    vendas = conn.execute("""
        SELECT ni.id AS nota_item_id, ni.valor_total, p.nome AS produto,
               nf.cliente_id, nf.id AS nota_id
        FROM nota_itens ni
        JOIN notas_fiscais nf ON nf.id = ni.nota_id AND nf.cooperativa_id = ni.cooperativa_id
        JOIN produtos p ON p.id = ni.produto_id AND p.cooperativa_id = ni.cooperativa_id
        WHERE ni.cooperativa_id = ?
        ORDER BY nf.data_emissao, nf.id, ni.id
    """, (cooperativa_id,)).fetchall()

    # Recalcula do zero para também reconhecer vendas importadas depois das compras.
    conn.execute("UPDATE pagamento_itens SET nota_item_id = NULL WHERE cooperativa_id = ?", (cooperativa_id,))
    disponiveis = {}
    for venda in vendas:
        chave = (venda['cliente_id'], _normalizar_produto_conciliacao(venda['produto']), round(float(venda['valor_total'] or 0) * 100))
        if chave[1] and chave[2] > 0:
            disponiveis.setdefault(chave, []).append(venda)

    contagem_compras = {}
    for compra in compras:
        chave = (compra['cliente_id'], _normalizar_produto_conciliacao(compra['produto_descricao']), round(float(compra['valor_total'] or 0) * 100))
        contagem_compras[chave] = contagem_compras.get(chave, 0) + 1
    itens_por_pagamento = {}
    for compra in compras:
        cliente_id = compra['cliente_id']
        chave = (cliente_id, _normalizar_produto_conciliacao(compra['produto_descricao']), round(float(compra['valor_total'] or 0) * 100))
        candidatos = disponiveis.get(chave, []) if cliente_id is not None else []
        if chave[1] and len(candidatos) == 1 and contagem_compras.get(chave) == 1:
            venda = candidatos.pop(0)
            conn.execute("UPDATE pagamento_itens SET nota_item_id = ? WHERE id = ? AND cooperativa_id = ?",
                         (venda['nota_item_id'], compra['pagamento_item_id'], cooperativa_id))
            itens_por_pagamento.setdefault(compra['pagamento_id'], []).append(venda['nota_id'])
        itens_por_pagamento.setdefault(compra['pagamento_id'], [])

    # O campo legado nota_id só recebe valor quando todos os itens da compra
    # foram faturados na mesma nota; não força um vínculo arbitrário em compras parciais.
    pagamentos = conn.execute("""
        SELECT pf.id, COUNT(pi.id) AS total_itens,
               COUNT(pi.nota_item_id) AS itens_faturados,
               COUNT(DISTINCT nf.id) AS notas_distintas,
               MIN(nf.id) AS nota_id_unica
        FROM pagamentos_fornecedores pf
        JOIN pagamento_itens pi ON pi.pagamento_id = pf.id AND pi.cooperativa_id = pf.cooperativa_id
        LEFT JOIN nota_itens ni ON ni.id = pi.nota_item_id
        LEFT JOIN notas_fiscais nf ON nf.id = ni.nota_id AND nf.cooperativa_id = pi.cooperativa_id
        WHERE pf.cooperativa_id = ?
        GROUP BY pf.id
    """, (cooperativa_id,)).fetchall()
    for pagamento in pagamentos:
        nota_id = pagamento['nota_id_unica'] if (
            pagamento['total_itens'] > 0 and
            pagamento['itens_faturados'] == pagamento['total_itens'] and
            pagamento['notas_distintas'] == 1
        ) else None
        conn.execute("UPDATE pagamentos_fornecedores SET nota_id = ? WHERE id = ? AND cooperativa_id = ?",
                     (nota_id, pagamento['id'], cooperativa_id))


_migrar_itens_compra_venda()
_migrar_repasses_cooperados()

def _migrar_descontos_dashboard():
    conn = get_db()
    try:
        for campo in ['pg_caixa_entrega', 'frete_cooaipro', 'pg_extra_paa', 'pg_frete_kits_mogi', 'pg_ref_uso_hr', 'pg_ref_uso_ford', 'ref_frete_entrega_poa', 'pg_afranio', 'pg_frete_mogi', 'vlr_pago_combustivel', 'frete_entrega_sao_jose', 'pg_uso_camara_fria', 'pg_doacao_produtor', 'pg_extra']:
            conn.execute(f'ALTER TABLE pagamentos_fornecedores ADD COLUMN IF NOT EXISTS {campo} NUMERIC(14,2) DEFAULT 0')
        conn.commit()
    finally:
        conn.close()

_migrar_descontos_dashboard()


def _migrar_unicidade_notas_cooperativa():
    """Mantém a unicidade existente por cooperativa, removendo a regra global antiga."""
    conn = get_db()
    try:
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS unq_notas_cooperativa_numero ON notas_fiscais (cooperativa_id, numero_nota)')
        conn.execute('ALTER TABLE notas_fiscais DROP CONSTRAINT IF EXISTS notas_fiscais_numero_nota_key')
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

_migrar_unicidade_notas_cooperativa()



# SQL_CUSTO_NOTA = custo líquido devido aos cooperados vinculado à nota.
# Na apresentação financeira: líquido = valor devido vinculado; custos = NF menos líquido.
# Repasses efetivos são registrados separadamente.
# Custo de cada nota: rateia o líquido da compra pelos ITENS que casaram com a nota
# (funciona também quando a compra casou só parcialmente ou com mais de uma nota)
# + compras lançadas à mão (sem itens) ligadas direto pelo nota_id.
SQL_CUSTO_NOTA = """ROUND((
    COALESCE((SELECT SUM(CASE WHEN pfc.valor_compra > 0 THEN pic.valor_total / pfc.valor_compra * pfc.valor_liquido ELSE 0 END)
              FROM pagamento_itens pic
              JOIN nota_itens nic ON nic.id = pic.nota_item_id
              JOIN pagamentos_fornecedores pfc ON pfc.id = pic.pagamento_id AND pfc.cooperativa_id = pic.cooperativa_id
              WHERE nic.nota_id = nf.id AND pic.cooperativa_id = nf.cooperativa_id), 0)
  + COALESCE((SELECT SUM(pfm.valor_liquido) FROM pagamentos_fornecedores pfm
              WHERE pfm.nota_id = nf.id AND pfm.cooperativa_id = nf.cooperativa_id
                AND NOT EXISTS (SELECT 1 FROM pagamento_itens pim WHERE pim.pagamento_id = pfm.id AND pim.cooperativa_id = pfm.cooperativa_id)), 0)
)::numeric, 2)"""

# Sem compra vinculada, não transformar a NF inteira em desconto.
# Havendo vínculo, o líquido vem do rateio da compra importada.
SQL_LIQUIDO_NOTA = """CASE WHEN
    EXISTS (SELECT 1 FROM pagamento_itens pl
            JOIN nota_itens nl ON nl.id = pl.nota_item_id AND nl.cooperativa_id = pl.cooperativa_id
            WHERE nl.nota_id = nf.id AND pl.cooperativa_id = nf.cooperativa_id)
    OR EXISTS (SELECT 1 FROM pagamentos_fornecedores pf
               WHERE pf.nota_id = nf.id AND pf.cooperativa_id = nf.cooperativa_id
               AND NOT EXISTS (SELECT 1 FROM pagamento_itens pi WHERE pi.pagamento_id = pf.id AND pi.cooperativa_id = pf.cooperativa_id))
    THEN """ + SQL_CUSTO_NOTA + " ELSE nf.valor_total END"

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
        conferir_csrf_conta()
        tipo = request.form.get('tipo', 'cooperado')
        if tipo == 'cooperativa':
            tipo = 'admin'

        email = request.form['email'].strip()
        senha = request.form['senha']
        conn = get_db()

        try:
            if tipo == 'admin':
                codigo = request.form.get('codigo_seguranca', '')
                erro_codigo = validar_codigo(codigo, request.form.get('confirma_codigo', ''), senha)
                if erro_codigo:
                    return render_template('landing.html', erro_cadastro=erro_codigo)
                if not 8 <= len(senha) <= 128:
                    return render_template('landing.html', erro_cadastro='Escolha uma senha de 8 a 128 caracteres.')

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

                if not nome_cooperativa or len(nome_cooperativa) > 150 or len(cnpj) > 20 or len(re.sub(r'\D', '', cnpj)) != 14:
                    return render_template(
                        'landing.html',
                        erro_cadastro='Informe o nome da cooperativa (até 150 caracteres) e um CNPJ com 14 dígitos.'
                    )

                if conn.execute("SELECT 1 FROM cooperativas WHERE regexp_replace(cnpj, '[^0-9]', '', 'g') = ?", (re.sub(r'\D', '', cnpj),)).fetchone():
                    return render_template('landing.html', erro_cadastro='Esse CNPJ já está cadastrado.')

                coop = conn.execute(
                    """INSERT INTO cooperativas (nome, cnpj, ativo, codigo_seguranca_hash, codigo_seguranca_atualizado_em)
                       VALUES (?, ?, TRUE, ?, CURRENT_TIMESTAMP)
                       RETURNING id""",
                    (nome_cooperativa, cnpj, generate_password_hash(codigo))
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

@app.template_filter('brl')
def brl_filter(v):
    """Formata como R$ 1234,56 (mesmo padrão já usado nas tabelas)."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        n = 0.0
    if n != n:
        n = 0.0
    n = round(n, 2) + 0.0  # evita "-0,00"
    return 'R$ ' + f'{n:.2f}'.replace('.', ',')

@app.route('/painel/tabelas')
@login_required
def tabelas():
    if current_user.tipo != 'admin': return redirect(url_for('meu_extrato'))
    cid = current_user.cooperativa_id
    conn = get_db()
    try:
        # O valor contratado vem do cadastro; notas representam a execução.
        # Não inferir o orçamento do contrato a partir do que já foi faturado.
        prefeituras = [dict(r) for r in conn.execute("""
            SELECT c.id, c.nome AS prefeitura, c.tipo, c.cidade,
                   c.valor_total_contrato AS valor_total_contratos,
                   COALESCE(STRING_AGG(DISTINCT NULLIF(nf.contrato_referencia, ''), ', '), NULLIF(c.contrato_referencia, '')) AS contratos_referencias,
                   COUNT(nf.id) AS total_notas,
                   COALESCE(SUM(nf.valor_total), 0) AS valor_recebido,
                   CASE WHEN c.valor_total_contrato > 0
                        THEN c.valor_total_contrato - COALESCE(SUM(nf.valor_total), 0)
                        ELSE NULL END AS valor_a_receber
            FROM clientes c
            LEFT JOIN notas_fiscais nf
              ON nf.cliente_id = c.id AND nf.cooperativa_id = c.cooperativa_id
            WHERE c.cooperativa_id = ?
            GROUP BY c.id, c.nome, c.tipo, c.cidade, c.valor_total_contrato, c.contrato_referencia
            ORDER BY c.nome
        """, (cid,)).fetchall()]

        forns_df = pd.read_sql_query("SELECT * FROM vw_pagamentos_resumo WHERE cooperativa_id = %s", conn.raw(), params=(cid,))
        fornecedores = forns_df.astype(object).where(pd.notna(forns_df), None).to_dict(orient='records')

        notas = conn.execute("""
            SELECT nf.*, c.nome as cliente_nome, (nf.valor_total - {SQL_CUSTO_NOTA}) as total_custos, {SQL_CUSTO_NOTA} as valor_liquido_nota
            FROM notas_fiscais nf JOIN clientes c ON nf.cliente_id = c.id AND c.cooperativa_id = nf.cooperativa_id
             WHERE nf.cooperativa_id = ? ORDER BY nf.data_emissao DESC
        """.replace('{SQL_CUSTO_NOTA}', SQL_LIQUIDO_NOTA), (current_user.cooperativa_id,)).fetchall()
        notas = [dict(n) for n in notas]

        pagamentos = conn.execute("""
            SELECT pf.*, f.nome as fornecedor_nome, c.nome AS cliente_nome,
                   COALESCE((SELECT STRING_AGG(DISTINCT NULLIF(nfc.contrato_referencia, ''), ', ')
                             FROM pagamento_itens pic
                             JOIN nota_itens nic ON nic.id = pic.nota_item_id AND nic.cooperativa_id = pic.cooperativa_id
                             JOIN notas_fiscais nfc ON nfc.id = nic.nota_id AND nfc.cooperativa_id = pic.cooperativa_id
                             WHERE pic.pagamento_id = pf.id AND pic.cooperativa_id = pf.cooperativa_id),
                            nf.contrato_referencia) AS contratos_referencias,
                   COALESCE((SELECT STRING_AGG(DISTINCT nf2.numero_nota, ', ' ORDER BY nf2.numero_nota)
                             FROM pagamento_itens pi2
                             JOIN nota_itens ni2 ON ni2.id = pi2.nota_item_id
                             JOIN notas_fiscais nf2 ON nf2.id = ni2.nota_id AND nf2.cooperativa_id = pi2.cooperativa_id
                             WHERE pi2.pagamento_id = pf.id AND pi2.cooperativa_id = pf.cooperativa_id),
                            CASE WHEN pf.numero_nota_fornecedor IS NULL THEN nf.numero_nota ELSE NULL END) as nota_vinculada,
                   COALESCE((SELECT STRING_AGG(pi3.produto_descricao, ', ' ORDER BY pi3.id)
                             FROM pagamento_itens pi3 WHERE pi3.pagamento_id = pf.id AND pi3.cooperativa_id = pf.cooperativa_id
                             AND pi3.nota_item_id IS NULL),
                            CASE WHEN EXISTS (SELECT 1 FROM pagamento_itens pi4 WHERE pi4.pagamento_id = pf.id AND pi4.cooperativa_id = pf.cooperativa_id) THEN '' ELSE COALESCE(pf.produto_descricao, '') END) as produtos_sem_nota,
                   COALESCE((SELECT SUM(pi5.valor_total) FROM pagamento_itens pi5
                             WHERE pi5.pagamento_id = pf.id AND pi5.cooperativa_id = pf.cooperativa_id AND pi5.nota_item_id IS NULL),
                            CASE WHEN EXISTS (SELECT 1 FROM pagamento_itens pi6 WHERE pi6.pagamento_id = pf.id AND pi6.cooperativa_id = pf.cooperativa_id) THEN 0 ELSE pf.valor_compra END) as valor_sem_nota,
                   COALESCE((SELECT SUM(pi7.valor_total) FROM pagamento_itens pi7
                             WHERE pi7.pagamento_id = pf.id AND pi7.cooperativa_id = pf.cooperativa_id AND pi7.nota_item_id IS NOT NULL), 0) as valor_faturado
            FROM pagamentos_fornecedores pf
            LEFT JOIN clientes c ON c.id = pf.cliente_id AND c.cooperativa_id = pf.cooperativa_id
            JOIN fornecedores f ON pf.fornecedor_id = f.id AND f.cooperativa_id = pf.cooperativa_id
            LEFT JOIN notas_fiscais nf ON pf.nota_id = nf.id AND nf.cooperativa_id = pf.cooperativa_id
            WHERE pf.cooperativa_id = ? ORDER BY pf.data_emissao DESC
        """, (current_user.cooperativa_id,)).fetchall()
        pagamentos = [dict(p) for p in pagamentos]

        # ------------------------------------------------------------------
        # Valores efetivamente repassados (histórico de repasses).
        # Se a tabela ainda não existir, a tela avisa em vez de mostrar zeros calados.
        # ------------------------------------------------------------------
        rep_pagamento, rep_cooperado, rep_contrato, rep_vinculo = {}, {}, {}, {}
        repasses_indisponiveis = False
        try:
            rep_rows = conn.execute("""
                SELECT pf.id AS pagamento_id, pf.fornecedor_id, pf.cliente_id, COALESCE(SUM(r.valor), 0) AS total
                FROM repasses_cooperados r
                JOIN pagamentos_fornecedores pf ON pf.id = r.pagamento_id AND pf.cooperativa_id = r.cooperativa_id
                WHERE r.cooperativa_id = ?
                GROUP BY pf.id, pf.fornecedor_id, pf.cliente_id
            """, (cid,)).fetchall()
        except Exception:
            conn.rollback()
            rep_rows = []
            repasses_indisponiveis = True
        for r in rep_rows:
            total = float(r['total'] or 0)
            rep_pagamento[r['pagamento_id']] = total
            rep_cooperado[r['fornecedor_id']] = rep_cooperado.get(r['fornecedor_id'], 0.0) + total
            if r['cliente_id'] is not None:
                rep_contrato[r['cliente_id']] = rep_contrato.get(r['cliente_id'], 0.0) + total
                chave = (r['fornecedor_id'], r['cliente_id'])
                rep_vinculo[chave] = rep_vinculo.get(chave, 0.0) + total

        # ------------------------------------------------------------------
        # Vínculo cooperado x contrato: uma única fonte para as duas abas.
        # Vem da cota cadastrada no contrato E das entregas/custos lançados
        # (assim o cooperado aparece no projeto mesmo sem cota cadastrada).
        # ------------------------------------------------------------------
        nome_cliente = {r['id']: r['nome'] for r in conn.execute("SELECT id, nome FROM clientes WHERE cooperativa_id = ?", (cid,)).fetchall()}
        nome_cooperado = {r['id']: r['nome'] for r in conn.execute("SELECT id, nome FROM fornecedores WHERE cooperativa_id = ?", (cid,)).fetchall()}
        vinculos = {}
        def _vinculo(forn_id, cli_id):
            chave = (forn_id, cli_id)
            if chave not in vinculos:
                vinculos[chave] = {
                    'fornecedor_id': forn_id, 'fornecedor_nome': nome_cooperado.get(forn_id) or '-',
                    'cliente_id': cli_id, 'cliente_nome': nome_cliente.get(cli_id) or '-',
                    'cota': 0.0, 'devido': 0.0, 'entregue': 0.0, 'pago': rep_vinculo.get(chave, 0.0)
                }
            return vinculos[chave]

        for r in conn.execute("SELECT fornecedor_id, cliente_id, cota FROM contrato_cooperado WHERE cooperativa_id = ?", (cid,)).fetchall():
            _vinculo(r['fornecedor_id'], r['cliente_id'])['cota'] = float(r['cota'] or 0)

        liquido_por_cooperado = {}
        for p in pagamentos:
            p['valor_repassado'] = rep_pagamento.get(p['id'], 0.0)
            liquido_por_cooperado[p['fornecedor_id']] = liquido_por_cooperado.get(p['fornecedor_id'], 0.0) + float(p.get('valor_liquido') or 0)
            if p.get('cliente_id') is None:
                continue
            v = _vinculo(p['fornecedor_id'], p['cliente_id'])
            v['devido'] += float(p.get('valor_liquido') or 0)
            # Conta como entregue assim que a compra do cooperado é registrada
            # pra este contrato — não depende mais de casar item a item com
            # uma nota de venda específica (notas lançadas manualmente nem
            # sempre têm itens detalhados, o que deixava a cota travada).
            v['entregue'] += float(p.get('valor_compra') or 0)

        proj_por_fornecedor, coop_por_cliente = {}, {}
        for v in sorted(vinculos.values(), key=lambda x: ((x['cliente_nome'] or ''), (x['fornecedor_nome'] or ''))):
            v['falta'] = (v['cota'] - v['entregue']) if v['cota'] > 0 else None
            proj_por_fornecedor.setdefault(v['fornecedor_id'], []).append(v)
            coop_por_cliente.setdefault(v['cliente_id'], []).append(v)

        for p in prefeituras:
            p['cooperados'] = coop_por_cliente.get(p.get('id'), [])
            p['total_repassado'] = rep_contrato.get(p.get('id'), 0.0)
            p['total_devido'] = sum(v['devido'] for v in p['cooperados'])
            p['a_pagar'] = max(round(p['total_devido'] - p['total_repassado'], 2), 0.0)
            p['custos_vinculados'] = round(sum(float(n.get('total_custos') or 0) for n in notas if n['cliente_id'] == p['id']), 2)
            p['resultado_liquido'] = round(sum(float(n.get('valor_liquido_nota') or 0) for n in notas if n['cliente_id'] == p['id']), 2)

        for f in fornecedores:
            fid = f.get('id')
            projs = proj_por_fornecedor.get(fid, [])
            f['projetos'] = projs
            f['cota_total'] = sum(v['cota'] for v in projs)
            faltas = [v['falta'] for v in projs if v['falta'] is not None]
            f['saldo_entrega'] = sum(faltas) if faltas else None
            f['total_repassado'] = rep_cooperado.get(fid, 0.0)
            f['saldo_receber'] = round(liquido_por_cooperado.get(fid, 0.0) - f['total_repassado'], 2)
    finally:
        conn.close()
    return render_template('tabelas.html', prefeituras=prefeituras, fornecedores=fornecedores, notas=notas,
                           pagamentos=pagamentos, repasses_indisponiveis=repasses_indisponiveis)

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
@login_required
def dashboard():
    if current_user.tipo != 'admin': return redirect(url_for('meu_extrato'))
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
        row = conn.execute("INSERT INTO clientes (cooperativa_id, nome, tipo, cidade, prazo_pagamento_dias, valor_total_contrato, contrato_referencia, ativo) VALUES (?, ?, ?, ?, ?, ?, ?, TRUE) RETURNING id",
            (current_user.cooperativa_id, nome, d.get('tipo', 'prefeitura'), d.get('cidade'), int(d.get('prazo_pagamento_dias') or 30), numero_float(d.get('valor_total_contrato') or 0), str(d.get('contrato_referencia') or '').strip())).fetchone()
        conn.commit()
        return jsonify({'id': row['id'], 'mensagem': 'Contrato cadastrado com sucesso'}), 201
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
            if conn.execute("SELECT COUNT(*) as t FROM pagamentos_fornecedores WHERE cliente_id = ? AND cooperativa_id = ?", (id, current_user.cooperativa_id)).fetchone()['t'] > 0:
                return jsonify({'erro': 'Existem entregas/pagamentos de cooperados vinculados a este contrato. Exclua-os antes.'}), 400
            conn.execute("DELETE FROM contrato_cooperado WHERE cliente_id = ? AND cooperativa_id = ?", (id, current_user.cooperativa_id))
            conn.execute("DELETE FROM clientes WHERE id = ? AND cooperativa_id = ?", (id, current_user.cooperativa_id))
            conn.commit()
            return jsonify({'mensagem': 'Excluído com sucesso'})
        elif request.method == 'PUT':
            d = request.json
            conn.execute("UPDATE clientes SET nome=?, tipo=?, cidade=?, prazo_pagamento_dias=?, valor_total_contrato=?, contrato_referencia=? WHERE id=? AND cooperativa_id=?",
                         (d['nome'], d.get('tipo'), d.get('cidade'), int(d.get('prazo_pagamento_dias') or 30), numero_float(d.get('valor_total_contrato') or 0), str(d.get('contrato_referencia') or '').strip(), id, current_user.cooperativa_id))
            conn.commit()
            return jsonify({'mensagem': 'Atualizado com sucesso'})
    except psycopg2.errors.ForeignKeyViolation:
        conn.rollback()
        return jsonify({'erro': 'Não foi possível excluir: há registros vinculados a este contrato.'}), 409
    except Exception as e:
        conn.rollback()
        return jsonify({'erro': str(e)}), 400
    finally: conn.close()

@app.route('/api/clientes/<int:id>/cooperados', methods=['GET', 'POST'])
@cooperativa_required
def api_contrato_cooperados(id):
    conn = get_db()
    try:
        if request.method == 'GET':
            rows = conn.execute("""
                SELECT cc.fornecedor_id, f.nome, cc.cota, COALESCE((SELECT SUM(pfx.valor_compra) FROM pagamentos_fornecedores pfx WHERE pfx.fornecedor_id = cc.fornecedor_id AND pfx.cliente_id = cc.cliente_id AND pfx.cooperativa_id = cc.cooperativa_id), 0) as entregue
                FROM contrato_cooperado cc JOIN fornecedores f ON f.id = cc.fornecedor_id AND f.cooperativa_id = cc.cooperativa_id
                WHERE cc.cliente_id = ? AND cc.cooperativa_id = ? GROUP BY cc.cooperativa_id, cc.fornecedor_id, f.nome, cc.cota
            """, (id, current_user.cooperativa_id)).fetchall()
            return jsonify([{'fornecedor_id': r['fornecedor_id'], 'nome': r['nome'], 'cota': float(r['cota'] or 0), 'entregue': float(r['entregue'] or 0), 'falta': float(r['cota'] or 0) - float(r['entregue'] or 0)} for r in rows])
        
        elif request.method == 'POST':
            d = request.json
            cota = numero_float(d.get('cota') or 0)
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
            if conn.execute("SELECT COUNT(*) as t FROM usuarios WHERE fornecedor_id = ? AND cooperativa_id = ?", (id, current_user.cooperativa_id)).fetchone()['t'] > 0:
                return jsonify({'erro': 'Este cooperado possui usuário de acesso cadastrado. Remova o usuário antes de excluir.'}), 400
            conn.execute("DELETE FROM contrato_cooperado WHERE fornecedor_id = ? AND cooperativa_id = ?", (id, current_user.cooperativa_id))
            conn.execute("DELETE FROM fornecedores WHERE id = ? AND cooperativa_id = ?", (id, current_user.cooperativa_id))
            conn.commit()
            return jsonify({'mensagem': 'Excluído'})
        elif request.method == 'PUT':
            d = request.json
            conn.execute("UPDATE fornecedores SET nome=?, cpf_cnpj=?, cidade=? WHERE id=? AND cooperativa_id=?", (d['nome'], d.get('cpf_cnpj'), d.get('cidade'), id, current_user.cooperativa_id))
            conn.commit()
            return jsonify({'mensagem': 'Atualizado'})
    except psycopg2.errors.ForeignKeyViolation:
        conn.rollback()
        return jsonify({'erro': 'Não foi possível excluir: há registros vinculados a este cooperado.'}), 409
    except Exception as e:
        conn.rollback()
        return jsonify({'erro': str(e)}), 400
    finally: conn.close()

@app.route('/api/fornecedores/<int:id>/contratos', methods=['GET'])
@cooperativa_required
def api_fornecedor_contratos(id):
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT cc.cliente_id, c.nome as cliente_nome, cc.cota, COALESCE((SELECT SUM(pfx.valor_compra) FROM pagamentos_fornecedores pfx WHERE pfx.fornecedor_id = cc.fornecedor_id AND pfx.cliente_id = cc.cliente_id AND pfx.cooperativa_id = cc.cooperativa_id), 0) as entregue
            FROM contrato_cooperado cc JOIN clientes c ON c.id = cc.cliente_id AND c.cooperativa_id = cc.cooperativa_id
            WHERE cc.fornecedor_id = ? AND cc.cooperativa_id = ? GROUP BY cc.cooperativa_id, cc.cliente_id, c.nome, cc.cota
        """, (id, current_user.cooperativa_id)).fetchall()
        return jsonify([{'cliente_id': r['cliente_id'], 'cliente_nome': r['cliente_nome'], 'cota': float(r['cota'] or 0), 'entregue': float(r['entregue'] or 0), 'falta': float(r['cota'] or 0) - float(r['entregue'] or 0)} for r in rows])
    except Exception as e:
        return jsonify({'erro': str(e)}), 400
    finally:
        conn.close()

# ===========================================================================
# API - NOTAS FISCAIS (IMPORTAÇÃO E LISTAGEM)
# ===========================================================================
# ===========================================================================
# RELATÓRIOS PARA IMPRESSÃO / PDF (abrem em nova aba e chamam window.print())
# ===========================================================================
def _data_br(v):
    """2026-08-06 (ou date) -> 06/08/2026. Valor vazio vira '-'."""
    if not v:
        return '-'
    try:
        if hasattr(v, 'strftime'):
            return v.strftime('%d/%m/%Y')
        return date.fromisoformat(str(v)[:10]).strftime('%d/%m/%Y')
    except ValueError:
        return str(v)

def _num(v):
    try:
        n = float(v or 0)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if n != n else n

def _emitente_cooperativa(conn):
    row = conn.execute("SELECT nome, cnpj FROM cooperativas WHERE id = ?", (current_user.cooperativa_id,)).fetchone()
    nome = (row['nome'] if row else None) or 'Cooperativa'
    cnpj = (row['cnpj'] if row else None) or ''
    return {'nome_fantasia': nome, 'razao_social': nome,
            'endereco_linha1': f'CNPJ: {cnpj}' if cnpj else '', 'endereco_linha2': ''}

@app.route('/api/fornecedores/<int:id>/relatorio')
@cooperativa_required
def api_relatorio_fornecedor(id):
    cid = current_user.cooperativa_id
    conn = get_db()
    try:
        fornecedor = conn.execute(
            "SELECT id, nome, cpf_cnpj, cidade FROM fornecedores WHERE id = ? AND cooperativa_id = ?", (id, cid)
        ).fetchone()
        if not fornecedor:
            return jsonify({'erro': 'Cooperado não encontrado.'}), 404

        rows = conn.execute("""
            SELECT pf.id, pf.data_emissao, pf.produto_descricao, pf.peso_liquido, pf.valor_compra,
                   pf.valor_liquido, pf.status, c.nome AS cliente_nome,
                   COALESCE((SELECT STRING_AGG(DISTINCT nf2.numero_nota, ', ' ORDER BY nf2.numero_nota)
                             FROM pagamento_itens pi2
                             JOIN nota_itens ni2 ON ni2.id = pi2.nota_item_id
                             JOIN notas_fiscais nf2 ON nf2.id = ni2.nota_id AND nf2.cooperativa_id = pi2.cooperativa_id
                             WHERE pi2.pagamento_id = pf.id AND pi2.cooperativa_id = pf.cooperativa_id),
                            CASE WHEN pf.numero_nota_fornecedor IS NULL THEN nf.numero_nota ELSE NULL END) AS nota_vinculada
            FROM pagamentos_fornecedores pf
            LEFT JOIN clientes c ON c.id = pf.cliente_id AND c.cooperativa_id = pf.cooperativa_id
            LEFT JOIN notas_fiscais nf ON nf.id = pf.nota_id AND nf.cooperativa_id = pf.cooperativa_id
            WHERE pf.fornecedor_id = ? AND pf.cooperativa_id = ?
            ORDER BY pf.data_emissao DESC, pf.id DESC
        """, (id, cid)).fetchall()

        cota = _num(conn.execute(
            "SELECT COALESCE(SUM(cota), 0) AS t FROM contrato_cooperado WHERE fornecedor_id = ? AND cooperativa_id = ?", (id, cid)
        ).fetchone()['t'])
        try:
            repassado = _num(conn.execute(
                "SELECT COALESCE(SUM(valor), 0) AS t FROM repasses_cooperados WHERE fornecedor_id = ? AND cooperativa_id = ?", (id, cid)
            ).fetchone()['t'])
        except Exception:
            conn.rollback()
            repassado = 0.0
        emitente = _emitente_cooperativa(conn)
    finally:
        conn.close()

    datas = [r['data_emissao'] for r in rows if r['data_emissao']]
    periodo = f"{_data_br(min(datas))} a {_data_br(max(datas))}" if datas else None
    pagamentos = [{
        'data_emissao': _data_br(r['data_emissao']), 'nota_vinculada': r['nota_vinculada'],
        'cliente_nome': r['cliente_nome'], 'produto_descricao': r['produto_descricao'],
        'peso_liquido': _num(r['peso_liquido']), 'valor_compra': _num(r['valor_compra']),
        'valor_liquido': _num(r['valor_liquido']), 'status': r['status'] or 'pendente',
    } for r in rows]
    bruto = sum(p['valor_compra'] for p in pagamentos)
    liquido = sum(p['valor_liquido'] for p in pagamentos)
    resumo = {'cota': cota, 'total_bruto': bruto, 'total_descontos': bruto - liquido,
              'total_liquido': liquido, 'total_repassado': repassado,
              'total_a_pagar': round(liquido - repassado, 2)}
    return render_template('relatorio_cooperado.html', fornecedor=dict(fornecedor), emitente=emitente,
                           data_emissao=date.today().strftime('%d/%m/%Y'), periodo=periodo,
                           pagamentos=pagamentos, resumo=resumo)

@app.route('/api/notas/<int:id>/relatorio')
@cooperativa_required
def api_relatorio_nota(id):
    cid = current_user.cooperativa_id
    conn = get_db()
    try:
        nota = conn.execute("""
            SELECT nf.id, nf.numero_nota, nf.data_emissao, nf.valor_total, nf.status,
                   c.nome AS cliente_nome, c.cidade AS cliente_cidade
            FROM notas_fiscais nf
            JOIN clientes c ON c.id = nf.cliente_id AND c.cooperativa_id = nf.cooperativa_id
            WHERE nf.id = ? AND nf.cooperativa_id = ?
        """, (id, cid)).fetchone()
        if not nota:
            return jsonify({'erro': 'Nota não encontrada.'}), 404

        # Custos ligados à nota: pagamentos ligados direto a ela OU com itens faturados nela.
        custos = conn.execute("""
            SELECT pf.id, f.nome AS fornecedor_nome, pf.produto_descricao, pf.peso_liquido,
                   pf.valor_compra, pf.valor_liquido
            FROM pagamentos_fornecedores pf
            JOIN fornecedores f ON f.id = pf.fornecedor_id AND f.cooperativa_id = pf.cooperativa_id
            WHERE pf.cooperativa_id = ?
              AND (pf.nota_id = ? OR EXISTS (
                    SELECT 1 FROM pagamento_itens pi
                    JOIN nota_itens ni ON ni.id = pi.nota_item_id
                    WHERE pi.pagamento_id = pf.id AND pi.cooperativa_id = pf.cooperativa_id AND ni.nota_id = ?))
            ORDER BY f.nome, pf.id
        """, (cid, id, id)).fetchall()
    finally:
        conn.close()

    nota_d = {'numero_nota': nota['numero_nota'], 'data_emissao': _data_br(nota['data_emissao']),
              'valor_total': _num(nota['valor_total']), 'status': nota['status'] or '-',
              'cliente_nome': nota['cliente_nome'], 'cliente_cidade': nota['cliente_cidade'] or ''}
    custos_d = [{'fornecedor_nome': r['fornecedor_nome'], 'produto_descricao': r['produto_descricao'],
                 'peso_liquido': _num(r['peso_liquido']), 'valor_compra': _num(r['valor_compra']),
                 'valor_liquido': _num(r['valor_liquido'])} for r in custos]
    return render_template('relatorio_nota.html', nota=nota_d, custos=custos_d)

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
            exatos = [c for c in candidatos if simplificar_texto(c['nome']) == alvo]
            if len(exatos) == 1:
                return exatos[0]['id']
            if exatos:
                raise ValueError('Há mais de um cliente com esse nome; revise o cadastro antes de importar.')
            parciais = [c for c in candidatos if alvo in simplificar_texto(c['nome']) or simplificar_texto(c['nome']) in alvo]
            if len(parciais) == 1:
                return parciais[0]['id']
            if parciais:
                raise ValueError('Cliente ambíguo no PDF; revise os nomes no cadastro.')
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
                        "SELECT id, cliente_id FROM notas_fiscais WHERE numero_nota = ? AND cooperativa_id = ?",
                        (numero_nota, current_user.cooperativa_id)
                    ).fetchone()
                    if ja_existe:
                        cidade_pdf = grupo.get('cidade_destinatario') or ''
                        if cidade_pdf:
                            conn.execute("UPDATE clientes SET cidade = ? WHERE id = ? AND cooperativa_id = ? AND (cidade IS NULL OR TRIM(cidade) IN ('', '-'))",
                                         (cidade_pdf, ja_existe['cliente_id'], current_user.cooperativa_id))
                        if grupo.get('contrato_referencia'):
                            conn.execute("UPDATE notas_fiscais SET contrato_referencia = ? WHERE id = ? AND cooperativa_id = ? AND (contrato_referencia IS NULL OR contrato_referencia = '')",
                                         (grupo['contrato_referencia'], ja_existe['id'], current_user.cooperativa_id))
                        conn.commit()
                        avisos.append(f"Venda, nota {numero_nota}: já importada; cidade vazia conferida pelo PDF, sem duplicar a nota.")
                        continue

                    nome_cliente = truncar_para_coluna(grupo.get('contraparte_nome') or 'Cliente não identificado (importado via PDF)')
                    cliente_id_encontrado = resolver_cliente_por_nome(nome_cliente)
                    if cliente_id_encontrado:
                        cliente_id = cliente_id_encontrado
                        if grupo.get('cidade_destinatario'):
                            conn.execute("UPDATE clientes SET cidade = ? WHERE id = ? AND cooperativa_id = ? AND (cidade IS NULL OR TRIM(cidade) IN ('', '-'))", (grupo['cidade_destinatario'], cliente_id, current_user.cooperativa_id))
                    else:
                        cliente = conn.execute(
                            """INSERT INTO clientes (cooperativa_id, nome, cidade, tipo, ativo)
                               VALUES (?, ?, ?, 'prefeitura', TRUE)
                               RETURNING id""",
                            (current_user.cooperativa_id, nome_cliente, grupo.get('cidade_destinatario') or None)
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
                    if grupo.get('contrato_referencia'):
                        conn.execute("UPDATE notas_fiscais SET contrato_referencia = ? WHERE id = ? AND cooperativa_id = ?",
                                     (grupo['contrato_referencia'], nota_id, current_user.cooperativa_id))

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
                        pagamento_existente_id = ja_existe['id']
                        cliente_pdf = resolver_cliente_por_nome(grupo.get('cliente_destino_nome') or '')
                        if cliente_pdf:
                            conn.execute("UPDATE pagamentos_fornecedores SET cliente_id = ? WHERE id = ? AND cooperativa_id = ? AND cliente_id IS NULL",
                                         (cliente_pdf, pagamento_existente_id, current_user.cooperativa_id))
                        itens_existentes = conn.execute(
                            "SELECT COUNT(*) AS total FROM pagamento_itens WHERE pagamento_id = ? AND cooperativa_id = ?",
                            (pagamento_existente_id, current_user.cooperativa_id)
                        ).fetchone()['total']
                        if not itens_existentes:
                            for item in grupo['itens']:
                                conn.execute(
                                    """INSERT INTO pagamento_itens
                                       (cooperativa_id, pagamento_id, produto_descricao, quantidade, valor_unitario, valor_total)
                                       VALUES (?, ?, ?, ?, ?, ?)""",
                                    (current_user.cooperativa_id, pagamento_existente_id,
                                     truncar_para_coluna(item.get('produto') or 'Produto não identificado', 500),
                                     item.get('quantidade') or 0, item.get('valor_unitario') or 0, item.get('valor_total') or 0)
                                )
                            conn.commit()
                        else:
                            conn.commit()
                        avisos.append(f"Compra, nota {numero_nota}: já estava importada; itens conferidos para conciliação.")
                        continue

                    nome_prefeitura_destino = grupo.get('cliente_destino_nome') or ''
                    cliente_id_pf = resolver_cliente_por_nome(nome_prefeitura_destino)

                    valor_compra = sum(i['valor_total'] for i in grupo['itens'])
                    funrural = grupo.get('funrural_valor') or 0.0
                    liquido_pdf = grupo.get('valor_liquido')
                    if liquido_pdf is None:
                        liquido_pdf = grupo.get('valor_liquido_nota')
                    valor_liquido = liquido_pdf if liquido_pdf is not None else (valor_compra - funrural)
                    # O leitor legado usa zero para ausência do campo.
                    if valor_liquido == 0 and valor_compra != funrural:
                        valor_liquido = valor_compra - funrural
                    data_emissao = grupo.get('data_emissao') or date.today().isoformat()
                    produto_descricao = ", ".join(dict.fromkeys(i['produto'] for i in grupo['itens'] if i['produto']))[:500]
                    prefeitura_texto = nome_prefeitura_destino or grupo.get('destinatario_doc') or ''

                    # Não associar por proximidade de datas. A associação é feita
                    # depois, linha a linha, por prefeitura + produto + valor.
                    nota_id_vinculada = None
                    if cliente_id_pf:
                        conn.execute(
                            """INSERT INTO contrato_cooperado (cooperativa_id, cliente_id, fornecedor_id, cota)
                               VALUES (?, ?, ?, 0)
                               ON CONFLICT (cooperativa_id, cliente_id, fornecedor_id) DO NOTHING""",
                            (current_user.cooperativa_id, cliente_id_pf, fornecedor_id)
                        )
                        vinculos_cota += 1
                    else:
                        avisos.append(f"Compra, nota {numero_nota or '?'}: prefeitura '{nome_prefeitura_destino or '?'}' não encontrada no cadastro — pagamento gravado, mas SEM vínculo de cota.")

                    pagamento = conn.execute(
                        """INSERT INTO pagamentos_fornecedores
                            (cooperativa_id, fornecedor_id, cliente_id, nota_id, data_emissao, prefeitura, produto_descricao,
                             numero_nota_fornecedor, valor_compra, funrural, valor_liquido, status)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pendente') RETURNING id""",
                        (current_user.cooperativa_id, fornecedor_id, cliente_id_pf, nota_id_vinculada, data_emissao, prefeitura_texto,
                         produto_descricao, numero_nota, valor_compra, funrural, valor_liquido)
                    ).fetchone()
                    for item in grupo['itens']:
                        conn.execute(
                            """INSERT INTO pagamento_itens
                               (cooperativa_id, pagamento_id, produto_descricao, quantidade, valor_unitario, valor_total)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (current_user.cooperativa_id, pagamento['id'],
                             truncar_para_coluna(item.get('produto') or 'Produto não identificado', 500),
                             item.get('quantidade') or 0, item.get('valor_unitario') or 0, item.get('valor_total') or 0)
                        )
                    conn.commit()
                    pagamentos_gravados += 1
                except Exception as e:
                    conn.rollback()
                    avisos.append(f"Compra, nota {numero_nota or '?'}: erro ao gravar ({e}).")

            try:
                _reconciliar_itens_compra_venda(conn, current_user.cooperativa_id)
                pendentes = conn.execute("SELECT COUNT(*) AS total FROM pagamento_itens WHERE cooperativa_id = ? AND nota_item_id IS NULL", (current_user.cooperativa_id,)).fetchone()['total']
                if pendentes:
                    avisos.append(f'{pendentes} item(ns) de compra sem venda vinculada. Confira os vínculos das notas; correspondências ambíguas também ficam pendentes.')
                conn.commit()
            except Exception as e:
                conn.rollback()
                avisos.append(f"Aviso: não foi possível atualizar os vínculos produto a produto: {e}")
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
        SELECT nf.id, nf.numero_nota, nf.valor_total, c.nome as cliente_nome
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
                   (nf.valor_total - {SQL_CUSTO_NOTA}) as total_custos, 
                   {SQL_CUSTO_NOTA} as valor_liquido_nota
            FROM notas_fiscais nf 
            JOIN clientes c ON nf.cliente_id = c.id AND c.cooperativa_id = nf.cooperativa_id
            WHERE nf.cooperativa_id = ? 
            ORDER BY nf.data_emissao DESC
        """.replace('{SQL_CUSTO_NOTA}', SQL_LIQUIDO_NOTA), (current_user.cooperativa_id,)).fetchall()
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
            (current_user.cooperativa_id, d['numero_nota'], d['data_emissao'], d['cliente_id'], numero_float(d.get('valor_total') or 0),
             numero_float(d.get('peso_liquido') or 0), int(d.get('quantidade_cx') or 0),
             numero_float(d.get('valor_frete') or 0), d.get('status', 'emitida'),
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
                (d['numero_nota'], d['data_emissao'], d['cliente_id'], numero_float(d.get('valor_total') or 0),
                 numero_float(d.get('peso_liquido') or 0), int(d.get('quantidade_cx') or 0),
                 numero_float(d.get('valor_frete') or 0), d.get('status'), d.get('observacoes'),
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
CAMPOS_DESCONTO_EXTRA = ('pg_caixa_entrega', 'frete_cooaipro', 'pg_extra_paa', 'pg_frete_kits_mogi', 'pg_ref_uso_hr', 'pg_ref_uso_ford', 'ref_frete_entrega_poa', 'pg_afranio', 'pg_frete_mogi', 'vlr_pago_combustivel', 'frete_entrega_sao_jose', 'pg_uso_camara_fria', 'pg_doacao_produtor', 'pg_extra')

def _salvar_descontos_extra(conn, pagamento_id, d):
    # Colunas fixas de código; valores sempre parametrizados.
    conn.execute('UPDATE pagamentos_fornecedores SET ' +
                 ', '.join(c + ' = ?' for c in CAMPOS_DESCONTO_EXTRA) +
                 ' WHERE id = ? AND cooperativa_id = ?',
                 tuple(numero_float(d.get(c) or 0) for c in CAMPOS_DESCONTO_EXTRA) +
                 (pagamento_id, current_user.cooperativa_id))

def _calcular_valor_liquido_custo(d) -> float:
    valor_compra = numero_float(d.get('valor_compra') or 0)
    deducoes = sum(numero_float(d.get(campo) or 0) for campo in (
        'funrural', 'taxa_cooperativa', 'desconto_aipro',
        'pg_frete_coop', 'pg_caixa_papelao', 'adiantamento', 'descontos_outros'
    ))
    deducoes += sum(numero_float(d.get(c) or 0) for c in CAMPOS_DESCONTO_EXTRA)
    return round(valor_compra - deducoes, 2)


_ROTULOS_NUMERICOS_CUSTO = {
    'valor_compra': 'Valor da compra', 'peso_liquido': 'Peso líquido', 'funrural': 'Funrural',
    'taxa_cooperativa': 'Taxa da cooperativa', 'desconto_aipro': 'Desconto AIPRO',
    'pg_frete_coop': 'Frete', 'pg_caixa_papelao': 'Caixa de papelão',
    'adiantamento': 'Adiantamento', 'descontos_outros': 'Outros descontos',
}

def _validar_custo(d):
    """Valida os dados da entrega ANTES de ir ao banco. Retorna mensagem clara ou None."""
    if not isinstance(d, dict):
        return 'Nenhum dado foi enviado. Preencha o formulário e tente novamente.'
    faltando = []
    if not str(d.get('nota_id') or '').strip():
        faltando.append('Nota fiscal vinculada')
    if not str(d.get('fornecedor_id') or '').strip():
        faltando.append('Fornecedor')
    if not str(d.get('data_emissao') or '').strip():
        faltando.append('Data')
    if not str(d.get('valor_compra') or '').strip():
        faltando.append('Valor da compra')
    if faltando:
        return 'Preencha os campos obrigatórios: ' + ', '.join(faltando) + '.'
    for campo in ('nota_id', 'fornecedor_id'):
        try:
            int(d[campo])
        except (TypeError, ValueError):
            return f"Valor inválido para {'Nota fiscal' if campo == 'nota_id' else 'Fornecedor'}. Selecione uma opção da lista."
    try:
        datetime.strptime(str(d['data_emissao'])[:10], '%Y-%m-%d')
    except ValueError:
        return 'A data informada é inválida. Use o formato dia/mês/ano pelo seletor de data.'
    invalidos = []
    for campo, rotulo in {**_ROTULOS_NUMERICOS_CUSTO, **{c: c for c in CAMPOS_DESCONTO_EXTRA}}.items():
        try:
            import math
            numero = numero_float(d.get(campo) or 0)
            if not math.isfinite(numero) or numero < 0: raise ValueError()
        except (TypeError, ValueError):
            invalidos.append(rotulo)
    if invalidos:
        return 'Valor numérico inválido em: ' + ', '.join(invalidos) + '.'
    if numero_float(d.get('valor_compra') or 0) <= 0:
        return 'O valor da compra deve ser maior que zero.'
    if _calcular_valor_liquido_custo(d) < 0:
        return 'Os descontos não podem superar o valor da compra.'
    return None

def _mensagem_erro_banco(e):
    """Traduz erros do banco em mensagem compreensível para o usuário."""
    if isinstance(e, psycopg2.errors.NotNullViolation):
        coluna = getattr(getattr(e, 'diag', None), 'column_name', None)
        return f'Campo obrigatório não preenchido{f" ({coluna})" if coluna else ""}.'
    if isinstance(e, (psycopg2.errors.InvalidTextRepresentation,
                      psycopg2.errors.InvalidDatetimeFormat,
                      psycopg2.errors.NumericValueOutOfRange)):
        return 'Algum campo foi preenchido com um valor inválido. Revise datas e valores numéricos.'
    if isinstance(e, KeyError):
        return f'Campo obrigatório ausente: {e}.'
    return str(e)

@app.route('/api/custos', methods=['POST'])
@cooperativa_required
def api_criar_custo():
    if current_user.tipo != 'admin': return jsonify({'erro': 'Acesso negado'}), 403
    d = request.get_json(silent=True)
    erro_validacao = _validar_custo(d)
    if erro_validacao:
        return jsonify({'erro': erro_validacao}), 400
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
             numero_float(d.get('peso_liquido') or 0), numero_float(d.get('valor_compra') or 0), numero_float(d.get('funrural') or 0),
             numero_float(d.get('taxa_cooperativa') or 0), numero_float(d.get('desconto_aipro') or 0),
             numero_float(d.get('pg_frete_coop') or 0), numero_float(d.get('pg_caixa_papelao') or 0),
             numero_float(d.get('adiantamento') or 0), numero_float(d.get('descontos_outros') or 0), valor_liquido)
        ).fetchone()
        _salvar_descontos_extra(conn, row['id'], d)
        conn.commit()
        return jsonify({'id': row['id'], 'valor_liquido': valor_liquido, 'mensagem': 'Custo lançado com sucesso'}), 201
    except Exception as e:
        conn.rollback()
        return jsonify({'erro': _mensagem_erro_banco(e)}), 400
    finally:
        conn.close()

@app.route('/api/custos/<int:nota_id>', methods=['GET'])
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
            d = request.get_json(silent=True)
            atual = conn.execute('SELECT * FROM pagamentos_fornecedores WHERE id = ? AND cooperativa_id = ? FOR UPDATE', (id, current_user.cooperativa_id)).fetchone()
            if not atual: return jsonify({'erro': 'Custo não encontrado.'}), 404
            if isinstance(d, dict):
                d = {**{c: atual.get(c) for c in CAMPOS_DESCONTO_EXTRA}, **d}
            erro_validacao = _validar_custo(d)
            if erro_validacao:
                return jsonify({'erro': erro_validacao}), 400
            if d.get('nota_id') and not _tenant_exists(conn, 'notas_fiscais', d['nota_id']):
                return jsonify({'erro': 'Nota não pertence à cooperativa atual.'}), 400
            if not _tenant_exists(conn, 'fornecedores', d['fornecedor_id']):
                return jsonify({'erro': 'Fornecedor não pertence à cooperativa atual.'}), 400
            valor_liquido = _calcular_valor_liquido_custo(d)
            repassado = float(conn.execute(
                "SELECT COALESCE(SUM(valor), 0) AS t FROM repasses_cooperados WHERE pagamento_id = ? AND cooperativa_id = ?",
                (id, current_user.cooperativa_id)).fetchone()['t'] or 0)
            if valor_liquido + 0.005 < repassado:
                return jsonify({'erro': f'O valor líquido (R$ {valor_liquido:.2f}) não pode ficar abaixo do que já foi repassado (R$ {repassado:.2f}).'}), 409
            if repassado > 0:
                atual = conn.execute("SELECT fornecedor_id FROM pagamentos_fornecedores WHERE id = ? AND cooperativa_id = ?",
                                     (id, current_user.cooperativa_id)).fetchone()
                if atual and str(atual['fornecedor_id']) != str(d.get('fornecedor_id')):
                    return jsonify({'erro': 'Esta entrega já tem repasses registrados; não é possível trocar o cooperado.'}), 409
            cliente_id = None
            if d.get('nota_id'):
                nota = conn.execute("SELECT cliente_id FROM notas_fiscais WHERE id = ? AND cooperativa_id = ?",
                                    (d['nota_id'], current_user.cooperativa_id)).fetchone()
                cliente_id = nota['cliente_id'] if nota else None
            conn.execute(
                """UPDATE pagamentos_fornecedores
                   SET cliente_id=COALESCE(?, cliente_id), nota_id=?, fornecedor_id=?, data_emissao=?, numero_nota_fornecedor=?, status=?,
                       produto_descricao=?, peso_liquido=?, valor_compra=?, funrural=?, taxa_cooperativa=?,
                       desconto_aipro=?, pg_frete_coop=?, pg_caixa_papelao=?, adiantamento=?,
                       descontos_outros=?, valor_liquido=?
                   WHERE id=? AND cooperativa_id=?""",
                (cliente_id, d.get('nota_id'), d.get('fornecedor_id'), d.get('data_emissao'), d.get('numero_nota_fornecedor'),
                 d.get('status'), d.get('produto_descricao'), numero_float(d.get('peso_liquido') or 0),
                 numero_float(d.get('valor_compra') or 0), numero_float(d.get('funrural') or 0),
                 numero_float(d.get('taxa_cooperativa') or 0), numero_float(d.get('desconto_aipro') or 0),
                 numero_float(d.get('pg_frete_coop') or 0), numero_float(d.get('pg_caixa_papelao') or 0),
                 numero_float(d.get('adiantamento') or 0), numero_float(d.get('descontos_outros') or 0), valor_liquido, id, current_user.cooperativa_id)
            )
            _salvar_descontos_extra(conn, id, d)
            # Mantém o detalhamento por produto em sincronia quando a entrega tem um único item.
            itens = conn.execute("SELECT id FROM pagamento_itens WHERE pagamento_id = ? AND cooperativa_id = ?",
                                 (id, current_user.cooperativa_id)).fetchall()
            if len(itens) == 1:
                conn.execute(
                    "UPDATE pagamento_itens SET produto_descricao=?, quantidade=?, valor_total=? WHERE id=? AND cooperativa_id=?",
                    (d.get('produto_descricao') or 'Produto não identificado', numero_float(d.get('peso_liquido') or 0),
                     numero_float(d.get('valor_compra') or 0), itens[0]['id'], current_user.cooperativa_id))
            if itens:
                _reconciliar_itens_compra_venda(conn, current_user.cooperativa_id)
            conn.commit()
            return jsonify({'mensagem': 'Atualizado com sucesso'})
        elif request.method == 'DELETE':
            qtd, total = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(valor), 0) AS t FROM repasses_cooperados WHERE pagamento_id = ? AND cooperativa_id = ?",
                (id, current_user.cooperativa_id)).fetchone().values()
            if qtd and request.args.get('excluir_repasses') != '1':
                return jsonify({'erro': f'Esta entrega tem {qtd} repasse(s) registrado(s), somando R$ {float(total):.2f}.',
                                'requer_confirmacao_repasses': True}), 409
            conn.execute("DELETE FROM repasses_cooperados WHERE pagamento_id = ? AND cooperativa_id = ?", (id, current_user.cooperativa_id))
            conn.execute("DELETE FROM pagamento_itens WHERE pagamento_id = ? AND cooperativa_id = ?", (id, current_user.cooperativa_id))
            conn.execute("DELETE FROM pagamentos_fornecedores WHERE id = ? AND cooperativa_id = ?", (id, current_user.cooperativa_id))
            conn.commit()
            return jsonify({'mensagem': 'Excluído com sucesso'})
    except psycopg2.errors.ForeignKeyViolation as e:
        conn.rollback()
        return jsonify({'erro': f'Não foi possível concluir: há registros vinculados. Detalhe: {e.diag.constraint_name or e}'}), 409
    except Exception as e:
        conn.rollback()
        return jsonify({'erro': _mensagem_erro_banco(e)}), 400
    finally:
        conn.close()

# ===========================================================================
# API - REPASSES EFETIVOS A COOPERADOS (histórico separado dos custos)
# A tabela nova é criada pela migração migracao_repasses_cooperados.sql.
# ===========================================================================
@app.route('/api/pagamentos/<int:pagamento_id>/repasses', methods=['GET', 'POST'])
@cooperativa_required
def api_repasses_pagamento(pagamento_id):
    # Usuários autenticados da cooperativa podem consultar/registrar repasses
    # somente nos registros pertencentes à própria cooperativa (filtro abaixo).
    conn = get_db()
    try:
        pagamento = conn.execute(
            "SELECT id, fornecedor_id, valor_liquido, status FROM pagamentos_fornecedores WHERE id = ? AND cooperativa_id = ?",
            (pagamento_id, current_user.cooperativa_id)
        ).fetchone()
        if not pagamento:
            return jsonify({'erro': 'Pagamento/custo não encontrado nesta cooperativa.'}), 404

        if request.method == 'GET':
            rows = conn.execute(
                """SELECT id, valor, data_pagamento, forma_pagamento, referencia, observacoes, created_at
                   FROM repasses_cooperados
                   WHERE pagamento_id = ? AND cooperativa_id = ?
                   ORDER BY data_pagamento DESC, id DESC""",
                (pagamento_id, current_user.cooperativa_id)
            ).fetchall()
            total = conn.execute(
                "SELECT COALESCE(SUM(valor), 0) AS total FROM repasses_cooperados WHERE pagamento_id = ? AND cooperativa_id = ?",
                (pagamento_id, current_user.cooperativa_id)
            ).fetchone()['total'] or 0
            liquido = pagamento['valor_liquido'] or 0
            return jsonify({
                'pagamento_id': pagamento_id,
                'valor_liquido': float(liquido),
                'total_repassado': float(total),
                'saldo': round(float(liquido) - float(total), 2),
                'repasses': [
                    {**r, 'data_pagamento': r['data_pagamento'].isoformat() if hasattr(r.get('data_pagamento'), 'isoformat') else r.get('data_pagamento'),
                     'created_at': r['created_at'].isoformat() if hasattr(r.get('created_at'), 'isoformat') else r.get('created_at')}
                    for r in _limpar([dict(r) for r in rows])
                ]
            })

        d = request.get_json(silent=True) or {}
        try:
            valor = numero_decimal(d.get('valor', '0'))
        except Exception:
            return jsonify({'erro': 'Informe um valor válido.'}), 400
        if not valor.is_finite() or valor <= 0:
            return jsonify({'erro': 'O valor do repasse deve ser maior que zero.'}), 400
        data_pagamento = (d.get('data_pagamento') or '').strip()
        forma = (d.get('forma_pagamento') or '').strip()
        if not data_pagamento or not forma:
            return jsonify({'erro': 'Data e forma de pagamento são obrigatórias.'}), 400
        # Valida a data e usa transação para evitar exceder o valor líquido.
        try:
            date.fromisoformat(data_pagamento)
        except ValueError:
            return jsonify({'erro': 'Data de pagamento inválida.'}), 400

        pagamento_bloqueado = conn.execute(
            'SELECT id, fornecedor_id, valor_liquido FROM pagamentos_fornecedores WHERE id = ? AND cooperativa_id = ? FOR UPDATE',
            (pagamento_id, current_user.cooperativa_id)
        ).fetchone()
        if not pagamento_bloqueado:
            return jsonify({'erro': 'Pagamento/custo não encontrado nesta cooperativa.'}), 404
        pagamento = pagamento_bloqueado
        total_row = conn.execute(
            "SELECT COALESCE(SUM(valor), 0) AS total FROM repasses_cooperados WHERE pagamento_id = ? AND cooperativa_id = ?",
            (pagamento_id, current_user.cooperativa_id)
        ).fetchone()
        cent = Decimal('0.01')
        valor = valor.quantize(cent)
        total_atual = Decimal(str(total_row['total'] or 0)).quantize(cent)
        liquido = Decimal(str(pagamento['valor_liquido'] or 0)).quantize(cent)
        saldo = liquido - total_atual
        if valor > saldo:
            return jsonify({'erro': f'O repasse excede o saldo disponível de R$ {max(saldo, Decimal("0.00")):.2f}.'}), 400

        conn.execute(
            """INSERT INTO repasses_cooperados
               (cooperativa_id, pagamento_id, fornecedor_id, valor, data_pagamento, forma_pagamento, referencia, observacoes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (current_user.cooperativa_id, pagamento_id, pagamento['fornecedor_id'], str(valor), data_pagamento,
             forma, (d.get('referencia') or '').strip() or None, (d.get('observacoes') or '').strip() or None)
        )
        total_novo = total_atual + valor
        if total_novo == liquido:
            conn.execute(
                "UPDATE pagamentos_fornecedores SET status = 'pago', data_pagamento = ? WHERE id = ? AND cooperativa_id = ?",
                (data_pagamento, pagamento_id, current_user.cooperativa_id)
            )
        else:
            # Não muda para 'pago' quando ainda há saldo em aberto.
            conn.execute(
                "UPDATE pagamentos_fornecedores SET status = 'pendente', data_pagamento = NULL WHERE id = ? AND cooperativa_id = ?",
                (pagamento_id, current_user.cooperativa_id)
            )
        conn.commit()
        return jsonify({'mensagem': 'Repasse registrado.', 'total_repassado': float(total_novo),
                        'saldo': float(liquido-total_novo), 'status': 'pago' if total_novo == liquido else 'pendente'}), 201
    except Exception as e:
        conn.rollback()
        if isinstance(e, psycopg2.errors.UndefinedTable):
            msg = 'A tabela de repasses ainda não existe no banco. Reinicie o sistema para criá-la automaticamente.'
        else:
            msg = _mensagem_erro_banco(e)
        return jsonify({'erro': msg}), 400
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
        saldos = conn.execute("SELECT fornecedor_id, SUM(saldo) AS saldo FROM (" + SQL_SALDOS_REPASSES + ") saldos WHERE cooperativa_id = ? GROUP BY fornecedor_id", (_tenant_id(),)).fetchall()
        por_fornecedor = {r['fornecedor_id']: r['saldo'] for r in saldos}
        return jsonify(_limpar([{**dict(r), 'total_a_pagar': por_fornecedor.get(r['id'], 0)} for r in rows]))
    finally:
        conn.close()

# Desconta repasses parciais; preserva baixas antigas sem histórico de repasses.
SQL_SALDOS_REPASSES = """
    SELECT pf.id, pf.fornecedor_id, pf.cooperativa_id,
           CASE WHEN pf.status = 'pago' AND COALESCE(r.quantidade, 0) = 0 THEN 0
                ELSE GREATEST(COALESCE(pf.valor_liquido, 0) - COALESCE(r.total, 0), 0) END AS saldo
    FROM pagamentos_fornecedores pf
    LEFT JOIN (SELECT pagamento_id, cooperativa_id, SUM(valor) AS total, COUNT(*) AS quantidade
               FROM repasses_cooperados GROUP BY pagamento_id, cooperativa_id) r
    ON r.pagamento_id = pf.id AND r.cooperativa_id = pf.cooperativa_id
"""

@app.route('/api/resumo')
@cooperativa_required
def api_resumo():
    conn = get_db()
    try:
        a_receber = conn.execute("SELECT COALESCE(SUM(valor_a_receber),0) AS v FROM vw_resultados_por_prefeitura WHERE cooperativa_id = ?", (current_user.cooperativa_id,)).fetchone()['v']
        a_pagar = conn.execute("SELECT COALESCE(SUM(saldo), 0) AS v FROM (" + SQL_SALDOS_REPASSES + ") saldos WHERE cooperativa_id = ?", (_tenant_id(),)).fetchone()['v']
        notas_pend = conn.execute("SELECT COUNT(*) AS n FROM notas_fiscais WHERE status <> 'paga' AND cooperativa_id = ?", (current_user.cooperativa_id,)).fetchone()['n']
        pag_pend = conn.execute("SELECT COUNT(*) AS n FROM (" + SQL_SALDOS_REPASSES + ") saldos WHERE saldo > 0 AND cooperativa_id = ?", (_tenant_id(),)).fetchone()['n']
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


# Compatibilidade com as importações já oferecidas nas telas.
def _numero_importacao(valor, campo):
    try:
        n = numero_decimal(valor)
        if n < 0:
            raise ValueError()
        return n
    except Exception:
        raise ValueError(f'{campo}: informe um número positivo válido.')


def _data_importacao(valor):
    texto = str(valor or '').strip()
    for formato in ('%Y-%m-%d', '%d/%m/%Y', '%d/%m/%y'):
        try: return datetime.strptime(texto[:10], formato).date().isoformat()
        except ValueError: pass
    raise ValueError('Data inválida: use dd/mm/aaaa ou aaaa-mm-dd.')


def _resolver_cadastro_importacao(conn, tabela, nome):
    if tabela not in ('clientes', 'fornecedores'): raise ValueError('Cadastro inválido.')
    rows = conn.execute(f'SELECT id, nome FROM {tabela} WHERE cooperativa_id = ?', (_tenant_id(),)).fetchall()
    encontrados = [r for r in rows if simplificar_texto(r['nome']) == simplificar_texto(str(nome or ''))]
    if len(encontrados) != 1:
        raise ValueError(f'{nome}: cadastro ausente ou ambíguo. Confira o cadastro antes de importar.')
    return encontrados[0]['id']


def _gravar_notas_importadas(conn, registros):
    importadas = ignoradas = 0
    for linha, d in enumerate(registros, 2):
        if not isinstance(d, dict): raise ValueError(f'Linha {linha}: registro inválido.')
        try:
            numero = str(d.get('numero_nota') or '').strip()
            if not numero: raise ValueError('Número da nota obrigatório.')
            if len(numero) > 20: raise ValueError('Número da nota deve ter no máximo 20 caracteres.')
            cliente = _resolver_cadastro_importacao(conn, 'clientes', d.get('cliente') or d.get('cliente_nome'))
            emissao = _data_importacao(d.get('data_emissao'))
            valor = _numero_importacao(d.get('valor_total'), 'Valor total')
            if valor <= 0: raise ValueError('Valor total deve ser maior que zero.')
            status = str(d.get('status') or 'emitida').lower().strip()
            if status not in ('paga', 'pendente', 'emitida', 'cancelada'): raise ValueError('Status inválido.')
            if conn.execute('SELECT id FROM notas_fiscais WHERE cooperativa_id = ? AND numero_nota = ?', (_tenant_id(), numero)).fetchone():
                ignoradas += 1
                continue
            nota = conn.execute('''INSERT INTO notas_fiscais
                (cooperativa_id, numero_nota, cliente_id, data_emissao, valor_total, status, peso_liquido, valor_frete)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id''',
                (_tenant_id(), numero, cliente, emissao, valor, status,
                 _numero_importacao(d.get('peso_liquido'), 'Peso'), _numero_importacao(d.get('valor_frete'), 'Frete'))).fetchone()
            for item in d.get('itens', []):
                if not item.get('descricao') or len(item['descricao']) > 100:
                    raise ValueError('Descrição do produto deve ter entre 1 e 100 caracteres. Confira o XML antes de importar.')
                produto = conn.execute('SELECT id FROM produtos WHERE cooperativa_id = ? AND nome = ?', (_tenant_id(), item['descricao'])).fetchone()
                if not produto:
                    produto = conn.execute('INSERT INTO produtos (cooperativa_id, nome, unidade_medida) VALUES (?, ?, ?) RETURNING id', (_tenant_id(), item['descricao'], item['unidade'])).fetchone()
                conn.execute('INSERT INTO nota_itens (cooperativa_id, nota_id, produto_id, quantidade, valor_unitario, valor_total) VALUES (?, ?, ?, ?, ?, ?)', (_tenant_id(), nota['id'], produto['id'], _numero_importacao(item['quantidade'], 'Quantidade'), _numero_importacao(item['valor_unitario'], 'Valor unitário'), _numero_importacao(item['valor_total'], 'Total do item')))
            importadas += 1
        except ValueError as e: raise ValueError(f'Linha {linha}: {e}') from e
    return importadas, ignoradas


@app.route('/api/notas/importar', methods=['POST'])
@cooperativa_required
def api_importar_planilha_notas():
    registros = request.get_json(silent=True)
    if not isinstance(registros, list) or not registros:
        return jsonify({'erro': 'Envie uma lista não vazia de notas.'}), 400
    conn = get_db()
    try:
        n, ignoradas = _gravar_notas_importadas(conn, registros)
        conn.commit()
        return jsonify({'mensagem': f'{n} nota(s) importada(s); {ignoradas} já existente(s).', 'importadas': n, 'ignoradas': ignoradas})
    except Exception as e:
        conn.rollback()
        return jsonify({'erro': str(e)}), 400
    finally: conn.close()


@app.route('/api/notas/importar_xml', methods=['POST'])
@cooperativa_required
def api_importar_xml_notas():
    arquivos = request.files.getlist('arquivos_xml')
    if not arquivos: return jsonify({'erro': 'Selecione os XML de NF-e.'}), 400
    conn = get_db()
    try:
        registros = [parse_nfe_xml(f.stream) for f in arquivos]
        n, ignoradas = _gravar_notas_importadas(conn, registros)
        conn.commit()
        return jsonify({'mensagem': f'{n} nota(s) importada(s); {ignoradas} já existente(s).'})
    except Exception as e:
        conn.rollback()
        return jsonify({'erro': str(e)}), 400
    finally: conn.close()


@app.route('/api/notas/<int:nota_id>/produtos')
@cooperativa_required
def api_produtos_nota(nota_id):
    conn = get_db()
    try:
        if not _tenant_exists(conn, 'notas_fiscais', nota_id):
            return jsonify({'erro': 'Nota não encontrada.'}), 404
        rows = conn.execute('''SELECT p.nome AS descricao, ni.quantidade, ni.valor_unitario, ni.valor_total
            FROM nota_itens ni JOIN produtos p ON p.id = ni.produto_id AND p.cooperativa_id = ni.cooperativa_id
            WHERE ni.nota_id = ? AND ni.cooperativa_id = ? ORDER BY ni.id''', (nota_id, _tenant_id())).fetchall()
        return jsonify(_limpar([dict(r) for r in rows]))
    finally: conn.close()


@app.route('/api/produtos/extrair_pdf', methods=['POST'])
@cooperativa_required
def api_extrair_produtos_pdf():
    arquivo = request.files.get('arquivo_pdf')
    if not arquivo or not arquivo.filename: return jsonify({'erro': 'Selecione um PDF.'}), 400
    fd, caminho = tempfile.mkstemp(suffix='.pdf')
    os.close(fd)
    try:
        arquivo.save(caminho)
        registros = parse_danfe_pdf(caminho)
        produtos = [{'descricao': r['produto'], 'quantidade': r.get('quantidade') or 0}
                    for r in registros if r.get('produto')]
        if not produtos: return jsonify({'erro': 'Não foi possível identificar produtos neste PDF. Confira o documento ou preencha manualmente.'}), 422
        numeros = {str(r['numero_nota']) for r in registros if r.get('numero_nota')}
        if len(numeros) > 1: return jsonify({'erro': 'Selecione um PDF com apenas uma nota fiscal.'}), 400
        return jsonify({'produtos': produtos, 'numero_nota': next(iter(numeros), '')})
    except Exception as e: return jsonify({'erro': f'Não foi possível ler o PDF: {e}'}), 400
    finally:
        if os.path.exists(caminho): os.remove(caminho)


@app.route('/api/pagamentos/importar', methods=['POST'])
@cooperativa_required
def api_importar_planilha_pagamentos():
    registros = request.get_json(silent=True)
    if not isinstance(registros, list) or not registros:
        return jsonify({'erro': 'Envie uma lista não vazia de pagamentos.'}), 400
    conn = get_db()
    try:
        importadas = 0
        for linha, d in enumerate(registros, 2):
            if not isinstance(d, dict): raise ValueError(f'Linha {linha}: registro inválido.')
            fornecedor = _resolver_cadastro_importacao(conn, 'fornecedores', d.get('fornecedor') or d.get('fornecedor_nome'))
            emissao = _data_importacao(d.get('data_emissao'))
            valor = _numero_importacao(d.get('valor_compra') or d.get('bruto'), 'Valor da compra')
            if valor <= 0: raise ValueError(f'Linha {linha}: valor da compra deve ser positivo.')
            campos = ('funrural', 'taxa_cooperativa', 'desconto_aipro', 'pg_frete_coop', 'pg_caixa_papelao', 'adiantamento', 'descontos_outros') + CAMPOS_DESCONTO_EXTRA
            descontos = {c: _numero_importacao(d.get(c), c) for c in campos}
            liquido = valor - sum(descontos.values())
            if liquido < 0: raise ValueError(f'Linha {linha}: descontos superiores à compra.')
            # Um lançamento importado não é comprovante de repasse.
            if str(d.get('status') or 'pendente').lower().strip() != 'pendente':
                raise ValueError(f'Linha {linha}: importe como pendente e registre os repasses com data e forma de pagamento.')
            nota_id = cliente_id = None
            numero = str(d.get('numero_nota') or '').strip()
            if numero:
                notas = conn.execute('SELECT id, cliente_id FROM notas_fiscais WHERE cooperativa_id = ? AND numero_nota = ?', (_tenant_id(), numero)).fetchall()
                if len(notas) != 1: raise ValueError(f'Linha {linha}: nota de venda ausente ou ambígua.')
                nota_id, cliente_id = notas[0]['id'], notas[0]['cliente_id']
            numero_fornecedor = str(d.get('numero_nota_fornecedor') or '').strip() or None
            if not numero_fornecedor:
                raise ValueError(f'Linha {linha}: informe numero_nota_fornecedor para controlar duplicidades.')
            if conn.execute('SELECT id FROM pagamentos_fornecedores WHERE cooperativa_id = ? AND fornecedor_id = ? AND numero_nota_fornecedor = ?', (_tenant_id(), fornecedor, numero_fornecedor)).fetchone():
                raise ValueError(f'Linha {linha}: nota do cooperado já cadastrada. Confira os lançamentos antes de importar novamente.')
            colunas = ('cooperativa_id', 'fornecedor_id', 'nota_id', 'cliente_id', 'data_emissao', 'numero_nota_fornecedor', 'produto_descricao', 'peso_liquido', 'valor_compra', 'valor_liquido', 'status') + campos
            valores = (_tenant_id(), fornecedor, nota_id, cliente_id, emissao, numero_fornecedor, d.get('produto_descricao') or d.get('produto') or '', _numero_importacao(d.get('peso_liquido'), 'Peso'), valor, liquido, 'pendente') + tuple(descontos.values())
            conn.execute('INSERT INTO pagamentos_fornecedores (' + ', '.join(colunas) + ') VALUES (' + ', '.join('?' for _ in colunas) + ')', valores)
            importadas += 1
        conn.commit()
        return jsonify({'mensagem': f'{importadas} lançamento(s) importado(s). Registre os repasses quando ocorrerem.'})
    except Exception as e:
        conn.rollback()
        return jsonify({'erro': str(e)}), 400
    finally: conn.close()


instalar_minha_conta(app, get_db, cooperativa_required)

if __name__ == '__main__':
    app.run(debug=True)