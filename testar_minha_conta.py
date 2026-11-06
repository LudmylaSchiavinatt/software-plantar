"""Testes HTTP reais com Flask e banco simulado; não acessam PostgreSQL.
Execute: python -m unittest testar_minha_conta -v
"""
import ast
import re
from copy import deepcopy
from functools import wraps
from pathlib import Path
import unittest
from flask import Flask, jsonify, render_template
from flask_login import LoginManager, UserMixin, current_user
from werkzeug.security import check_password_hash, generate_password_hash
from conta_seguranca import instalar_minha_conta, validar_codigo, conferir_csrf_conta

ROOT=Path(__file__).parent
PASSWORD='SenhaOriginal123!'
CODE='CodigoOriginal123!'
PASSWORD_HASH=generate_password_hash(PASSWORD)
CODE_HASH=generate_password_hash(CODE)

class Result:
    def __init__(self, row=None): self.row=row
    def fetchone(self): return self.row
    def fetchall(self): return [] if self.row is None else [self.row]

class Banco:
    def __init__(self):
        self.conta=dict(id=7,usuario_id=9,nome='Cooperativa Teste',cnpj='12345678000190',email='teste@coop.com',senha_hash=PASSWORD_HASH,codigo_seguranca_hash=CODE_HASH,ativo=True,aprovado=True,bloqueada=False,tentativas=0)
        self.calls=[];self.commits=0;self.rollbacks=0;self.scope=None
    def execute(self,sql,params=()):
        self.calls.append((sql,params));q=' '.join(sql.split())
        if q.startswith('ALTER TABLE'): return Result()
        if q.startswith('SELECT c.id,'):
            self.scope=params
            return Result(deepcopy(self.conta)) if tuple(map(str,params))==('7','9') else Result()
        if q.startswith('SELECT 1 FROM cooperativas'): return Result()
        if q.startswith('SELECT id FROM cooperativas') or q.startswith('SELECT id FROM usuarios'): return Result()
        if q.startswith('UPDATE cooperativas SET codigo_seguranca_hash'):
            self.conta['codigo_seguranca_hash']=params[0];return Result()
        if q.startswith('UPDATE cooperativas SET nome'):
            self.conta.update(nome=params[0],cnpj=params[1]);return Result()
        if q.startswith('UPDATE usuarios SET email'):
            self.conta['email']=params[0];return Result()
        if q.startswith('UPDATE usuarios SET senha_hash'):
            self.conta['senha_hash']=params[0];return Result()
        if q.startswith('UPDATE usuarios SET conta_tentativas = 0'):
            self.conta.update(tentativas=0,bloqueada=False);return Result()
        if q.startswith('UPDATE usuarios SET conta_tentativas = CASE'):
            self.conta['tentativas']+=1;self.conta['bloqueada']=self.conta['tentativas']>=5;return Result()
        if q.startswith('INSERT INTO cooperativas'):
            self.insert_coop=params;return Result({'id':7})
        if q.startswith('INSERT INTO usuarios'):
            self.insert_user=params;return Result()
        raise AssertionError('Consulta não coberta no banco simulado: '+q)
    def commit(self):self.commits+=1
    def rollback(self):self.rollbacks+=1
    def close(self):pass

class User(UserMixin):
    id='9';cooperativa_id=7;tipo='admin';email='teste@coop.com';aprovado=True

