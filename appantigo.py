from flask import Flask, jsonify, render_template, request, send_file, redirect, url_for
from flask_cors import CORS
import psycopg2
import psycopg2.extras
import subprocess
import pandas as pd
import os
import io
import re
import unicodedata
import tempfile
import xml.etree.ElementTree as ET
from datetime import date
from werkzeug.utils import secure_filename

# Carrega variáveis de ambiente
from dotenv import load_dotenv
load_dotenv()

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
app.config['SECRET_KEY'] = 'coaipro'
app.config['CODIGO_CADASTRO_COOPERATIVA'] = 'coaipro'

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
# FUNÇÕES AUXILIARES E PARSERS (XML e PDF)
# ===========================================================================
def simplificar_texto(texto):
    if not texto: return ""
    # Remove acentos
    texto_sem_acento = unicodedata.normalize('NFKD', str(texto)).encode('ASCII', 'ignore').decode('utf-8')
    texto_limpo = texto_sem_acento.lower()
    
    # Normaliza abreviações comuns de prefeituras para não duplicar contratos
    texto_limpo = texto_limpo.replace('pref.', 'prefeitura').replace('pref ', 'prefeitura ')
    texto_limpo = texto_limpo.replace('mun.', 'municipal').replace('mun ', 'municipal ')
    
    # Remove tudo que não for letra ou número
    return re.sub(r'[^a-z0-9]', '', texto_limpo)

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

