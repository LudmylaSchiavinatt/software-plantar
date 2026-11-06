import re
import pdfplumber
from typing import List, Dict, Any


def normalizar_texto(texto: str) -> str:
    if not texto:
        return ""
    texto = texto.upper().strip()
    texto = re.sub(r'[ÁÀÂÃ]', 'A', texto)
    texto = re.sub(r'[ÉÈÊ]', 'E', texto)
    texto = re.sub(r'[ÍÌÎ]', 'I', texto)
    texto = re.sub(r'[ÓÒÔÕ]', 'O', texto)
    texto = re.sub(r'[ÚÙÛ]', 'U', texto)
    texto = re.sub(r'Ç', 'C', texto)
    return re.sub(r'\s+', ' ', texto)


def parse_valor_br(val_str: str) -> float:
    if not val_str:
        return 0.0
    val_limpo = re.sub(r'[^\d,.-]', '', val_str.strip())
    if not val_limpo:
        return 0.0
    val_limpo = val_limpo.replace('.', '').replace(',', '.')
    try:
        return float(val_limpo)
    except ValueError:
        return 0.0


def parse_data_br(data_str: str) -> str:
    if not data_str:
        return ""
    m = re.search(r'(\d{2})/(\d{2})/(\d{4})', data_str)
    if m:
        dia, mes, ano = m.groups()
        return f"{ano}-{mes}-{dia}"
    return ""


def normalizar_produto(nome_prod: str) -> str:
    prod = normalizar_texto(nome_prod)
    # Remove marcas de código e caracteres especiais no início do nome
    prod = re.sub(r'^[0-9\.\-\|]+\s*', '', prod)
    return prod.strip()


# Rótulos de cabeçalho de tabela do DANFE que às vezes ficam colados ao
# nome do emitente/destinatário por causa da ordem em que o pdfplumber
# extrai o texto (colunas do PDF nem sempre viram linhas na ordem visual).
# Ex.: "DESTINATÁRIO/REMETENTE NOME/RAZÃO SOCIAL CNPJ/CPF DATA DA EMISSÃO
# EUGENIO DE MORAES" -> o nome de verdade é só "EUGENIO DE MORAES".
_MARCADORES_CABECALHO_NOME = [
    r'(?:DES)?TINAT[ÁA]RIO\s*/?\s*REMETENTE',
    r'REMETENTE\s*/?\s*DESTINAT[ÁA]RIO',
    r'NOME\s*/?\s*RAZ[ÃA]O\s*SOCIAL',
    r'CNPJ\s*/?\s*CPF',
    r'DATA\s*DA\s*EMISS[ÃA]O',
    r'DATA\s*(?:DE\s*)?ENTRADA\s*/?\s*SA[IÍ]DA',
]


def limpar_nome_extraido(nome_bruto: str, max_len: int = 100) -> str:
    """
    Remove rótulos de cabeçalho de tabela que grudaram no nome extraído
    e garante que o resultado caiba em colunas VARCHAR(100) do banco
    (clientes.nome, fornecedores.nome, produtos.nome).
    """
    if not nome_bruto:
        return nome_bruto

    limpo = nome_bruto
    for marcador in _MARCADORES_CABECALHO_NOME:
        limpo = re.sub(marcador, ' ', limpo, flags=re.IGNORECASE)

    limpo = re.sub(r'\s+', ' ', limpo).strip(' /-.')

    # Rede de segurança: se mesmo depois de limpar ainda sobrou algo maior
    # que o limite da coluna (padrão do PDF não reconhecido), trunca em
    # vez de deixar a gravação inteira falhar.
    if len(limpo) > max_len:
        limpo = limpo[:max_len].strip()

    return limpo


