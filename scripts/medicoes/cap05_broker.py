"""Capítulo 5: quanto o Broker custa por pedido, sem contar a inferência.

20 mil pedidos antigos concluídos na tabela; 300 pedidos novos pela API (cada um com
uma conexão nova ao banco) e depois os mesmos 300 de novo, que voltam reaproveitados.
"""
import os

import psycopg
from fastapi.testclient import TestClient

from scripts.medicoes._broker import TOKEN, preparar
from scripts.medicoes._comum import cronometrar, dsn, mediana, ms

ANTIGOS, PEDIDOS = 20_000, 300


def main() -> None:
    banco = dsn()
    os.environ["BROKER_DSN"] = banco
    from prumo.broker import api
    preparar(banco)
    with psycopg.connect(banco, autocommit=True) as conn:
        conn.execute("""INSERT INTO pedido (integracao, tarefa, chave, status, resultado,
                                            concluido_em)
                        SELECT 'parcelamentos', 'extrair_debitos', md5(g::text), 'concluido',
                               '{}', now()
                          FROM generate_series(1, %s) g""", (ANTIGOS,))
        conn.execute("ANALYZE pedido")
    cli = TestClient(api.app)
    cabecalho = {"Authorization": f"Bearer {TOKEN}"}
    contador = iter(range(10**9))

    def pedir(status: str):
        def uma_vez():
            n = next(contador) % PEDIDOS
            corpo = {"tarefa": "extrair_debitos", "conteudo": {"texto": f"documento {n}"}}
            r = cli.post("/v1/pedidos", json=corpo, headers=cabecalho)
            assert r.json()["status"] == status, r.text
        return uma_vez

    novos = cronometrar(pedir("pendente"), PEDIDOS)
    with psycopg.connect(banco, autocommit=True) as conn:   # o consumidor terminou todos
        conn.execute("""UPDATE pedido SET status = 'concluido', resultado = '{}',
                               concluido_em = now() WHERE status = 'pendente'""")
    reaproveitados = cronometrar(pedir("reaproveitado"), PEDIDOS)
    print(f"{ANTIGOS} pedidos antigos; {PEDIDOS} pedidos de cada tipo, pela API")
    print(f"criar um pedido novo:          mediana {ms(mediana(novos))}")
    print(f"devolver um reaproveitado:     mediana {ms(mediana(reaproveitados))}")


if __name__ == "__main__":
    main()
