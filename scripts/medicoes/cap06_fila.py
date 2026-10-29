"""Capítulo 6: quanto custa tirar um pedido da fila, e o que acontece com oito consumidores.

10 mil pedidos pendentes. Um consumidor tira todos; depois a fila volta ao começo e
oito consumidores, cada um com sua conexão, tiram todos ao mesmo tempo. Nenhum pedido
pode sair duas vezes.
"""
import threading
import time

import psycopg

from prumo.broker.fila import pegar_proximo
from scripts.medicoes._broker import preparar
from scripts.medicoes._comum import dsn, ms

PEDIDOS = 10_000


def rodada(banco: str, consumidores: int) -> tuple[float, list]:
    with psycopg.connect(banco, autocommit=True) as conn:
        conn.execute("DELETE FROM pedido")
        conn.execute("""INSERT INTO pedido (integracao, tarefa, chave, conteudo)
                        SELECT 'parcelamentos', 'extrair_debitos', md5(g::text), '{}'
                          FROM generate_series(1, %s) g""", (PEDIDOS,))
        conn.execute("ANALYZE pedido")
    pegos, trava = [], threading.Lock()

    def consumir(i: int) -> None:
        with psycopg.connect(banco, autocommit=True) as conn:
            while pedido := pegar_proximo(conn, f"gpu-{i}"):
                with trava:
                    pegos.append(pedido["id"])

    fios = [threading.Thread(target=consumir, args=(i,)) for i in range(consumidores)]
    inicio = time.perf_counter()
    for f in fios:
        f.start()
    for f in fios:
        f.join()
    return time.perf_counter() - inicio, pegos


def main() -> None:
    banco = dsn()
    preparar(banco)
    for consumidores in (1, 8):
        duracao, pegos = rodada(banco, consumidores)
        repetidos = len(pegos) - len(set(pegos))
        print(f"{consumidores} consumidor(es): {len(pegos)} pedidos em "
              f"{duracao:.1f} s".replace(".", ",")
              + f", {ms(duracao / len(pegos))} por pedido, {repetidos} repetidos")


if __name__ == "__main__":
    main()
