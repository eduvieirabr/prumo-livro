"""Cria todos os esquemas do livro num PostgreSQL 16 com pgvector (apêndice A).

    python scripts/criar_banco.py postgresql://postgres:senha@localhost:5432/prumo

Com MEM_APP_SENHA no ambiente, o papel mem_app da memória (cap. 10) ganha essa senha:
a aplicação da memória conecta como ele, sem os poderes do dono do banco.
"""
import os
import sys
from pathlib import Path

import psycopg

from prumo.agentes import chaves

RAIZ = Path(__file__).resolve().parent.parent / "prumo"
ORDEM = ["broker/schema.sql", "memoria/schema.sql", "agentes/schema.sql",
         "central/schema.sql", "parcelamentos/schema.sql"]

if __name__ == "__main__":
    with psycopg.connect(sys.argv[1], autocommit=True) as conn:
        for arquivo in ORDEM:
            conn.execute((RAIZ / arquivo).read_text())
            print("ok", arquivo)
        conn.execute(chaves.SCHEMA)
        print("ok agentes/chaves.py")
        if os.getenv("MEM_APP_SENHA"):
            senha = conn.execute("SELECT quote_literal(%s)", (os.environ["MEM_APP_SENHA"],))
            conn.execute(f"ALTER ROLE mem_app PASSWORD {senha.fetchone()[0]}")
            print("ok senha do mem_app")