def parse_danfe_pdf(caminho_arquivo):
    with pdfplumber.open(caminho_arquivo) as pdf:
        texto = "\n".join([page.extract_text() or '' for page in pdf.pages])

    if not texto.strip():
        raise ValueError("Não foi possível extrair texto do PDF.")

    # 1. Número da Nota Fiscal
    num_match = re.search(r'N[°º\s\.:]*([\d\.]+)', texto, re.IGNORECASE)
    if not num_match:
        raise ValueError("Número da nota não identificado no PDF.")
    numero_nota = num_match.group(1).replace('.', '')

    # 2. Data de Emissão (Busca flexível para tabelas DANFE)
    data_match = re.search(r'DATA DA EMISS[ÃA]O[^\d]*(\d{2}/\d{2}/\d{4})', texto, re.IGNORECASE)
    if not data_match:
        # Fallback: pega qualquer primeira data no formato DD/MM/AAAA que aparecer
        data_match = re.search(r'(\d{2}/\d{2}/\d{4})', texto)
        
    if data_match:
        d, m, y = data_match.group(1).split('/')
        data_emissao = f"{y}-{m}-{d}"
    else:
        # Fallback final de segurança para não violar o NOT NULL do banco
        from datetime import date
        data_emissao = date.today().strftime('%Y-%m-%d')

    # 3. Valor Total da Nota
    # "VALOR TOTAL DA NOTA" é o último cabeçalho de uma linha de colunas (frete, seguro,
    # desconto, outras despesas, IPI, total). O valor correspondente é o ÚLTIMO número
    # da linha de valores seguinte, não o primeiro (que seria o Valor do Frete).
    valor_total = 0.0
    linha_match = re.search(r'VALOR TOTAL DA NOTA\s*\n\s*([\d\.,\s]+)', texto, re.IGNORECASE)
    if linha_match:
        numeros = re.findall(r'[\d]{1,3}(?:\.\d{3})*,\d{2}', linha_match.group(1))
        if numeros:
            try: valor_total = float(numeros[-1].replace('.', '').replace(',', '.'))
            except: pass
    if valor_total == 0.0:
        # Fallback: tenta "VALOR TOTAL DOS PRODUTOS" (última coluna da linha do ICMS)
        linha_match = re.search(r'VALOR TOTAL DOS PRODUTOS\s*\n\s*([\d\.,\s]+)', texto, re.IGNORECASE)
        if linha_match:
            numeros = re.findall(r'[\d]{1,3}(?:\.\d{3})*,\d{2}', linha_match.group(1))
            if numeros:
                try: valor_total = float(numeros[-1].replace('.', '').replace(',', '.'))
                except: pass

    # 4. Frete
    frete_match = re.search(r'VALOR DO FRETE[^\d]*([\d\.,]+)', texto, re.IGNORECASE)
    valor_frete = 0.0
    if frete_match:
        try: valor_frete = float(frete_match.group(1).replace('.', '').replace(',', '.'))
        except: pass

    # 5. Peso / Quantidade
    peso_match = re.search(r'kg\s*\|\s*([\d\.,]+)', texto, re.IGNORECASE) or re.search(r'kg[^\d]*([\d\.,]+)', texto, re.IGNORECASE)
    peso_liquido = 0.0
    if peso_match:
        try: peso_liquido = float(peso_match.group(1).replace('.', '').replace(',', '.'))
        except: pass

    # 6. Destinatário / Razão Social
    # "NOME/RAZÃO SOCIAL" é um cabeçalho de coluna; o valor real (nome do cliente) fica
    # na LINHA SEGUINTE, junto com CNPJ/CPF e data na mesma linha. Precisamos pegar essa
    # linha de valores e cortar antes do primeiro número (CNPJ/CPF/data).
    cliente_nome = 'Cliente Não Identificado'
    linha_match = re.search(r'NOME/RAZ[ÃA]O SOCIAL[^\n]*\n\s*([^\n]+)', texto, re.IGNORECASE)
    if linha_match:
        linha_valor = linha_match.group(1).strip()
        cliente_nome = re.split(r'\s+\d[\d./\-]{5,}', linha_valor)[0].strip()
        if not cliente_nome:
            cliente_nome = 'Cliente Não Identificado'

    # 7. Produtos (linhas da tabela "DADOS DO PRODUTO/SERVIÇO")
    # O pdfplumber.extract_text() gera texto simples (sem "|"), então o padrão
    # precisa casar direto com a sequência de colunas da linha do produto, ex.:
    # "1.5 ABOBORA JAPONESA 07099300 040 5102 kg 105,0000 4,7700 500,85"
    #    código      descrição        NCM/SH  CST  CFOP  unid   qtd     vlr.unit  vlr.total
   # NOVO CÓDIGO: Extração dos Produtos (Melhorado para PDFs irregulares)
   # 7. Produtos (linhas da tabela "DADOS DO PRODUTO/SERVIÇO")
   # 7. Produtos (linhas da tabela "DADOS DO PRODUTO/SERVIÇO")
    produtos = []
    
    # Processa estritamente LINHA POR LINHA para não misturar os cabeçalhos com os itens
    linhas = texto.split('\n')
    
    # Regex para buscar a estrutura da linha do produto focando no NCM
    padrao_produto = re.compile(
        r'(.*?)\s*\|?\s*'                 # Grupo 1: Tudo antes do NCM (Código + Descrição)
        r'(\b\d{7,8}\b)\s*\|?\s*'         # Grupo 2: NCM (7 ou 8 dígitos)
        r'\d{1,4}\s*\|?\s*'               # CST
        r'\d{1,4}\s*\|?\s*'               # CFOP
        r'[A-Za-z]{2,4}\s*\|?\s*'         # UNID (kg, cx, un, etc)
        r'([\d,.]+)\s*\|?\s*'             # Grupo 3: Quantidade
        r'([\d,.]+)\s*\|?\s*'             # Grupo 4: Valor Unitário
        r'([\d,.]+)'                      # Grupo 5: Valor Total
    )
    
    for linha in linhas:
        match = padrao_produto.search(linha)
        if match:
            try:
                descricao_bruta = match.group(1).strip()
                
                # Se o PDF extraiu os separadores '|', pegamos apenas o que está após o último
                if '|' in descricao_bruta:
                    descricao_bruta = descricao_bruta.split('|')[-1]
                
                # Limpa o código numérico do item que vem antes do nome (ex: "1.5 ABOBORA" -> "ABOBORA")
                descricao_limpa = re.sub(r'^[\d.\-\s]+', '', descricao_bruta).strip()
                
                # Se limpou tudo e não sobrou texto válido, pula a linha
                if len(descricao_limpa) < 2:
                    continue
                
                quantidade = float(match.group(3).replace('.', '').replace(',', '.'))
                valor_total_item = float(match.group(5).replace('.', '').replace(',', '.'))
                
                produtos.append({
                    'descricao': descricao_limpa,
                    'quantidade': quantidade,
                    'valor_total': valor_total_item
                })
            except ValueError:
                continue

    return {
        'numero_nota': numero_nota,
        'data_emissao': data_emissao,
        'cliente_nome': cliente_nome,
        'valor_total': valor_total,
        'valor_frete': valor_frete,
        'peso_liquido': peso_liquido,
        'produtos': produtos
    }
# ===========================================================================
# INICIALIZAÇÕES E MIGRAÇÕES
# ===========================================================================
def _inicializar_primeira_execucao():
    garantir_banco_criado(DB_CONFIG)
    if is_primeira_execucao(DB_CONFIG):
        migrar_seed_tabelas(DB_CONFIG)
        carregar_planilhas_modelo(DB_CONFIG)

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
        conn = get_db()
        row = conn.execute("SELECT * FROM usuarios WHERE email = ?", (email,)).fetchone()
        conn.close()

        if not row or not check_password_hash(row['senha_hash'], senha):
            return render_template('landing.html', erro_login='Email ou senha inválidos')

        usuario = Usuario(row['id'], row['email'], row['tipo'], row['fornecedor_id'], row['aprovado'])
        login_user(usuario)
        return redirect(url_for('tabelas')) if usuario.tipo == 'cooperativa' else redirect(url_for('meu_extrato'))
    return redirect(url_for('landing'))

