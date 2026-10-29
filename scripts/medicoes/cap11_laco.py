"""Capítulo 11: quanto custa gravar o estado e o registro de cada passo do agente.

50 execuções de um agente de 10 passos com executores vazios: o tempo é todo do banco.
"""
import time

import psycopg

from prumo.agentes import laco
from scripts.medicoes._comum import dsn, mediana, ms

EXECUCOES, PASSOS = 50, 10


def main() -> None:
    banco = dsn()
    agente = laco.Agente("medir", 1, tuple(laco.Passo(f"p{i}", "nada") for i in range(PASSOS)))
    executores = {"nada": lambda passo, contexto: {"ok": 1}}
    with psycopg.connect(banco, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS passo_execucao, execucao, skill_versao CASCADE")
        conn.execute(open("prumo/agentes/schema.sql").read())
        por_passo = []
        for _ in range(EXECUCOES):
            execucao = laco.iniciar(conn, agente, "marta", {})
            inicio = time.perf_counter()
            status = laco.rodar(conn, execucao, agente, executores, visiveis=set)
            por_passo.append((time.perf_counter() - inicio) / PASSOS)
            assert status == "concluida", status
    print(f"{EXECUCOES} execuções de {PASSOS} passos: mediana {ms(mediana(por_passo), 2)} "
          "por passo")


if __name__ == "__main__":
    main()
