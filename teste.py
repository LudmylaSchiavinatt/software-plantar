"""
Parser de DANFE (Cooperativa) que distingue COMPRA (entrada do cooperado)
de VENDA (saída para o cliente/prefeitura) e já faz o casamento
cooperado -> cliente do contrato -> venda, antes de contar qualquer coisa
como "entregue" na cota.
"""
import re
import pdfplumber


def normalizar_texto(texto: str) -> str:
    return re.sub(r'\s+', ' ', (texto or '').replace('\xa0', ' ')).strip()


def parse_valor_br(valor_str: str) -> float:
    if not valor_str:
        return 0.0
    v = valor_str.strip().replace('.', '').replace(',', '.')
    try:
        return float(v)
    except ValueError:
        return 0.0


def parse_danfe_pagina(texto_bruto: str) -> dict:
    """Recebe o texto de UMA página/nota (já extraído do PDF) e devolve
    um dicionário estruturado."""
    texto = normalizar_texto(texto_bruto)

    dados = {
        'numero_nota': None,
        'serie': None,
        'natureza_operacao': None,      # 'COMPRA' ou 'VENDA'
        'chave_acesso': None,
        'data_emissao': None,
        'outra_parte_nome': None,       # cooperado (compra) OU cliente (venda)
        'outra_parte_cnpj_cpf': None,
        'valor_total_nota': None,
        'produtos': [],
        # específico de COMPRA
        'cooperado_numero': None,
        'cooperado_cpf': None,
        'cliente_contrato_nome': None,  # extraído do texto complementar
        'funrural_valor': None,
        'valor_liquido_informado': None,
        'data_entrega_compra': None,
        # específico de VENDA
        'contrato_numero': None,
        'empenho_numero': None,
        'pedido_numero': None,
        'data_entrega_venda': None,
    }

    m = re.search(r'N[ºo°]\s*([\d]{3}\.[\d]{3}\.[\d]{3})', texto)
    if m:
        dados['numero_nota'] = m.group(1).replace('.', '')
    m = re.search(r'S[ÉE]RIE:\s*(\d+)', texto)
    if m:
        dados['serie'] = m.group(1)

    m = re.search(r'\b(COMPRA|VENDA)\b', texto)
    if m:
        dados['natureza_operacao'] = m.group(1)

    m = re.search(r'CHAVE DE ACESSO\s*((?:\d{4}\s+){10}\d{4})', texto)
    if m:
        dados['chave_acesso'] = re.sub(r'\s+', '', m.group(1))

    m = re.search(
        r'NOME/RAZ[ÃA]O SOCIAL[^:]*?DATA DA EMISS[ÃA]O\s+'
        r'([A-ZÀ-Ú][A-ZÀ-Ú0-9 \.\-]{4,60}?)\s+'
        r'(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\d{3}\.\d{3}\.\d{3}-\d{2})\s+'
        r'(\d{2}/\d{2}/\d{4})',
        texto
    )
    if m:
        dados['outra_parte_nome'] = m.group(1).strip()
        dados['outra_parte_cnpj_cpf'] = m.group(2)
        dados['data_emissao'] = m.group(3)

    m = re.search(r'VALOR TOTAL DA NOTA\s+[\d\.,]+\s+[\d\.,]+\s+[\d\.,]+\s+[\d\.,]+\s+[\d\.,]+\s+([\d\.]+,\d{2})', texto)
    if not m:
        m = re.search(r'VALOR TOTAL DA NOTA\D*?([\d\.]+,\d{2})\s*$', texto)
    if m:
        dados['valor_total_nota'] = parse_valor_br(m.group(1))

    # Produtos: <codigo> <descricao...> <ncm 8 digitos> <cst 3> <cfop 4> <unid> <qtd> <vlr_unit> <vlr_total>
    for pm in re.finditer(
        r'(\d+(?:\.\d+)?)\s+([A-ZÀ-Ú][A-ZÀ-Ú ]+?)\s+(\d{8})\s+(\d{3})\s+(\d{4})\s+'
        r'(kg|un|cx)\s+([\d\.]+,\d{4})\s+([\d\.]+,\d{4})\s+([\d\.]+,\d{2})',
        texto, flags=re.IGNORECASE
    ):
        dados['produtos'].append({
            'codigo': pm.group(1),
            'descricao': pm.group(2).strip(),
            'ncm': pm.group(3),
            'cst': pm.group(4),
            'cfop': pm.group(5),
            'unidade': pm.group(6),
            'quantidade': parse_valor_br(pm.group(7)),
            'valor_unitario': parse_valor_br(pm.group(8)),
            'valor_total': parse_valor_br(pm.group(9)),
        })

    if dados['natureza_operacao'] == 'COMPRA':
        m = re.search(r'entrega de produtos para\s+([A-ZÀ-Ú0-9 \.\-]+?)\s+pelo cooperado', texto)
        if m:
            dados['cliente_contrato_nome'] = m.group(1).strip()

        m = re.search(r'cooperado No\s*(\d+)', texto)
        if m:
            dados['cooperado_numero'] = m.group(1)

        m = re.search(r'CPF:\s*(\d{3}\.\d{3}\.\d{3}-\d{2})', texto)
        if m:
            dados['cooperado_cpf'] = m.group(1)

        m = re.search(r'no dia\s*(\d{2}/\d{2}/\d{4})', texto)
        if m:
            dados['data_entrega_compra'] = m.group(1)

        m = re.search(r'FUNRURAL retido\s*\(\s*[\d,]+%\)\s*:?\s*R\$\s*([\d\.]+,\d{2})', texto)
        if m:
            dados['funrural_valor'] = parse_valor_br(m.group(1))

        m = re.search(r'Valor liquido:\s*R\$\s*([\d\.]+,\d{2})', texto)
        if m:
            dados['valor_liquido_informado'] = parse_valor_br(m.group(1))

    if dados['natureza_operacao'] == 'VENDA':
        m = re.search(r'CONTRATO\s*([\d/]+)', texto)
        if m:
            dados['contrato_numero'] = m.group(1)
        m = re.search(r'EMPENHO\s*([\d/]+)', texto)
        if m:
            dados['empenho_numero'] = m.group(1)
        m = re.search(r'PEDIDO\s*([\d/]+)', texto)
        if m:
            dados['pedido_numero'] = m.group(1)
        m = re.search(r'Entregue em\s*(\d{2}/\d{2}/\d{4})', texto)
        if m:
            dados['data_entrega_venda'] = m.group(1)

        dados['cliente_contrato_nome'] = dados['outra_parte_nome']

    return dados