@app.route('/cadastro', methods=['GET', 'POST'])
def cadastro():
    if request.method == 'POST':
        tipo = request.form.get('tipo', 'cooperado')
        email = request.form['email']
        senha = request.form['senha']
        conn = get_db()

        if conn.execute("SELECT id FROM usuarios WHERE email = ?", (email,)).fetchone():
            conn.close()
            return render_template('landing.html', erro_cadastro='Esse email já está cadastrado.')

        if tipo == 'cooperativa':
            if request.form.get('codigo_acesso', '') != app.config['CODIGO_CADASTRO_COOPERATIVA']:
                conn.close()
                return render_template('landing.html', erro_cadastro='Código de acesso inválido.')
            conn.execute("INSERT INTO usuarios (email, senha_hash, tipo, fornecedor_id, aprovado, ativo) VALUES (?, ?, 'cooperativa', NULL, TRUE, TRUE)", (email, generate_password_hash(senha)))
            conn.commit()
            conn.close()
            return render_template('landing.html', sucesso_cadastro='Conta criada!')
        else:
            cpf_cnpj = request.form.get('cpf_cnpj', '')
            fornecedor = conn.execute("SELECT id FROM fornecedores WHERE cpf_cnpj = ?", (cpf_cnpj,)).fetchone()
            if not fornecedor:
                conn.close()
                return render_template('landing.html', erro_cadastro='CPF/CNPJ não encontrado.')
            conn.execute("INSERT INTO usuarios (email, senha_hash, tipo, fornecedor_id, aprovado) VALUES (?, ?, 'cooperado', ?, TRUE)", (email, generate_password_hash(senha), fornecedor['id']))
            conn.commit()
            conn.close()
            return render_template('landing.html', sucesso_cadastro='Cadastro realizado com sucesso!')
    return redirect(url_for('landing'))

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/painel/tabelas')
@login_required
def tabelas():
    if current_user.tipo != 'cooperativa': return redirect(url_for('meu_extrato'))
    conn = get_db()
    try:
        # IMPORTANTE: usar .astype(object) ANTES do .where(). Sem isso, o pandas
        # reconverte None para NaN em colunas numéricas (float64), e como NaN é
        # "truthy" em Python, expressões tipo "{{ f.cota or 0 }}" no template não
        # pegam esse caso e o valor aparece como "R$ nan" na tela.
        prefs_df = pd.read_sql_query("SELECT * FROM vw_resultados_por_prefeitura", conn.raw())
        prefeituras = prefs_df.astype(object).where(pd.notna(prefs_df), None).to_dict(orient='records')

        coop_rows = conn.execute("SELECT cc.cliente_id, f.nome, cc.cota FROM contrato_cooperado cc JOIN fornecedores f ON f.id = cc.fornecedor_id").fetchall()
        coop_por_cliente = {}
        for r in coop_rows: coop_por_cliente.setdefault(r['cliente_id'], []).append({'nome': r['nome'], 'cota': float(r['cota'] or 0)})
        for p in prefeituras: p['cooperados'] = coop_por_cliente.get(p.get('id'), [])

        forns_df = pd.read_sql_query("SELECT * FROM vw_pagamentos_resumo", conn.raw())
        fornecedores = forns_df.astype(object).where(pd.notna(forns_df), None).to_dict(orient='records')

        proj_rows = conn.execute("""
            SELECT cc.fornecedor_id, cc.cliente_id, c.nome as cliente_nome, cc.cota, COALESCE(SUM(pf.valor_compra), 0) as entregue
            FROM contrato_cooperado cc JOIN clientes c ON c.id = cc.cliente_id
            LEFT JOIN pagamentos_fornecedores pf ON pf.fornecedor_id = cc.fornecedor_id AND pf.cliente_id = cc.cliente_id
            GROUP BY cc.fornecedor_id, cc.cliente_id, c.nome, cc.cota
        """).fetchall()
        proj_por_fornecedor = {}
        for r in proj_rows: proj_por_fornecedor.setdefault(r['fornecedor_id'], []).append({'cliente_id': r['cliente_id'], 'cliente_nome': r['cliente_nome'], 'cota': float(r['cota'] or 0), 'falta': float(r['cota'] or 0) - float(r['entregue'] or 0)})
        for f in fornecedores: f['projetos'] = proj_por_fornecedor.get(f.get('id'), [])

        notas = conn.execute("""
            SELECT nf.*, c.nome as cliente_nome, COALESCE(cn.total_liquido_fornecedores, 0) as total_custos, (nf.valor_total - COALESCE(cn.total_liquido_fornecedores, 0)) as valor_liquido_nota
            FROM notas_fiscais nf JOIN clientes c ON nf.cliente_id = c.id LEFT JOIN vw_custos_por_nota cn ON cn.nota_id = nf.id ORDER BY nf.data_emissao DESC
        """).fetchall()
        notas = [dict(n) for n in notas]

        pagamentos = conn.execute("""
            SELECT pf.*, f.nome as fornecedor_nome, nf.numero_nota as nota_vinculada
            FROM pagamentos_fornecedores pf JOIN fornecedores f ON pf.fornecedor_id = f.id LEFT JOIN notas_fiscais nf ON pf.nota_id = nf.id ORDER BY pf.data_emissao DESC
        """).fetchall()
        pagamentos = [dict(p) for p in pagamentos]
    finally:
        conn.close()
    return render_template('tabelas.html', prefeituras=prefeituras, fornecedores=fornecedores, notas=notas, pagamentos=pagamentos)

