import os
import psycopg2

# Configuração da conexão com o PostgreSQL
DB_CONFIG = {
    'host': os.environ.get('DB_HOST', 'localhost'),
    'port': os.environ.get('DB_PORT', '5432'),
    'dbname': os.environ.get('DB_NAME', 'coaipro'),
    'user': os.environ.get('DB_USER', 'postgres'),
    'password': os.environ.get('DB_PASSWORD', ''),
}

def get_db():
    """Retorna uma conexão com o banco de dados PostgreSQL."""
    conn = psycopg2.connect(**DB_CONFIG)
    return conn