class MinhaConta(unittest.TestCase):
    def setUp(self):
        self.db=Banco()
        self.app=Flask(__name__,template_folder=str(ROOT/'templates'))
        self.app.config.update(TESTING=True,SECRET_KEY='test-only')
        lm=LoginManager(self.app)
        self.user=User()
        lm.user_loader(lambda uid:self.user)
        self.app.add_url_rule('/', 'landing', lambda:render_template('landing.html'))
        for endpoint in ['logout','tabelas','meu_extrato','login']:
            self.app.add_url_rule('/'+endpoint,endpoint,lambda:'OK')
        def required(f):
            @wraps(f)
            def call(*args,**kwargs):
                if not current_user.is_authenticated:return jsonify(erro='Não autenticado'),401
                if current_user.tipo!='admin':return jsonify(erro='Restrito'),403
                return f(*args,**kwargs)
            return call
        instalar_minha_conta(self.app,lambda:self.db,required)
        # Testa também a função de cadastro de produção sem importar migrações do app.
        node=next(n for n in ast.parse((ROOT/'app.py').read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='cadastro')
        from flask import request,redirect,url_for
        ns=dict(re=re,app=self.app,request=request,redirect=redirect,url_for=url_for,render_template=render_template,get_db=lambda:self.db,validar_codigo=validar_codigo,conferir_csrf_conta=conferir_csrf_conta,generate_password_hash=generate_password_hash)
        exec(compile(ast.Module(body=[node],type_ignores=[]),'<cadastro>','exec'),ns)
        self.client=self.app.test_client()
        with self.client.session_transaction() as session:
            session['_user_id']='9';session['_fresh']=True;session['csrf_conta']='test-token'
        self.db.calls=[]
    def post(self,**data):
        return self.client.post('/minha-conta',data=dict(csrf_token='test-token',**data))
    def test_link_minha_conta_visivel_para_admin(self):
        self.assertIn(b'href="/minha-conta"', self.client.get('/').data)
    def test_pagina_nao_expoe_hash(self):
        r=self.client.get('/minha-conta');self.assertEqual(r.status_code,200)
        self.assertNotIn(CODE_HASH.encode(),r.data);self.assertNotIn(PASSWORD_HASH.encode(),r.data)
        self.assertIn(b'Minha conta',r.data)
    def test_anonimo_e_cooperado_bloqueados(self):
        self.user.tipo='cooperado';self.assertEqual(self.client.get('/minha-conta').status_code,403)
        with self.client.session_transaction() as session:session.clear()
        self.assertEqual(self.client.get('/minha-conta').status_code,401)
    def test_csrf_obrigatorio_antes_do_banco(self):
        self.assertEqual(self.client.post('/minha-conta',data={'acao':'codigo'}).status_code,400)
        self.assertEqual(self.db.calls,[])
    def test_troca_codigo_exige_senha(self):
        self.post(acao='codigo',senha_atual='errada',novo_codigo='NovoCodigo123!',confirma_codigo='NovoCodigo123!')
        self.assertEqual(self.db.conta['codigo_seguranca_hash'],CODE_HASH)
    def test_troca_codigo_hash_e_invalida_antigo(self):
        self.post(acao='codigo',senha_atual=PASSWORD,novo_codigo='NovoCodigo123!',confirma_codigo='NovoCodigo123!')
        h=self.db.conta['codigo_seguranca_hash']
        self.assertTrue(check_password_hash(h,'NovoCodigo123!'));self.assertFalse(check_password_hash(h,CODE))
        self.assertNotEqual(h,'NovoCodigo123!')
    def test_confirmacao_divergente_nao_salva(self):
        self.post(acao='codigo',senha_atual=PASSWORD,novo_codigo='NovoCodigo123!',confirma_codigo='outroCodigo')
        self.assertEqual(self.db.conta['codigo_seguranca_hash'],CODE_HASH)
    def test_conta_antiga_define_primeiro_codigo(self):
        self.db.conta['codigo_seguranca_hash']=None
        self.post(acao='codigo',senha_atual=PASSWORD,novo_codigo='NovoCodigo123!',confirma_codigo='NovoCodigo123!')
        self.assertTrue(check_password_hash(self.db.conta['codigo_seguranca_hash'],'NovoCodigo123!'))
    def test_dados_exigem_codigo_correto(self):
        self.post(acao='dados',codigo_atual='incorreto',nome='Alterado',cnpj='12345678000190',email='novo@coop.com')
        self.assertEqual(self.db.conta['nome'],'Cooperativa Teste')
        self.post(acao='dados',codigo_atual=CODE,nome='Alterado',cnpj='12345678000190',email='novo@coop.com',cooperativa_id='999',usuario_id='999')
        self.assertEqual(self.db.conta['nome'],'Alterado');self.assertEqual(self.db.scope,(7,'9'))
    def test_troca_senha_exige_dois_segredos_e_sai(self):
        self.post(acao='senha',senha_atual=PASSWORD,codigo_atual='errado',nova_senha='OutraSenha123!',confirma_senha='OutraSenha123!')
        self.assertEqual(self.db.conta['senha_hash'],PASSWORD_HASH)
        self.post(acao='senha',senha_atual=PASSWORD,codigo_atual=CODE,nova_senha='OutraSenha123!',confirma_senha='OutraSenha123!')
        self.assertTrue(check_password_hash(self.db.conta['senha_hash'],'OutraSenha123!'))
        with self.client.session_transaction() as session:self.assertNotIn('_user_id',session)
    def test_bloqueio_apos_cinco_erros(self):
        for _ in range(5):self.post(acao='codigo',senha_atual='errada')
        self.assertTrue(self.db.conta['bloqueada'])
        self.post(acao='codigo',senha_atual=PASSWORD,novo_codigo='NovoCodigo123!',confirma_codigo='NovoCodigo123!')
        self.assertEqual(self.db.conta['codigo_seguranca_hash'],CODE_HASH)
    def test_codigo_diferente_da_senha(self):
        self.assertIsNotNone(validar_codigo(PASSWORD,PASSWORD,PASSWORD))
    def test_cadastro_salva_codigo_protegido(self):
        r=self.client.post('/cadastro',data=dict(csrf_token='test-token',tipo='cooperativa',email='nova@coop.com',senha=PASSWORD,nome_cooperativa='Nova',cnpj_cooperativa='12345678000190',codigo_seguranca=CODE,confirma_codigo=CODE))
        self.assertEqual(r.status_code,200)
        self.assertTrue(check_password_hash(self.db.insert_coop[2],CODE))
        self.assertNotEqual(self.db.insert_coop[2],CODE)
    def test_cadastro_confirmacao_incorreta(self):
        r=self.client.post('/cadastro',data=dict(csrf_token='test-token',tipo='cooperativa',email='nova@coop.com',senha=PASSWORD,codigo_seguranca=CODE,confirma_codigo='outroCodigo'))
        self.assertEqual(r.status_code,200);self.assertFalse(hasattr(self.db,'insert_coop'))
    def test_cadastro_csrf(self):
        self.assertEqual(self.client.post('/cadastro',data={}).status_code,400)

if __name__=='__main__':unittest.main(verbosity=2)
