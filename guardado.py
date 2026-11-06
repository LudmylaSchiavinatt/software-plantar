import os
import tempfile
from flask import Flask, request, jsonify
from flask_login import login_required, current_user
from flask import Flask, request, jsonify, render_template
from database import get_db
from processador_danfe import parse_danfe_pdf, reconciliar_notas
# No app.py (ou database.py)
from processador_coaipro import is_primeira_execucao, carregar_planilhas_modelo
app = Flask(__name__)


def obter_ou_criar_cliente(cursor, nome: str, cpf_cnpj: str = None) -> int:
    nome_clean = nome.strip() if nome else "PREFEITURA MUNICIPAL"
    cursor.execute("SELECT id FROM clientes WHERE nome = %s OR (cpf_cnpj IS NOT NULL AND cpf_cnpj = %s)", (nome_clean, cpf_cnpj))
    res = cursor.fetchone()
    if res:
        return res[0]
    cursor.execute("INSERT INTO clientes (nome, cpf_cnpj, tipo) VALUES (%s, %s, 'prefeitura') RETURNING id", (nome_clean, cpf_cnpj))
    row = cursor.fetchone()
    return row[0] if row else cursor.lastrowid


def obter_ou_criar_fornecedor(cursor, nome: str, cpf_cnpj: str = None) -> int:
    nome_clean = nome.strip() if nome else "PRODUTOR RURAL"
    cursor.execute("SELECT id FROM fornecedores WHERE (cpf_cnpj IS NOT NULL AND cpf_cnpj = %s) OR nome = %s", (cpf_cnpj, nome_clean))
    res = cursor.fetchone()
    if res:
        return res[0]
    cursor.execute("INSERT INTO fornecedores (nome, cpf_cnpj) VALUES (%s, %s) RETURNING id", (nome_clean, cpf_cnpj))
    row = cursor.fetchone()
    return row[0] if row else cursor.lastrowid


def obter_ou_criar_produto(cursor, nome_produto: str) -> int:
    nome_clean = nome_produto.strip().upper()
    cursor.execute("SELECT id FROM produtos WHERE nome = %s", (nome_clean,))
    res = cursor.fetchone()
    if res:
        return res[0]
    cursor.execute("INSERT INTO produtos (nome, unidade_medida) VALUES (%s, 'kg') RETURNING id", (nome_clean,))
    row = cursor.fetchone()
    return row[0] if row else cursor.lastrowid


