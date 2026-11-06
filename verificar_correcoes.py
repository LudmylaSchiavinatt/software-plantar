"""Verificações isoladas: python verificar_correcoes.py. Não acessa o banco."""
import ast
import io
import re
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE = (ROOT / 'app.py').read_text()
TREE = ast.parse(SOURCE)
NAMES = {'_numero_importacao', '_data_importacao', '_calcular_valor_liquido_custo', '_validar_custo', '_txt', 'parse_nfe_xml'}
NAMESPACE = dict(Decimal=Decimal, datetime=datetime, ET=ET, NFE_NS={'nfe': 'http://www.portalfiscal.inf.br/nfe'})
for node in TREE.body:
    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in ('CAMPOS_DESCONTO_EXTRA', '_ROTULOS_NUMERICOS_CUSTO') for t in node.targets):
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<isolated>', 'exec'), NAMESPACE)
for node in TREE.body:
    if isinstance(node, ast.FunctionDef) and node.name in NAMES:
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<isolated>', 'exec'), NAMESPACE)

class Correcoes(unittest.TestCase):
    def test_numeros_brasileiros(self):
        f=NAMESPACE['_numero_importacao']
        self.assertEqual(f('R$ 1.234,56', 'Valor'), Decimal('1234.56'))
        self.assertEqual(f('1234.56', 'Valor'), Decimal('1234.56'))
        for v in ['nan', 'inf', '-10', 'abc']:
            with self.assertRaises(ValueError): f(v, 'Valor')
    def test_datas(self):
        f=NAMESPACE['_data_importacao']
        self.assertEqual(f('29/10/2026'), '2026-10-29')
        with self.assertRaises(ValueError): f('31/02/2026')
    def test_descontos_antes_ignorados(self):
        d={'valor_compra': '100', 'funrural': '1.5', 'pg_extra': '10', 'pg_caixa_entrega': '4'}
        self.assertEqual(NAMESPACE['_calcular_valor_liquido_custo'](d), 84.5)
    def test_validacao_custos(self):
        d=dict(nota_id=1, fornecedor_id=2, data_emissao='2026-10-29', valor_compra='100')
        f=NAMESPACE['_validar_custo']
        self.assertIsNone(f(d))
        for valor in ['nan', 'inf', '-1', '101']:
            self.assertIsNotNone(f(dict(d, pg_extra=valor)))
    def test_xml_produtos(self):
        xml=b'''<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe"><NFe><infNFe Id="NFe123"><ide><nNF>10</nNF><dhEmi>2026-10-29T12:00:00-03:00</dhEmi></ide><dest><xNome>Cliente</xNome></dest><total><ICMSTot><vNF>20.00</vNF></ICMSTot></total><det><prod><xProd>Banana</xProd><uCom>KG</uCom><qCom>2</qCom><vUnCom>10</vUnCom><vProd>20</vProd></prod></det></infNFe></NFe></nfeProc>'''
        nota=NAMESPACE['parse_nfe_xml'](io.BytesIO(xml))
        self.assertEqual(nota['numero_nota'], '10')
        self.assertEqual(nota['itens'][0]['descricao'], 'Banana')
        self.assertEqual(nota['itens'][0]['unidade'], 'KG')
    def test_rotas_das_telas(self):
        routes=[]
        for node in ast.walk(TREE):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr=='route' and node.args:
                route=ast.literal_eval(node.args[0])
                route=re.sub(r'<(?:int:)?\w+>', '[^/]+', route)
                routes.append(re.compile('^'+route+'$'))
        for path in (ROOT/'templates').glob('*.html'):
            for url in re.findall(r'fetch\([\'"`]([^\'"`]+)', path.read_text()):
                url=re.sub(r'\$\{[^}]+\}', '1', url).split('?')[0]
                self.assertTrue(any(p.fullmatch(url) for p in routes), (path.name, url))
    def test_preserva_estilos(self):
        originals=ROOT.parent/'upload'
        if originals.exists():
            for p in (ROOT/'templates').glob('*.html'):
                if not (originals/p.name).exists(): continue
                self.assertEqual(re.findall(r'<style[^>]*>.*?</style>', p.read_text(), re.S), re.findall(r'<style[^>]*>.*?</style>', (originals/p.name).read_text(), re.S))

if __name__ == '__main__': unittest.main(verbosity=2)
