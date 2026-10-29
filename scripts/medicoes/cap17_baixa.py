"""Capítulo 17: a baixa das parcelas pelos eventos da central.

Mil títulos pagos, um para cada parcela, consumidos em lotes de 100, com a baixa,
o registro de aplicado, o evento de saída e a confirmação do cursor. Depois, o outbox
com as mil baixas, publicadas na central uma a uma. Cliente da central chamando as
funções sem HTTP; mediana de três rodadas.
"""
import time

import psycopg

from prumo.central import central as c
from prumo.parcelamentos import baixa
from scripts.medicoes._central import ClienteLocal, pago, preparar
from scripts.medicoes._comum import dsn, mediana

PARCELAS, RODADAS = 1_000, 3


def rodada(conn) -> tuple[float, float, int, int]:
    preparar(conn)
    with conn.cursor() as cur:
        cur.executemany("""INSERT INTO parc.parcela (parcelamento, numero, vencimento,
                                                     valor_calculado)
                           VALUES ('PONTAL-2025-09', %s, '2026-02-27', 3412.87)""",
                        [(n,) for n in range(1, PARCELAS + 1)])
    for k in range(0, PARCELAS, 500):
        c.publicar(conn, "vertice", baixa.CANAL_PAGOS,
                   [pago(n, titulo=str(n)) for n in range(k + 1, k + 501)])
    cliente = ClienteLocal(conn, "prumo-parcelamentos")
    inicio, consumidos = time.perf_counter(), 0
    while relatorio := baixa.consumir(conn, cliente, maximo=100):
        consumidos += len(relatorio)
    consumo = time.perf_counter() - inicio
    inicio, enviados = time.perf_counter(), 0
    while n := baixa.enviar_saida(conn, cliente, 100):
        enviados += n
    return consumo, time.perf_counter() - inicio, consumidos, enviados


def main() -> None:
    with psycopg.connect(dsn(), autocommit=True) as conn:
        rodadas = [rodada(conn) for _ in range(RODADAS)]
    consumo = mediana([r[0] for r in rodadas])
    outbox = mediana([r[1] for r in rodadas])
    print(f"mediana de {RODADAS} rodadas; {rodadas[0][2]} consumidos, "
          f"{rodadas[0][3]} enviados")
    print(f"consumir {PARCELAS} pagamentos em lotes de 100: {consumo:.2f} s".replace(".", ",")
          + f" ({consumo / PARCELAS * 1000:.2f} ms por evento)".replace(".", ","))
    print(f"outbox com {PARCELAS} baixas, uma a uma: {outbox:.2f} s".replace(".", ","))


if __name__ == "__main__":
    main()