@app.route('/painel/graficos')
@login_required
def graficos():
    if current_user.tipo != 'cooperativa': return redirect(url_for('meu_extrato'))
    return render_template('graficos.html')

@app.route('/painel/visualizador')
@login_required
def visualizador():
    if current_user.tipo != 'cooperativa': return redirect(url_for('meu_extrato'))
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
        resumo = conn.execute("SELECT * FROM vw_pagamentos_resumo WHERE id = ?", (fornecedor_id,)).fetchone()
        pagamentos = conn.execute("""
            SELECT pf.data_emissao, pf.produto_descricao, nf.numero_nota as nota_vinculada, pf.valor_compra, pf.valor_liquido, pf.status
            FROM pagamentos_fornecedores pf LEFT JOIN notas_fiscais nf ON pf.nota_id = nf.id WHERE pf.fornecedor_id = ? ORDER BY pf.data_emissao DESC
        """, (fornecedor_id,)).fetchall()
    finally:
        conn.close()
    if not resumo: resumo = {'cota': 0, 'saldo': 0, 'total_pago': 0, 'pendente': 0}
    return render_template('meu_extrato.html', resumo=dict(resumo), pagamentos=[dict(p) for p in pagamentos])

@app.route('/cooperativa')
@login_required
def painel_cooperativa():
    if current_user.tipo != 'cooperado': return redirect(url_for('tabelas'))
    conn = get_db()
    fornecedor = conn.execute("SELECT nome FROM fornecedores WHERE id = ?", (current_user.fornecedor_id,)).fetchone()
    conn.close()
    nome_cooperado = fornecedor['nome'] if fornecedor else 'Cooperado'
    cotacoes_do_dia = get_todas_cotacoes()
    avisos = []
    return render_template('cooperativa_visao_cooperado.html', cotacoes=cotacoes_do_dia, avisos=avisos, nome_cooperado=nome_cooperado)

# ===========================================================================
# API - CRUD CLIENTES E CONTRATOS
# ===========================================================================
@app.route('/api/clientes_lista')
def api_clientes_lista():
    conn = get_db()
    rows = conn.execute("SELECT id, nome, tipo FROM clientes WHERE ativo = TRUE ORDER BY nome").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/clientes', methods=['POST'])
def api_criar_cliente():
    d = request.json
    nome = str(d.get('nome') or '').strip()
    if not nome: return jsonify({'erro': 'Nome é obrigatório'}), 400
    conn = get_db()
    try:
        row = conn.execute("INSERT INTO clientes (nome, tipo, cidade, prazo_pagamento_dias, valor_total_contrato, ativo) VALUES (?, ?, ?, ?, ?, TRUE) RETURNING id",
            (nome, d.get('tipo', 'prefeitura'), d.get('cidade'), int(d.get('prazo_pagamento_dias') or 30), float(d.get('valor_total_contrato') or 0))).fetchone()
        conn.commit()
        return jsonify({'id': row['id'], 'mensagem': 'Cliente cadastrado com sucesso'}), 201
    except Exception as e: return jsonify({'erro': str(e)}), 400
    finally: conn.close()

@app.route('/api/clientes/<int:id>', methods=['PUT', 'DELETE', 'GET'])
def api_cliente_crud(id):
    conn = get_db()
    try:
        if request.method == 'GET':
            return jsonify(dict(conn.execute("SELECT * FROM clientes WHERE id = ?", (id,)).fetchone() or {}))
        elif request.method == 'DELETE':
            if conn.execute("SELECT COUNT(*) as t FROM notas_fiscais WHERE cliente_id = ?", (id,)).fetchone()['t'] > 0:
                return jsonify({'erro': 'Existem notas vinculadas'}), 400
            conn.execute("DELETE FROM clientes WHERE id = ?", (id,))
            conn.commit()
            return jsonify({'mensagem': 'Excluído com sucesso'})
        elif request.method == 'PUT':
            d = request.json
            conn.execute("UPDATE clientes SET nome=?, tipo=?, cidade=?, prazo_pagamento_dias=?, valor_total_contrato=? WHERE id=?",
                         (d['nome'], d.get('tipo'), d.get('cidade'), int(d.get('prazo_pagamento_dias') or 30), float(d.get('valor_total_contrato') or 0), id))
            conn.commit()
            return jsonify({'mensagem': 'Atualizado com sucesso'})
    except Exception as e: return jsonify({'erro': str(e)}), 400
    finally: conn.close()

