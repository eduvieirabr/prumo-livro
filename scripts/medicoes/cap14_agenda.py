"""Capítulo 14: o agendador.

1. A próxima vez de uma regra "5º dia útil, 8h": mediana de 2.000 cálculos.
2. Uma volta sem nada vencido: mediana de 20 voltas.
3. Uma volta com 500 agendamentos vencidos no mesmo minuto, agentes que respondem na hora:
   cada um vira ocorrência, é pego, roda e é registrado.
4. Quarenta agentes de 1 segundo cada, vencidos juntos: oito rodam por vez.
"""
import json
import time
from datetime import datetime, timedelta, timezone

import psycopg

from prumo.agentes import agenda
from scripts.medicoes._comum import cronometrar, dsn, mediana, ms

VENCE = datetime(2026, 11, 2, 11, 0, tzinfo=timezone.utc)      # segunda, 8h em Brasília
REGRA = {"tipo": "n_dia_util", "n": 5, "hora": "08:00"}
DIAS_UTEIS = json.dumps({"tipo": "dias_uteis", "hora": "08:00"})


def com_acesso(dono, agente) -> bool:
    return True


def preparar(conn, quantos: int) -> None:
    conn.execute("DROP TABLE IF EXISTS ocorrencia, aviso, agendamento CASCADE")
    conn.execute(open("prumo/agentes/schema.sql").read())
    with conn.cursor() as cur:
        cur.executemany("""INSERT INTO agendamento (dono, agente, regra, proxima)
                           VALUES (%s, 'conciliacao', %s, %s)""",
                        [(f"pessoa{i}", DIAS_UTEIS, VENCE) for i in range(quantos)])


def main() -> None:
    depois = datetime(2026, 11, 10, tzinfo=timezone.utc)
    tempos = cronometrar(lambda: agenda.proxima(REGRA, depois), 2_000)
    print(f"próxima vez de '5º dia útil, 8h': mediana {ms(mediana(tempos), 2)}")
    instantaneo = lambda agendamento, retomar_de: (True, None)
    with psycopg.connect(dsn(), autocommit=True) as conn:
        preparar(conn, 500)
        inicio = time.perf_counter()
        eventos = agenda.tick(conn, VENCE, com_acesso, instantaneo)
        cheia = time.perf_counter() - inicio
        ok = conn.execute("SELECT count(*) FROM ocorrencia WHERE status = 'ok'").fetchone()[0]
        vazias = []
        for i in range(20):
            agora = VENCE + timedelta(minutes=1 + i)
            inicio = time.perf_counter()
            agenda.tick(conn, agora, com_acesso, instantaneo)
            vazias.append(time.perf_counter() - inicio)
        print(f"volta sem nada vencido: mediana {ms(mediana(vazias), 2)} (20 voltas)")
        print(f"volta com 500 vencidos: {ms(cheia, 0)}, {len(eventos)} eventos, "
              f"{ok} ocorrências 'ok'")

        preparar(conn, 40)
        um_segundo = lambda agendamento, retomar_de: (time.sleep(1), (True, None))[1]
        inicio = time.perf_counter()
        agenda.tick(conn, VENCE, com_acesso, um_segundo)
        duracao = time.perf_counter() - inicio
        print(f"40 agentes de 1 s: {duracao:.1f} s".replace(".", ",")
              + f", {agenda.PARALELO} por vez")


if __name__ == "__main__":
    main()