@app.route('/api/notas/importar_pdf', methods=['POST'])
@login_required
def importar_pdf():
    if getattr(current_user, 'tipo', None) != 'cooperativa':
        return jsonify({'erro': 'Acesso negado. Apenas cooperativas podem importar DANFEs.'}), 403

    # O front-end (tabelas.html / uploadPdfNotas) envia múltiplos arquivos
    # sob a chave 'arquivos_pdf' (input com "multiple"). Antes esta rota
    # só lia request.files['file'] (singular), então TODO upload de PDF
    # caía direto no erro abaixo.
    arquivos = request.files.getlist('arquivos_pdf')
    if not arquivos or all(f.filename == '' for f in arquivos):
        return jsonify({'erro': 'Nenhum arquivo enviado'}), 400

    total_repassados = 0
    total_pendentes = 0
    total_notas_lidas = 0
    dados_agregados = []
    erros_por_arquivo = []

    for file in arquivos:
        if file.filename == '':
            continue
        if not file.filename.lower().endswith('.pdf'):
            erros_por_arquivo.append(f'{file.filename}: formato inválido, esperado PDF.')
            continue

        fd, filepath = tempfile.mkstemp(suffix='.pdf')
        try:
            with os.fdopen(fd, 'wb') as f:
                file.save(f)

            notas_extraidas = parse_danfe_pdf(filepath)
            if not notas_extraidas:
                erros_por_arquivo.append(f'{file.filename}: não foi possível extrair dados válidos.')
                continue

            itens_reconciliados = reconciliar_notas(notas_extraidas)

            conn = get_db()
            cursor = conn.cursor()
            try:
                vendas_dict = {}
                for nota in notas_extraidas:
                    if nota['tipo'] == 'SAIDA':
                        cliente_id = obter_ou_criar_cliente(cursor, nota['contraparte_nome'], nota['destinatario_doc'])
                        total_venda = sum(it['valor_total'] for it in nota['itens'])

                        cursor.execute("""
                            INSERT INTO notas_fiscais (chave_acesso, numero_nota, data_emissao, cliente_id, valor_total, status)
                            VALUES (%s, %s, %s, %s, %s, 'emitida')
                            ON CONFLICT (numero_nota) DO UPDATE SET valor_total = EXCLUDED.valor_total
                            RETURNING id
                        """, (nota['chave_acesso'], nota['numero_nota'], nota['data_emissao'], cliente_id, total_venda))

                        res = cursor.fetchone()
                        nota_id = res[0] if res else cursor.lastrowid
                        vendas_dict[nota['numero_nota']] = nota_id

                        # Reimportar o mesmo PDF (numero_nota já existente) não pode
                        # duplicar os itens — o UPSERT acima só atualiza o total,
                        # então limpamos os itens antigos dessa nota antes de
                        # reinserir os atuais.
                        cursor.execute("DELETE FROM nota_itens WHERE nota_id = %s", (nota_id,))

                        for item in nota['itens']:
                            produto_id = obter_ou_criar_produto(cursor, item['produto'])
                            cursor.execute("""
                                INSERT INTO nota_itens (nota_id, produto_id, quantidade, valor_unitario, valor_total)
                                VALUES (%s, %s, %s, %s, %s)
                            """, (nota_id, produto_id, item['quantidade'], item['valor_unitario'], item['valor_total']))

                for item in itens_reconciliados:
                    if item['status'] == 'repassado':
                        fornecedor_id = obter_ou_criar_fornecedor(cursor, item['contraparte_nome'], item['cooperado_cpf'])
                        nota_venda_id = vendas_dict.get(item.get('nota_venda'))

                        cursor.execute("""
                            INSERT INTO pagamentos_fornecedores (
                                fornecedor_id, nota_id, data_emissao, prefeitura, produto_descricao,
                                numero_nota_fornecedor, valor_compra, funrural, valor_liquido, status
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pendente')
                        """, (
                            fornecedor_id, nota_venda_id, item['data_emissao'],
                            item['cliente_destino_nome'] or item.get('cliente_nome', ''),
                            item['produto'], item['numero_nota'], item['valor_total'],
                            item['funrural_valor'], item['valor_liquido_nota'] or (item['valor_total'] - item['funrural_valor'])
                        ))
                        total_repassados += 1

                    elif item['status'] == 'aguardando_repasse':
                        fornecedor_id = obter_ou_criar_fornecedor(cursor, item['contraparte_nome'], item['cooperado_cpf'])
                        cursor.execute("""
                            INSERT INTO estoque_pendente (
                                fornecedor_id, chave_acesso, numero_nota_fornecedor, data_emissao,
                                produto_descricao, quantidade, valor_unitario, valor_total, funrural, valor_liquido, status
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'aguardando_repasse')
                        """, (
                            fornecedor_id, item['chave_acesso'], item['numero_nota'], item['data_emissao'],
                            item['produto'], item['quantidade'], item['valor_unitario'], item['valor_total'],
                            item['funrural_valor'], item['valor_liquido_nota'] or (item['valor_total'] - item['funrural_valor'])
                        ))
                        total_pendentes += 1

                conn.commit()
                total_notas_lidas += len(notas_extraidas)
                dados_agregados.extend(itens_reconciliados)

            except Exception as db_err:
                conn.rollback()
                erros_por_arquivo.append(f'{file.filename}: erro ao persistir no banco ({db_err}).')
            finally:
                conn.close()

        except Exception as parse_err:
            erros_por_arquivo.append(f'{file.filename}: erro no processamento do DANFE ({parse_err}).')
        finally:
            if os.path.exists(filepath):
                os.remove(filepath)

    mensagem = f'Processamento concluído: {total_repassados} repasses vinculados a vendas e {total_pendentes} itens em estoque pendente.'
    if erros_por_arquivo:
        mensagem += f' {len(erros_por_arquivo)} arquivo(s) com problema.'

    return jsonify({
        'sucesso': total_notas_lidas > 0,
        'mensagem': mensagem,
        'total_notas_lidas': total_notas_lidas,
        'dados': dados_agregados,
        'erros_por_arquivo': erros_por_arquivo
    }), 200 if total_notas_lidas > 0 else 422
@app.route('/painel/tabelas')
@login_required
def painel_tabelas():
    conn = get_db()
    cursor = conn.cursor()
    
    # Exemplo simples de consulta aos dados para o template
    # Ajuste as consultas de acordo com a estrutura do seu BD
    cursor.execute("SELECT * FROM clientes")
    prefeituras = cursor.fetchall()
    
    cursor.execute("SELECT * FROM fornecedores")
    fornecedores = cursor.fetchall()
    
    cursor.execute("SELECT * FROM notas_fiscais")
    notas = cursor.fetchall()
    
    cursor.execute("SELECT * FROM pagamentos_fornecedores")
    pagamentos = cursor.fetchall()
    
    conn.close()
    
    return render_template(
        'tabelas_2.html', 
        prefeituras=prefeituras, 
        fornecedores=fornecedores, 
        notas=notas, 
        pagamentos=pagamentos
    )

# Rota opcional para redirecionar /painel/ para /painel/tabelas
@app.route('/painel/')
@login_required
def painel_index():
    return painel_tabelas()

if __name__ == '__main__':
    app.run(debug=True)