@app.route('/api/clientes/<int:id>/cooperados', methods=['GET', 'POST'])
def api_contrato_cooperados(id):
    conn = get_db()
    try:
        if request.method == 'GET':
            rows = conn.execute("""
                SELECT cc.fornecedor_id, f.nome, cc.cota, COALESCE(SUM(pf.valor_compra), 0) as entregue
                FROM contrato_cooperado cc JOIN fornecedores f ON f.id = cc.fornecedor_id
                LEFT JOIN pagamentos_fornecedores pf ON pf.fornecedor_id = cc.fornecedor_id AND pf.cliente_id = cc.cliente_id
                WHERE cc.cliente_id = ? GROUP BY cc.fornecedor_id, f.nome, cc.cota
            """, (id,)).fetchall()
            return jsonify([{'fornecedor_id': r['fornecedor_id'], 'nome': r['nome'], 'cota': float(r['cota'] or 0), 'entregue': float(r['entregue'] or 0), 'falta': float(r['cota'] or 0) - float(r['entregue'] or 0)} for r in rows])
        
        elif request.method == 'POST':
            d = request.json
            cota = float(d.get('cota') or 0)
            conn.execute("INSERT INTO contrato_cooperado (cliente_id, fornecedor_id, cota) VALUES (?, ?, ?) ON CONFLICT (cliente_id, fornecedor_id) DO UPDATE SET cota = EXCLUDED.cota", (id, d['fornecedor_id'], cota))
            conn.commit()
            return jsonify({'mensagem': 'Vinculado com sucesso'})
    except Exception as e: return jsonify({'erro': str(e)}), 400
    finally: conn.close()

@app.route('/api/clientes/<int:cliente_id>/cooperados/<int:fornecedor_id>', methods=['DELETE'])
def api_remover_coop_contrato(cliente_id, fornecedor_id):
    conn = get_db()
    conn.execute("DELETE FROM contrato_cooperado WHERE cliente_id = ? AND fornecedor_id = ?", (cliente_id, fornecedor_id))
    conn.commit()
    conn.close()
    return jsonify({'mensagem': 'Removido do contrato'})

# ===========================================================================
# API - CRUD FORNECEDORES (COOPERADOS)
# ===========================================================================
@app.route('/api/fornecedores_lista')
def api_fornecedores_lista():
    conn = get_db()
    rows = conn.execute("SELECT id, nome FROM fornecedores WHERE ativo = TRUE ORDER BY nome").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/fornecedores', methods=['POST'])
def api_criar_fornecedor():
    d = request.json
    if not d.get('nome'): return jsonify({'erro': 'Nome é obrigatório'}), 400
    conn = get_db()
    try:
        row = conn.execute("INSERT INTO fornecedores (nome, cpf_cnpj, cidade, ativo) VALUES (?, ?, ?, TRUE) RETURNING id",
                           (d['nome'], d.get('cpf_cnpj'), d.get('cidade'))).fetchone()
        conn.commit()
        return jsonify({'id': row['id'], 'mensagem': 'Cadastrado com sucesso'}), 201
    except Exception as e: return jsonify({'erro': str(e)}), 400
    finally: conn.close()

@app.route('/api/fornecedores/<int:id>', methods=['GET', 'PUT', 'DELETE'])
def api_fornecedor_crud(id):
    conn = get_db()
    try:
        if request.method == 'GET':
            return jsonify(dict(conn.execute("SELECT * FROM fornecedores WHERE id = ?", (id,)).fetchone() or {}))
        elif request.method == 'DELETE':
            if conn.execute("SELECT COUNT(*) as t FROM pagamentos_fornecedores WHERE fornecedor_id = ?", (id,)).fetchone()['t'] > 0:
                return jsonify({'erro': 'Existem pagamentos vinculados'}), 400
            conn.execute("DELETE FROM fornecedores WHERE id = ?", (id,))
            conn.commit()
            return jsonify({'mensagem': 'Excluído'})
        elif request.method == 'PUT':
            d = request.json
            conn.execute("UPDATE fornecedores SET nome=?, cpf_cnpj=?, cidade=? WHERE id=?", (d['nome'], d.get('cpf_cnpj'), d.get('cidade'), id))
            conn.commit()
            return jsonify({'mensagem': 'Atualizado'})
    except Exception as e: return jsonify({'erro': str(e)}), 400
    finally: conn.close()

