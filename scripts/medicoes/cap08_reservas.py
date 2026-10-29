"""Capítulo 8: reservas concorrentes contra o teto da LLM paga.

Teto de R$ 100,00 no mês; oito conexões tentam, ao mesmo tempo, 400 reservas de R$ 0,50.
Exatamente 200 podem passar.
"""
import threading
import time
import uuid
from datetime import date
from decimal import Decimal

import psycopg

from prumo.broker import pagos
from scripts.medicoes._broker import preparar
from scripts.medicoes._comum import dsn, mediana, ms

TENTATIVAS, CONEXOES = 400, 8


def main() -> None:
    banco = dsn()
    preparar(banco)
    with psycopg.connect(banco, autocommit=True) as conn:
        conn.execute("""INSERT INTO orcamento (integracao, mes, teto)
                        VALUES ('parcelamentos', '2026-06-01', 100)""")
        pedidos = [conn.execute("""INSERT INTO pedido (integracao, tarefa, chave)
                                   VALUES ('parcelamentos', 'assistente', %s) RETURNING id""",
                                (uuid.uuid4().hex,)).fetchone()[0] for _ in range(TENTATIVAS)]
    aceitas, tempos, trava = [], [], threading.Lock()

    def tentar(lote) -> None:
        with psycopg.connect(banco, autocommit=True) as conn:
            for pedido in lote:
                inicio = time.perf_counter()
                ok = pagos.reservar(conn, pedido, "parcelamentos", "modelo-pago",
                                    Decimal("0.50"), date(2026, 6, 15))
                with trava:
                    tempos.append(time.perf_counter() - inicio)
                    if ok:
                        aceitas.append(pedido)

    fios = [threading.Thread(target=tentar, args=(pedidos[i::CONEXOES],))
            for i in range(CONEXOES)]
    for f in fios:
        f.start()
    for f in fios:
        f.join()
    print(f"{CONEXOES} conexões, {TENTATIVAS} reservas de R$ 0,50 contra teto de R$ 100,00: "
          f"{len(aceitas)} aceitas; mediana {ms(mediana(tempos))} por tentativa")


if __name__ == "__main__":
    main()