def parse_danfe_pdf(caminho_pdf: str) -> list:
    """Um PDF pode ter várias páginas = várias notas. Devolve uma lista
    de dicionários, um por nota."""
    resultado = []
    with pdfplumber.open(caminho_pdf) as pdf:
        for page in pdf.pages:
            texto = page.extract_text() or ''
            if 'DANFE' not in texto and 'NF-e' not in texto:
                continue
            resultado.append(parse_danfe_pagina(texto))
    return resultado


# ---------------------------------------------------------------------
# RECONCILIAÇÃO: casa itens de COMPRA (entrada do cooperado) com itens
# de VENDA (saída para a prefeitura/cliente)
# ---------------------------------------------------------------------

APELIDOS_PRODUTO = {
    'ABOBORA JAPONESA': 'ABOBORA',
    'COUVE MANTEIGA': 'COUVE',
}


def normalizar_produto(nome: str) -> str:
    nome = (nome or '').upper().strip()
    return APELIDOS_PRODUTO.get(nome, nome)


def reconciliar_notas(notas: list) -> list:
    """Recebe a lista de notas (saída de parse_danfe_pdf) e devolve uma
    lista achatada por item/produto, já com status:
      - 'repassado'              -> comprado do cooperado E vendido ao cliente
      - 'aguardando_repasse'     -> comprado do cooperado, ainda NÃO vendido
      - 'venda_sem_compra_correspondente' -> saída sem entrada equivalente (alerta)
    """
    itens_compra, itens_venda = [], []

    for nota in notas:
        for prod in nota['produtos']:
            base = {
                'nota_numero': nota['numero_nota'],
                'chave_acesso': nota['chave_acesso'],
                'produto': prod['descricao'],
                'produto_chave': normalizar_produto(prod['descricao']),
                'valor': prod['valor_total'],
                'peso': prod['quantidade'],
            }
            if nota['natureza_operacao'] == 'COMPRA':
                base.update({
                    'data': nota['data_entrega_compra'] or nota['data_emissao'],
                    'cooperado_nome': nota['outra_parte_nome'],
                    'cooperado_numero': nota['cooperado_numero'],
                    'cooperado_cpf': nota['cooperado_cpf'],
                    'cliente_contrato_nome': nota['cliente_contrato_nome'],
                    'funrural': nota['funrural_valor'],
                    'valor_liquido': nota['valor_liquido_informado'],
                })
                itens_compra.append(base)
            elif nota['natureza_operacao'] == 'VENDA':
                base.update({
                    'data': nota['data_entrega_venda'] or nota['data_emissao'],
                    'cliente_nome': nota['outra_parte_nome'],
                    'contrato_numero': nota['contrato_numero'],
                })
                itens_venda.append(base)

    usados = set()
    resultado = []

    for c in itens_compra:
        match_idx = None
        for i, v in enumerate(itens_venda):
            if i in usados:
                continue
            mesmo_cliente = (v['cliente_nome'] or '').upper() == (c['cliente_contrato_nome'] or '').upper()
            mesmo_produto = v['produto_chave'] == c['produto_chave']
            mesmo_valor = abs(v['valor'] - c['valor']) < 0.02
            if mesmo_cliente and mesmo_produto and mesmo_valor:
                match_idx = i
                break

        item = dict(c)
        if match_idx is not None:
            usados.add(match_idx)
            item['status'] = 'repassado'
            item['nota_venda_vinculada'] = itens_venda[match_idx]['nota_numero']
        else:
            item['status'] = 'aguardando_repasse'
            item['nota_venda_vinculada'] = None
        resultado.append(item)

    for i, v in enumerate(itens_venda):
        if i not in usados:
            item = dict(v)
            item['status'] = 'venda_sem_compra_correspondente'
            resultado.append(item)

    return resultado


