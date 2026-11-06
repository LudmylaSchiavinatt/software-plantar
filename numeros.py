"""Conversão de números digitados com separadores brasileiros ou decimais."""
import re
from decimal import Decimal


def numero_decimal(valor):
    if isinstance(valor, bool):
        raise ValueError('Informe um número válido.')
    if isinstance(valor, (int, float, Decimal)):
        n = Decimal(str(valor))
    else:
        texto = re.sub(r'\s+', '', str(valor or '').replace('R$', ''))
        if not texto:
            return Decimal('0')
        if ',' in texto and '.' in texto:
            decimal = ',' if texto.rfind(',') > texto.rfind('.') else '.'
            milhar = '.' if decimal == ',' else ','
            padrao = r'[+-]?\d{1,3}(?:' + re.escape(milhar) + r'\d{3})+' + re.escape(decimal) + r'\d+'
            if not re.fullmatch(padrao, texto):
                raise ValueError('Separadores inválidos. Exemplo: 1.234,56.')
            texto = texto.replace(milhar, '').replace(decimal, '.')
        else:
            if not re.fullmatch(r'[+-]?\d+(?:[.,]\d+)?', texto):
                raise ValueError('Informe um número válido. Exemplo: 1234,56.')
            texto = texto.replace(',', '.')
        n = Decimal(texto)
    if not n.is_finite():
        raise ValueError('Informe um número finito.')
    return n


def numero_float(valor):
    return float(numero_decimal(valor))