def parse_danfe_pagina(texto_pagina: str) -> Dict[str, Any]:
    linhas = [l.strip() for l in texto_pagina.split('\n') if l.strip()]
    texto_full = " ".join(linhas)

    # --- Chave de Acesso ---
    chave_match = re.search(r'(\d{4}\s*){10}\d{4}', texto_full) or re.search(r'\d{44}', texto_full)
    chave = re.sub(r'\s+', '', chave_match.group(0)) if chave_match else ""

    # --- Número da Nota ---
    numero_match = re.search(r'N[º°]?\s*[:.]?\s*(\d{1,3}(?:\.\d{3})*|\d+)', texto_full, re.IGNORECASE)
    numero_nota = numero_match.group(1).replace('.', '') if numero_match else ""

    # --- Tipo de Operação (COMPRA / VENDA) ---
    natureza_match = re.search(
        r'NATUREZA DA OPERA[CÇ][AÃ]O.*?\b(COMPRA|VENDA|ENTRADA|SAIDA|SA[IÍ]DA)\b',
        texto_full, re.IGNORECASE
    )
    if natureza_match:
        valor_natureza = natureza_match.group(1).upper()
        tipo_op = "ENTRADA" if valor_natureza in ("COMPRA", "ENTRADA") else "SAIDA"
    else:
        digito_match = re.search(r'0\s*-\s*Entrada\s*(\d)\s*1\s*-\s*Sa[ií]da', texto_full, re.IGNORECASE)
        tipo_op = "SAIDA" if digito_match and digito_match.group(1) == "1" else "ENTRADA"

    # --- Documentos e Nomes ---
    cpf_cnpj_matches = re.findall(r'\d{2,3}\.\d{3}\.\d{3}/\d{4}-\d{2}|\d{3}\.\d{3}\.\d{3}-\d{2}', texto_full)
    emitente_doc = cpf_cnpj_matches[0] if len(cpf_cnpj_matches) > 0 else ""
    destinatario_doc = cpf_cnpj_matches[1] if len(cpf_cnpj_matches) > 1 else ""

    # O bloco DESTINATÁRIO/REMETENTE do DANFE tem cabeçalho de colunas
    # "NOME/RAZÃO SOCIAL  CNPJ/CPF  DATA DA EMISSÃO" seguido pela linha
    # de dados real "EUGENIO DE MORAES  123.456.789-01  07/09/2026".
    # "DATA DA EMISSÃO" é o ÚLTIMO rótulo desse cabeçalho e sempre
    # precede imediatamente o trio nome/CNPJ/data de verdade - por isso
    # ancoramos a busca do nome logo depois dele, em vez de tentar
    # adivinhar o início andando para trás a partir do CNPJ+data com um
    # limite de tamanho (abordagem antiga, frágil: quando o cabeçalho
    # era mais longo que o limite, o início do casamento deslizava para
    # o meio de uma palavra do próprio cabeçalho, e reduzir o limite ou
    # remover os marcadores só deslocava o problema para outro trecho
    # do cabeçalho que passasse a caber).
    cabecalho_data_emissao = re.search(r'DATA\s*DA\s*EMISS[ÃA]O', texto_full, re.IGNORECASE)
    texto_apos_cabecalho = texto_full[cabecalho_data_emissao.end():] if cabecalho_data_emissao else texto_full

    nome_match = re.search(
        r'([A-ZÀ-Ú0-9 .\-/]{3,100}?)\s+(?:\d{2,3}\.\d{3}\.\d{3}/\d{4}-\d{2}|\d{3}\.\d{3}\.\d{3}-\d{2})\s+\d{2}/\d{2}/\d{4}',
        texto_apos_cabecalho
    )
    contraparte_nome = limpar_nome_extraido(nome_match.group(1).strip()) if nome_match else ""

    # Município no bloco do destinatário, antes de FATURA/CÁLCULO.
    cidade_destinatario = ""
    bloco_destino = re.split(r'DESTINAT[ÁA]RIO/REMETENTE', texto_pagina, flags=re.I)[-1]
    bloco_destino = re.split(r'FATURA|C[ÁA]LCULO DO IMPOSTO', bloco_destino, flags=re.I)[0]
    linhas_destino = bloco_destino.splitlines()
    for indice, linha in enumerate(linhas_destino):
        if re.search(r'MUN[IÍ]C[IÍ]PIO', linha, re.I) and indice + 1 < len(linhas_destino):
            dados_cidade = linhas_destino[indice + 1].strip()
            cidade_match = re.match(r'(.+?)\s+(?:\(?\d[\d ()-]*\s+)?[A-Z]{2}(?:\s|$)', dados_cidade)
            if cidade_match:
                cidade_destinatario = cidade_match.group(1).strip()
            break

    data_emissao_match = re.search(r'(\d{2}/\d{2}/\d{4})', texto_full)
    data_emissao = parse_data_br(data_emissao_match.group(1)) if data_emissao_match else ""

    # --- Extração da Área de Itens ---
    itens_area = texto_full
    inicio_tabela = re.search(r'ALIQ\.?\s*IPI|AL[IÍ]Q\.?\s*IPI', texto_full, re.IGNORECASE)
    fim_tabela = re.search(r'C[AÁ]LCULO DO ISSQN|DADOS ADICIONAIS', texto_full, re.IGNORECASE)
    if inicio_tabela and fim_tabela and fim_tabela.start() > inicio_tabela.end():
        itens_area = texto_full[inicio_tabela.end():fim_tabela.start()]
    elif fim_tabela:
        itens_area = texto_full[:fim_tabela.start()]

    # Suporta linhas com ou sem separador de coluna '|'
    regex_item = re.compile(
        r'(?P<codigo>\d+(?:[.,]\d+)?)\s*\|?\s*'
        # Limite de tamanho na descrição (produto real nunca passa de ~60
        # caracteres): sem esse limite, quando o cabeçalho da tabela de
        # itens vem embaralhado no texto extraído pelo pdfplumber (colunas
        # fora de ordem), a descrição "engole" o cabeçalho inteiro até
        # achar o primeiro NCM de 8 dígitos válido mais à frente.
        r'(?P<descricao>[A-ZÀ-Ú0-9\s/%\-\.\(\)]{1,60}?)\s*\|?\s*'
        r'(?P<ncm>\d{8})\s*\|?\s*'
        r'(?P<cst>\d{3,4})\s*\|?\s*'
        r'(?P<cfop>\d{4})\s*\|?\s*'
        r'(?P<unidade>KG|UN|CX|MACO|PC|SCA|FD)\s*\|?\s*'
        r'(?P<val1>[\d\.,]+)\s*\|?\s*'
        r'(?P<val2>[\d\.,]+)\s*\|?\s*'
        r'(?P<val3>[\d\.,]+)',
        re.IGNORECASE
    )

    itens = []
    for match in regex_item.finditer(itens_area):
        d = match.groupdict()
        qtd = parse_valor_br(d['val1'])
        v2 = parse_valor_br(d['val2'])
        v3 = parse_valor_br(d['val3'])

        # Correção inteligente para evitar inversão de Vlr Unitário e Vlr Total
        if abs((qtd * v2) - v3) < 0.1:
            vunit, vtot = v2, v3
        elif abs((qtd * v3) - v2) < 0.1:
            vunit, vtot = v3, v2
        else:
            vunit, vtot = v2, v3

        itens.append({
            'codigo': d.get('codigo') or '',
            'produto': normalizar_produto(d['descricao']),
            'ncm': d['ncm'],
            'cfop': d['cfop'],
            'unidade': d['unidade'].upper(),
            'quantidade': qtd,
            'valor_unitario': vunit,
            'valor_total': vtot
        })

    contrato_match = re.search(r'\bCONTRATO\s*(?:N[º°O.]?\s*)?[:#-]?\s*(\d+\s*/\s*\d{4})', texto_full, re.I)
    contrato_referencia = re.sub(r'\s+', '', contrato_match.group(1)) if contrato_match else ''

    # --- Informações Adicionais para Repasse de Cooperado ---
    cooperado_cpf = ""
    funrural_valor = 0.0
    valor_liquido = 0.0
    cliente_destino_nome = ""

    cpf_coop_match = re.search(r'CPF\s*:?\s*(\d{3}\.\d{3}\.\d{3}-\d{2})', texto_full)
    if cpf_coop_match:
        cooperado_cpf = cpf_coop_match.group(1)

    funrural_match = re.search(r'FUNRURAL retido.*?R\$\s*([\d\.,]+)', texto_full, re.IGNORECASE)
    if funrural_match:
        funrural_valor = parse_valor_br(funrural_match.group(1))

    liquido_match = re.search(r'Valor l[ií]quido\s*:?\s*R\$\s*([\d\.,]+)', texto_full, re.IGNORECASE)
    if liquido_match:
        valor_liquido = parse_valor_br(liquido_match.group(1))

    cliente_destino_match = re.search(
        r'entrega de produtos para\s+([A-ZÀ-Ú0-9 .\-/]{3,80}?)\s+pelo cooperado',
        texto_full, re.IGNORECASE
    )
    if cliente_destino_match:
        cliente_destino_nome = limpar_nome_extraido(cliente_destino_match.group(1).strip())

    return {
        'chave_acesso': chave,
        'contrato_referencia': contrato_referencia,
        'numero_nota': numero_nota,
        'tipo': tipo_op,
        'emitente_doc': emitente_doc,
        'destinatario_doc': destinatario_doc,
        'contraparte_nome': contraparte_nome,
        'cidade_destinatario': cidade_destinatario,
        'data_emissao': data_emissao,
        'cooperado_cpf': cooperado_cpf,
        'funrural_valor': funrural_valor,
        'valor_liquido': valor_liquido,
        'cliente_destino_nome': cliente_destino_nome,
        'itens': itens,
        'texto_bruto': texto_full
    }


