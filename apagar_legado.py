"""
APAGA os dados antigos que nao pertencem a nenhuma cooperativa (cooperativa_id vazio).
Nao mexe em cooperativas nem em nada que ja esteja vinculado a uma cooperativa.
Tudo ou nada: se der erro, nada e apagado.
"""
from processador_coaipro import _conectar

# (nome, condicao dos registros a apagar). A ordem respeita as chaves estrangeiras.
# Tabelas "filhas" sao identificadas pelo pai sem cooperativa.
ETAPAS = [
    ("nota_itens",
     "nota_id IN (SELECT id FROM notas_fiscais WHERE cooperativa_id IS NULL)"),
    ("contrato_cooperado",
     "cliente_id IN (SELECT id FROM clientes WHERE cooperativa_id IS NULL)"),
    ("estoque_pendente",
     "fornecedor_id IN (SELECT id FROM fornecedores WHERE cooperativa_id IS NULL)"),
    ("pagamentos_fornecedores", "cooperativa_id IS NULL"),
    ("notas_fiscais", "cooperativa_id IS NULL"),
    ("usuarios", "cooperativa_id IS NULL"),
    ("produtos", "cooperativa_id IS NULL"),
    ("clientes", "cooperativa_id IS NULL"),
    ("fornecedores", "cooperativa_id IS NULL"),
]

conn = _conectar()
try:
    cur = conn.cursor()

    cur.execute("SELECT email, tipo FROM usuarios WHERE cooperativa_id IS NULL ORDER BY id")
    usuarios_velhos = cur.fetchall()

    plano = []
    for tabela, cond in ETAPAS:
        cur.execute("SELECT to_regclass(%s)", (tabela,))
        if cur.fetchone()[0] is None:
            continue
        cur.execute(f"SELECT COUNT(*) FROM {tabela} WHERE {cond}")
        plano.append((tabela, cond, cur.fetchone()[0]))

    print("Registros que serao APAGADOS:")
    for tabela, _, n in plano:
        print(f"  {tabela}: {n}")
    if usuarios_velhos:
        print("\nUsuarios antigos (sem cooperativa) que serao apagados:")
        for email, tipo in usuarios_velhos:
            print(f"  {email} ({tipo})")

    if sum(n for _, _, n in plano) == 0:
        print("\nNao ha nada para apagar.")
    elif input("\nIsso nao pode ser desfeito. Digite APAGAR para confirmar: ").strip() == "APAGAR":
        for tabela, cond, n in plano:
            if n:
                cur.execute(f"DELETE FROM {tabela} WHERE {cond}")
        conn.commit()
        print("Pronto. Agora rode: python aplicar_schema.py")
    else:
        conn.rollback()
        print("Cancelado. Nada foi apagado.")
except Exception as e:
    conn.rollback()
    print("ERRO (nada foi apagado):")
    print(e)
finally:
    conn.close()