"""Prepara as tabelas do Broker (caps. 5 a 8) no banco de medição."""
import psycopg

from prumo.broker.api import hash_token
from prumo.broker.fila import criar_schema

TOKEN = "prm_medir"


def preparar(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS aceite, evento, uso_pago, orcamento, pedido, "
                     "integracao CASCADE")
        criar_schema(conn)
        conn.execute("""INSERT INTO integracao (id, token_hash, tarefas)
                        VALUES ('parcelamentos', %s, '{extrair_debitos,assistente}')""",
                     (hash_token(TOKEN),))
