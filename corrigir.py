from processador_coaipro import _conectar

conn = _conectar()
cur = conn.cursor()

# 1. Remove as regras antigas
cur.execute("ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS usuarios_check;")
cur.execute("ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS usuarios_tipo_check;")

# 2. Converte usuários antigos
cur.execute("UPDATE usuarios SET tipo='admin' WHERE tipo='cooperativa';")

# 3. Recria as regras no padrão novo
cur.execute("""
    ALTER TABLE usuarios
    ADD CONSTRAINT usuarios_tipo_check CHECK (tipo IN ('cooperado', 'admin'));
""")
cur.execute("""
    ALTER TABLE usuarios
    ADD CONSTRAINT usuarios_check
    CHECK ((tipo = 'cooperado' AND fornecedor_id IS NOT NULL) OR (tipo = 'admin'));
""")
conn.commit()

cur.execute("SELECT id, email, tipo, aprovado, cooperativa_id FROM usuarios ORDER BY id;")
for linha in cur.fetchall():
    print(linha)
conn.close()
print("Pronto.")