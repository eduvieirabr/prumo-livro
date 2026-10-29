"""Capítulo 7: do evento gravado no banco ao cliente SSE, com o Broker num uvicorn de verdade.

40 eventos gravados a cada 0,37 s num pedido; um cliente HTTP escuta /eventos e anota a hora
de chegada de cada um. O canal SSE consulta o banco a cada 0,3 s.
"""
import json
import os
import socket
import threading
import time

import httpx
import psycopg
import uvicorn

from prumo.broker.eventos import publicar
from scripts.medicoes._broker import TOKEN, preparar
from scripts.medicoes._comum import dsn, mediana, ms

EVENTOS = 40


def main() -> None:
    banco = dsn()
    os.environ["BROKER_DSN"] = banco
    from prumo.broker import api
    preparar(banco)
    with psycopg.connect(banco, autocommit=True) as conn:
        pedido = conn.execute("""INSERT INTO pedido (integracao, tarefa, chave, conteudo)
                                 VALUES ('parcelamentos', 'extrair_debitos', 'k', '{}')
                                 RETURNING id""").fetchone()[0]
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        porta = s.getsockname()[1]
    servidor = uvicorn.Server(uvicorn.Config(api.app, port=porta, log_level="warning"))
    threading.Thread(target=servidor.run, daemon=True).start()
    while not servidor.started:
        time.sleep(0.05)

    chegou = {}

    def ouvir() -> None:
        url = f"http://127.0.0.1:{porta}/v1/pedidos/{pedido}/eventos"
        with httpx.stream("GET", url, headers={"Authorization": f"Bearer {TOKEN}"},
                          timeout=60) as resposta:
            for linha in resposta.iter_lines():
                if linha.startswith("data:"):
                    dados = json.loads(linha[5:])
                    chegou[dados["n"]] = time.monotonic()
                    if dados["fim"]:
                        return

    ouvinte = threading.Thread(target=ouvir)
    ouvinte.start()
    time.sleep(1)
    gravou = {}
    with psycopg.connect(banco, autocommit=True) as conn:
        for n in range(EVENTOS):
            fim = n == EVENTOS - 1
            gravou[n] = time.monotonic()
            publicar(conn, pedido, "concluido" if fim else "parcial", {"n": n, "fim": fim})
            time.sleep(0.37)
    ouvinte.join()
    servidor.should_exit = True
    atrasos = [chegou[n] - gravou[n] for n in gravou]
    print(f"{EVENTOS} eventos: mediana {ms(mediana(atrasos), 0)}, "
          f"pior caso {ms(max(atrasos), 0)}")


if __name__ == "__main__":
    main()
