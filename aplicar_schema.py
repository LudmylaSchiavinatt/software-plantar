"""Aplica o schema_postgres.sql no banco atual (tudo ou nada: se der erro, nada e alterado)."""
from pathlib import Path
from processador_coaipro import _conectar

arquivo = Path(__file__).resolve().parent / "schema_postgres.sql"
sql = arquivo.read_text(encoding="utf-8")

conn = _conectar()
try:
    cur = conn.cursor()
    cur.execute(sql)
    conn.commit()
    print("Schema aplicado com sucesso.")
except Exception as e:
    conn.rollback()
    print("ERRO (nada foi alterado no banco):")
    print(e)
finally:
    conn.close()