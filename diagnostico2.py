import os, psycopg2, psycopg2.extras
from dotenv import load_dotenv
load_dotenv()

conn = psycopg2.connect(
    host=os.environ.get('DB_HOST', 'localhost'),
    port=os.environ.get('DB_PORT', '5432'),
    dbname=os.environ.get('DB_NAME', 'coaipro'),
    user=os.environ.get('DB_USER', 'postgres'),
    password=os.environ.get('DB_PASSWORD', ''),
)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

def q(titulo, sql):
    print('\n==', titulo)
    try:
        cur.execute(sql)
        for r in cur.fetchall():
            print(dict(r))
    except Exception as e:
        print('ERRO:', e)
        conn.rollback()

q('colunas contrato_cooperado',
  "select column_name, is_nullable from information_schema.columns "
  "where table_name = 'contrato_cooperado' order by ordinal_position")
q('restricoes contrato_cooperado',
  "select conname, pg_get_constraintdef(oid) as definicao "
  "from pg_constraint where conrelid = 'contrato_cooperado'::regclass")