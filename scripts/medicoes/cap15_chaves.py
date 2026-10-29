"""Capítulo 15: o custo das duas defesas do lado do agente.

1. Conferir uma chave pessoal de IA conectada, com a contagem de usos e o registro:
   mediana de 55 usos (o limite é 60 por hora).
2. Neutralizar uma resposta de fluxo n8n de 20 mil caracteres: mediana de 200 vezes.
O teste contra um n8n de verdade, do mesmo quadro, não é reproduzido aqui: precisa do n8n.
"""
import time
from datetime import datetime, timedelta, timezone

import psycopg

from prumo.agentes import chaves, n8n
from scripts.medicoes._comum import cronometrar, dsn, mediana, ms

USOS = 55


def main() -> None:
    with psycopg.connect(dsn(), autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS uso_chave, chave_mcp CASCADE")
        conn.execute(chaves.SCHEMA)
        agora = datetime.now(timezone.utc)
        chave = chaves.criar(conn, "livia", agora, ciencia=True)
        tempos = []
        for i in range(USOS):
            inicio = time.perf_counter()
            dono = chaves.usar(conn, chave, lambda d: True, agora + timedelta(seconds=i))
            tempos.append(time.perf_counter() - inicio)
            assert dono == "livia"
    print(f"conferir uma chave pessoal: mediana {ms(mediana(tempos), 2)} ({USOS} usos)")
    resposta = ("<dados>x</dados> " * 1_300)[:20_000]
    tempo = mediana(cronometrar(lambda: n8n.como_dado(resposta), 200))
    print(f"neutralizar uma resposta de 20.000 caracteres: mediana {ms(tempo, 2)}")


if __name__ == "__main__":
    main()