@app.route('/api/fornecedores/<int:id>/contratos', methods=['GET'])
def api_fornecedor_contratos(id):
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT cc.cliente_id, c.nome as cliente_nome, cc.cota, COALESCE(SUM(pf.valor_compra), 0) as entregue
            FROM contrato_cooperado cc JOIN clientes c ON c.id = cc.cliente_id
            LEFT JOIN pagamentos_fornecedores pf ON pf.fornecedor_id = cc.fornecedor_id AND pf.cliente_id = cc.cliente_id
            WHERE cc.fornecedor_id = ? GROUP BY cc.cliente_id, c.nome, cc.cota
        """, (id,)).fetchall()
        return jsonify([{'cliente_id': r['cliente_id'], 'cliente_nome': r['cliente_nome'], 'cota': float(r['cota'] or 0), 'entregue': float(r['entregue'] or 0), 'saldo': float(r['cota'] or 0) - float(r['entregue'] or 0)} for r in rows])
    finally: conn.close()

# ===========================================================================
# API - NOTAS FISCAIS
# ===========================================================================
@app.route('/api/notas_lista')
def api_notas_lista():
    conn = get_db()
    rows = conn.execute("SELECT nf.id, nf.numero_nota, c.nome as cliente_nome, nf.valor_total FROM notas_fiscais nf JOIN clientes c ON nf.cliente_id = c.id ORDER BY nf.data_emissao DESC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/notas', methods=['POST'])
def api_criar_nota():
    d = request.json
    conn = get_db()
    try:
        row = conn.execute("INSERT INTO notas_fiscais (numero_nota, data_emissao, cliente_id, valor_total, peso_liquido, quantidade_cx, valor_frete, status, observacoes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                           (d['numero_nota'], d['data_emissao'], d['cliente_id'], float(d['valor_total']), float(d.get('peso_liquido') or 0), d.get('quantidade_cx'), float(d.get('valor_frete') or 0), d.get('status', 'emitida'), d.get('observacoes'))).fetchone()
        conn.commit()
        return jsonify({'id': row['id'], 'mensagem': 'Nota cadastrada'})
    except Exception as e: return jsonify({'erro': str(e)}), 400
    finally: conn.close()

@app.route('/api/notas/<int:id>', methods=['GET', 'PUT', 'DELETE'])
def api_nota_crud(id):
    conn = get_db()
    try:
        if request.method == 'GET':
            return jsonify(dict(conn.execute("SELECT * FROM notas_fiscais WHERE id = ?", (id,)).fetchone() or {}))
        elif request.method == 'DELETE':
            conn.execute("DELETE FROM notas_fiscais WHERE id = ?", (id,))
            conn.commit()
            return jsonify({'mensagem': 'Excluída'})
        elif request.method == 'PUT':
            d = request.json
            conn.execute("UPDATE notas_fiscais SET numero_nota=?, data_emissao=?, cliente_id=?, valor_total=?, status=?, observacoes=? WHERE id=?",
                         (d['numero_nota'], d['data_emissao'], d['cliente_id'], float(d['valor_total']), d['status'], d.get('observacoes'), id))
            conn.commit()
            return jsonify({'mensagem': 'Atualizada'})
    except Exception as e: return jsonify({'erro': str(e)}), 400
    finally: conn.close()

@app.route('/api/notas/importar_xml', methods=['POST'])
@login_required
def api_importar_notas_xml():
    if current_user.tipo != 'cooperativa': return jsonify({'erro': 'Acesso negado'}), 403
    arquivos = request.files.getlist('arquivos_xml')
    conn = get_db()
    sucesso, erros = 0, []
    clientes_db = [{'id': c['id'], 'nome': c['nome']} for c in conn.execute("SELECT id, nome FROM clientes").fetchall()]

    try:
        for arquivo in arquivos:
            if not arquivo.filename.endswith('.xml'): continue
            fd, caminho_temp = tempfile.mkstemp(suffix='.xml')
            os.close(fd)
            arquivo.save(caminho_temp)
            try:
                dados = parse_nfe_xml(caminho_temp)
                
                nome_limpo_xml = simplificar_texto(dados['cliente_nome'])
                cliente_id = next((c['id'] for c in clientes_db if nome_limpo_xml in simplificar_texto(c['nome']) or simplificar_texto(c['nome']) in nome_limpo_xml), None)
                if not cliente_id:
                    cliente_id = conn.execute("INSERT INTO clientes (nome, tipo) VALUES (?, 'prefeitura') RETURNING id", (dados['cliente_nome'],)).fetchone()['id']
                    clientes_db.append({'id': cliente_id, 'nome': dados['cliente_nome']})

                conn.execute("SAVEPOINT sp_xml")
                conn.execute("INSERT INTO notas_fiscais (numero_nota, data_emissao, cliente_id, valor_total, valor_frete, peso_liquido, status) VALUES (?, ?, ?, ?, ?, ?, 'emitida')",
                             (dados['numero_nota'], dados['data_emissao'], cliente_id, dados['valor_total'], dados['valor_frete'], dados['peso_liquido']))
                conn.execute("RELEASE SAVEPOINT sp_xml")
                sucesso += 1
            except Exception as e:
                conn.execute("ROLLBACK TO SAVEPOINT sp_xml")
                erros.append(f"{arquivo.filename}: {e}")
            finally:
                if os.path.exists(caminho_temp): os.remove(caminho_temp)
        conn.commit()
    finally: conn.close()
    if erros: return jsonify({'erro': f"{sucesso} importadas. Erros:\n" + "\n".join(erros)}), 400
    return jsonify({'mensagem': f'{sucesso} notas importadas via XML!'})

@app.route('/api/notas/importar_pdf', methods=['POST'])
@login_required
def api_importar_notas_pdf():
    if current_user.tipo != 'cooperativa': return jsonify({'erro': 'Acesso negado'}), 403
    arquivos = request.files.getlist('arquivos_pdf')
    conn = get_db()
    sucesso, erros = 0, []
    clientes_db = [{'id': c['id'], 'nome': c['nome']} for c in conn.execute("SELECT id, nome FROM clientes").fetchall()]

    try:
        for arquivo in arquivos:
            if not arquivo.filename.endswith('.pdf'): continue
            fd, caminho_temp = tempfile.mkstemp(suffix='.pdf')
            os.close(fd)
            arquivo.save(caminho_temp)
            try:
                dados = parse_danfe_pdf(caminho_temp)
                
                nome_limpo_pdf = simplificar_texto(dados['cliente_nome'])
                cliente_id = next((c['id'] for c in clientes_db if nome_limpo_pdf in simplificar_texto(c['nome']) or simplificar_texto(c['nome']) in nome_limpo_pdf), None)
                if not cliente_id:
                    cliente_id = conn.execute("INSERT INTO clientes (nome, tipo) VALUES (?, 'prefeitura') RETURNING id", (dados['cliente_nome'],)).fetchone()['id']
                    clientes_db.append({'id': cliente_id, 'nome': dados['cliente_nome']})

                conn.execute("SAVEPOINT sp_pdf")

                # 1. Salva a nota fiscal e pega o ID gerado
                nota_db = conn.execute(
                    "INSERT INTO notas_fiscais (numero_nota, data_emissao, cliente_id, valor_total, valor_frete, peso_liquido, status) VALUES (?, ?, ?, ?, ?, ?, 'emitida') RETURNING id",
                    (dados['numero_nota'], dados['data_emissao'], cliente_id, dados['valor_total'], dados['valor_frete'], dados['peso_liquido'])
                ).fetchone()
                nota_id = nota_db['id']

                # 2. Busca ou cria o "Fornecedor Genérico" para não quebrar o banco
                fornecedor_generico = conn.execute(
                    "SELECT id FROM fornecedores WHERE nome = 'A DEFINIR (Lançamento Automático)'"
                ).fetchone()
                if not fornecedor_generico:
                    novo_forn = conn.execute(
                        "INSERT INTO fornecedores (nome, cpf_cnpj, cidade, ativo) VALUES ('A DEFINIR (Lançamento Automático)', '00000000000', 'Sistema', TRUE) RETURNING id"
                    ).fetchone()
                    fornecedor_id = novo_forn['id']
                else:
                    fornecedor_id = fornecedor_generico['id']

                # 3. Insere os produtos extraídos vinculando ao Fornecedor Genérico
                for prod in dados.get('produtos', []):
                    conn.execute("""
                        INSERT INTO pagamentos_fornecedores
                        (nota_id, cliente_id, fornecedor_id, data_emissao, produto_descricao, peso_liquido, valor_compra, valor_liquido, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pendente')
                    """, (
                        nota_id,
                        cliente_id,
                        fornecedor_id,
                        dados['data_emissao'],
                        prod['descricao'],
                        prod['quantidade'],
                        prod['valor_total'],
                        prod['valor_total']
                    ))

                conn.execute("RELEASE SAVEPOINT sp_pdf")
                sucesso += 1
            except Exception as e:
                conn.execute("ROLLBACK TO SAVEPOINT sp_pdf")
                erros.append(f"{arquivo.filename}: {e}")
            finally:
                if os.path.exists(caminho_temp): os.remove(caminho_temp)
        conn.commit()
    finally: conn.close()
    if erros: return jsonify({'erro': f"{sucesso} importadas. Erros:\n" + "\n".join(erros)}), 400
    return jsonify({'mensagem': f'{sucesso} notas importadas via PDF!'})

# ===========================================================================
# API - CUSTOS (ENTREGAS DE COOPERADOS)
# ===========================================================================
@app.route('/api/custos/detalhe/<int:id>', methods=['GET', 'PUT', 'DELETE'])
def api_custo_crud(id):
    conn = get_db()
    try:
        if request.method == 'GET':
            return jsonify(dict(conn.execute("SELECT * FROM pagamentos_fornecedores WHERE id = ?", (id,)).fetchone() or {}))
        elif request.method == 'DELETE':
            conn.execute('DELETE FROM pagamentos_fornecedores WHERE id = ?', (id,))
            conn.commit()
            return jsonify({'mensagem': 'Excluído com sucesso'})
        elif request.method == 'PUT':
            d = request.json
            v_compra = float(d.get('valor_compra') or 0)
            total_desc = sum([float(d.get(k) or 0) for k in ['funrural', 'taxa_cooperativa', 'desconto_aipro', 'pg_frete_coop', 'pg_caixa_papelao', 'adiantamento', 'descontos_outros']])
            conn.execute("""
                UPDATE pagamentos_fornecedores SET nota_id=?, cliente_id=?, fornecedor_id=?, data_emissao=?, numero_nota_fornecedor=?, status=?, produto_descricao=?, peso_liquido=?, valor_compra=?, valor_liquido=?, funrural=?, taxa_cooperativa=?, desconto_aipro=?, pg_frete_coop=?, pg_caixa_papelao=?, adiantamento=?, descontos_outros=? WHERE id=?
            """, (d.get('nota_id'), d['cliente_id'], d['fornecedor_id'], d['data_emissao'], d.get('numero_nota_fornecedor'), d.get('status', 'pendente'), d.get('produto_descricao'), float(d.get('peso_liquido') or 0), v_compra, v_compra - total_desc, float(d.get('funrural') or 0), float(d.get('taxa_cooperativa') or 0), float(d.get('desconto_aipro') or 0), float(d.get('pg_frete_coop') or 0), float(d.get('pg_caixa_papelao') or 0), float(d.get('adiantamento') or 0), float(d.get('descontos_outros') or 0), id))
            conn.commit()
            return jsonify({'mensagem': 'Atualizado'})
    except Exception as e: return jsonify({'erro': str(e)}), 400
    finally: conn.close()

@app.route('/api/custos', methods=['POST'])
def api_criar_custo():
    d = request.json
    v_compra = float(d['valor_compra'])
    total_desc = sum([float(d.get(k) or 0) for k in ['funrural', 'taxa_cooperativa', 'desconto_aipro', 'pg_frete_coop', 'pg_caixa_papelao', 'adiantamento', 'descontos_outros']])
    conn = get_db()
    try:
        row = conn.execute("""
            INSERT INTO pagamentos_fornecedores (nota_id, cliente_id, fornecedor_id, data_emissao, numero_nota_fornecedor, produto_descricao, peso_liquido, valor_compra, valor_liquido, funrural, taxa_cooperativa, desconto_aipro, pg_frete_coop, pg_caixa_papelao, adiantamento, descontos_outros, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id
        """, (d.get('nota_id'), d['cliente_id'], d['fornecedor_id'], d['data_emissao'], d.get('numero_nota_fornecedor'), d.get('produto_descricao'), float(d.get('peso_liquido') or 0), v_compra, v_compra - total_desc, float(d.get('funrural') or 0), float(d.get('taxa_cooperativa') or 0), float(d.get('desconto_aipro') or 0), float(d.get('pg_frete_coop') or 0), float(d.get('pg_caixa_papelao') or 0), float(d.get('adiantamento') or 0), float(d.get('descontos_outros') or 0), d.get('status', 'pendente'))).fetchone()
        conn.commit()
        return jsonify({'id': row['id'], 'mensagem': 'Criado'})
    except Exception as e: return jsonify({'erro': str(e)}), 400
    finally: conn.close()

# ===========================================================================
# BACKUPS E EXPORTAÇÃO
# ===========================================================================
def _pg_env():
    env = os.environ.copy()
    if DB_CONFIG.get('password'): env['PGPASSWORD'] = DB_CONFIG['password']
    return env

def _pg_conn_args():
    return ['-h', DB_CONFIG['host'], '-p', str(DB_CONFIG['port']), '-U', DB_CONFIG['user'], '-d', DB_CONFIG['dbname']]

@app.route('/api/banco/download')
@login_required
def api_banco_download():
    if current_user.tipo != 'cooperativa': return jsonify({'erro': 'Acesso negado'}), 403
    try:
        resultado = subprocess.run(['pg_dump'] + _pg_conn_args() + ['--no-owner', '--no-privileges'], env=_pg_env(), capture_output=True, text=True, check=True)
    except Exception as e: return jsonify({'erro': str(e)}), 500
    return send_file(io.BytesIO(resultado.stdout.encode('utf-8')), as_attachment=True, download_name='coaipro_dump.sql', mimetype='application/sql')

@app.route('/api/banco/upload', methods=['POST'])
@login_required
def api_banco_upload():
    if current_user.tipo != 'cooperativa': return jsonify({'erro': 'Acesso negado'}), 403
    try:
        subprocess.run(['psql'] + _pg_conn_args(), input=request.files['arquivo'].read().decode('utf-8'), env=_pg_env(), capture_output=True, text=True, check=True)
        return jsonify({'mensagem': 'Banco atualizado!'})
    except Exception as e: return jsonify({'erro': str(e)}), 500

@app.route('/api/banco/excel')
@login_required
def api_banco_excel():
    conn = get_db()
    try:
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            tabelas = [r['name'] for r in conn.execute("SELECT table_name as name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'").fetchall()]
            for t in tabelas:
                try: pd.read_sql_query(f'SELECT * FROM "{t}"', conn.raw()).to_excel(writer, sheet_name=t[:31], index=False)
                except: pass
        output.seek(0)
    finally: conn.close()
    return send_file(output, as_attachment=True, download_name='banco_completo.xlsx', mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000)