if __name__ == '__main__':
    import sys, json

    caminho = sys.argv[1] if len(sys.argv) > 1 else '/mnt/user-data/uploads/COOAIPRO03-08-2026STI.pdf'
    notas = parse_danfe_pdf(caminho)

    print(f'\n=== {len(notas)} notas encontradas ===')
    for n in notas:
        print(f"  Nota {n['numero_nota']} | {n['natureza_operacao']:6} | "
              f"{n['outra_parte_nome']:35} | R$ {n['valor_total_nota']}")

    print('\n=== RECONCILIAÇÃO (por item de produto) ===')
    itens = reconciliar_notas(notas)
    for it in itens:
        if it['status'] == 'repassado':
            print(f"   {it['produto']:20} R$ {it['valor']:>8.2f}  "
                  f"cooperado={it['cooperado_nome']:25} -> venda nota {it['nota_venda_vinculada']}")
        elif it['status'] == 'aguardando_repasse':
            print(f"   {it['produto']:20} R$ {it['valor']:>8.2f}  "
                  f"cooperado={it['cooperado_nome']:25} -> AGUARDANDO REPASSE (cliente alvo: {it['cliente_contrato_nome']})")
        else:
            print(f"    {it['produto']:20} R$ {it['valor']:>8.2f}  "
                  f"venda para {it['cliente_nome']} SEM COMPRA CORRESPONDENTE")
    Bom, dá pra estruturar isso de um jeito bem mais robusto. Vou montar um parser standalone, testar contra o PDF real que você mandou (as 6 DANFEs) e confirmar que ele separa COMPRA de VENDA corretamente e já faz o casamento cooperado→cliente→venda.Funcionando perfeitamente contra as 6 DANFEs reais do seu PDF: as 4 compras que viraram venda casaram certinho (nota a nota), e o Coentro — que foi comprado do Paulo Roberto mas nunca saiu numa venda pra prefeitura — cai exatamente como `aguardando_repasse`, com o cliente-alvo (`PREFEITURA MUNICIPAL DE SANTA ISABEL`) já identificado a partir do texto complementar da própria nota de compra.**Como o parser resolve os dois problemas:**

1. **`parse_danfe_pdf(caminho)`** — lê o PDF com `pdfplumber` (uma nota por página) e devolve uma lista de dicts com: `numero_nota`, `natureza_operacao` (COMPRA/VENDA), `chave_acesso`, `outra_parte_nome`/`cnpj_cpf`, lista de `produtos`, e — crucial — os campos extraídos do texto livre "Informações Complementares":
   - Em **COMPRA**: `cliente_contrato_nome` (a prefeitura-alvo, extraída de *"entrega de produtos para X pelo cooperado"*), `cooperado_numero`, `cooperado_cpf`, `funrural_valor`, `valor_liquido_informado`.
   - Em **VENDA**: `contrato_numero`, `empenho_numero`, `pedido_numero`, `data_entrega_venda`.

2. **`reconciliar_notas(notas)`** — pega essa lista e casa cada item de COMPRA com um item de VENDA por **cliente + produto (normalizado) + valor**, marcando:
   - `repassado` → tem compra e venda casadas (é o que deveria contar na cota do cooperado)
   - `aguardando_repasse` → comprado do cooperado mas ainda sem venda equivalente (seu caso do Coentro)
   - `venda_sem_compra_correspondente` → alerta pro lado oposto (venda sem origem identificada)

Pontos para você decidir antes de plugar isso no `app.py`:

1. **Matching por valor exato** funciona bem no seu exemplo, mas se dois cooperados entregarem o mesmo produto pelo mesmo valor no mesmo dia pra prefeituras diferentes, ainda tá seguro (porque também exijo `cliente_contrato_nome` == `cliente_nome`). Risco real é se o **mesmo cliente** comprar o mesmo produto de dois cooperados por valores coincidentes — aí eu preciso de um critério de desempate (ex: ordem de emissão, ou tolerância de peso).
2. O regex de produto espera formato `CÓDIGO DESCRIÇÃO NCM(8) CST(3) CFOP(4) UNID QTD VLR.UNIT VLR.TOTAL` — se o extrator que vocês já usam no `app.py` (`pdfplumber` ou outro) devolver texto num layout diferente, é só eu ajustar essa regex, mas o layout das 6 notas que você mandou bateu certinho.
3. Não toquei nas queries SQL (`vw_pagamentos_resumo` etc.) porque ainda não vi essas views — se você me mandar a definição delas eu já devolvo o SQL ajustado pra somar só `status = 'repassado'`.

Quer que eu já integre esse `parse_danfe_pdf`/`reconciliar_notas` na rota `/api/notas/importar_pdf` do seu `app.py`, substituindo a lógica atual?