def parse_danfe_pdf(filepath: str) -> List[Dict[str, Any]]:
    notas = []
    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            texto = page.extract_text() or ""
            if texto:
                nota = parse_danfe_pagina(texto)
                if nota['chave_acesso'] or nota['numero_nota'] or nota['itens']:
                    notas.append(nota)
    return notas


def reconciliar_notas(notas_extraidas: List[Dict[str, Any]]) -> Dict[str, Any]:
    compras = []
    vendas = []

    for nota in notas_extraidas:
        for item in nota['itens']:
            registro = {
                'chave_acesso': nota['chave_acesso'],
                'contrato_referencia': nota.get('contrato_referencia', ''),
                'numero_nota': nota['numero_nota'],
                'emitente_doc': nota['emitente_doc'],
                'destinatario_doc': nota['destinatario_doc'],
                'contraparte_nome': nota.get('contraparte_nome', ''),
                'cidade_destinatario': nota.get('cidade_destinatario', ''),
                'data_emissao': nota.get('data_emissao', ''),
                'cooperado_cpf': nota.get('cooperado_cpf', ''),
                'funrural_valor': nota.get('funrural_valor', 0.0),
                'valor_liquido': nota.get('valor_liquido', 0.0),
                'valor_liquido_nota': nota.get('valor_liquido', 0.0),
                'cliente_destino_nome': nota.get('cliente_destino_nome', ''),
                'produto': item['produto'],
                'quantidade': item['quantidade'],
                'valor_unitario': item['valor_unitario'],
                'valor_total': item['valor_total'],
                'tipo': nota['tipo'],
                'status': 'pendente'
            }
            if nota['tipo'] == 'ENTRADA':
                compras.append(registro)
            else:
                vendas.append(registro)

    # Retorno estruturado exigido pela API no Flask
    return {
        'compras': compras,
        'vendas': vendas,
        'total_registros': len(compras) + len(vendas)
    }