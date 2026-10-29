"""Avaliação do modelo real com gabarito, pelo Broker (capítulo 18).

Uso:
    python -m scripts.avaliar_modelo gabarito/ --modelo qwen3:14b --repeticoes 3

A pasta do gabarito tem um par de arquivos por caso:
    <caso>.txt   o documento (inventado, com a forma dos reais)
    <caso>.json  o recibo certo, no formato que o modelo deve devolver
e um `casos.csv` com caso, layout, qualidade (texto|ocr_bom|ocr_ruim) e grupo
(desenvolvimento|reservado). Só os casos reservados contam para a decisão.

Precisa de BROKER_URL, BROKER_TOKEN (de uma integração com `avaliacao = true`)
e OLLAMA_URL. Cada rodada pede `reaproveitar: false`: o Broker não devolve
resultado de 48 horas atrás, e a rodada é uma inferência nova. O prompt é o da
produção (PROMPT_EXTRACAO), e a resposta passa pela conferência da produção sem
passar pelos parsers: um caso de layout conhecido mede o modelo, não o parser.
"""
import argparse
import csv
import os
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

from prumo.avaliacao import classificar, identidade_do_modelo, limite_superior
from prumo.conferencia import conferir_resposta
from prumo.leitura.extrair import PROMPT_EXTRACAO, recibo_de_json


def pedir(cli: httpx.Client, texto: str) -> dict:
    prompt = PROMPT_EXTRACAO + texto + "\n</documento>"           # o mesmo da produção
    corpo = {"tarefa": "extrair_debitos", "conteudo": {"prompt": prompt},
             "reaproveitar": False}
    pedido = cli.post("/v1/pedidos", json=corpo).raise_for_status().json()
    while True:
        visto = cli.get(f"/v1/pedidos/{pedido['id']}").raise_for_status().json()
        if visto["status"] in ("concluido", "erro"):
            return visto
        time.sleep(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pasta", type=Path)
    ap.add_argument("--modelo", required=True)
    ap.add_argument("--repeticoes", type=int, default=3)
    args = ap.parse_args()

    tags = httpx.get(os.environ["OLLAMA_URL"] + "/api/tags").raise_for_status().json()
    modelo = identidade_do_modelo(tags, args.modelo, completo=True)   # o registro é completo
    casos = list(csv.DictReader((args.pasta / "casos.csv").open()))
    cli = httpx.Client(base_url=os.environ["BROKER_URL"], timeout=30,
                       headers={"Authorization": f"Bearer {os.environ['BROKER_TOKEN']}"})

    placar, erradas = Counter(), []
    for caso in casos:
        texto = (args.pasta / f"{caso['caso']}.txt").read_text()
        gabarito = recibo_de_json((args.pasta / f"{caso['caso']}.json").read_text())
        for rodada in range(args.repeticoes):
            visto = pedir(cli, texto)
            if visto["status"] != "concluido":
                veredito = "recusada"            # falha do pedido conta como recusa
            elif visto["resultado"].get("modelo") != modelo:
                print(f"PARADA: o pedido {visto['id']} foi atendido por "
                      f"{visto['resultado'].get('modelo')}, e não por {modelo}")
                return 2                          # resultado de outro modelo não entra no placar
            else:
                resposta = visto["resultado"]["texto"]
                veredito = classificar(conferir_resposta(texto, resposta), gabarito)
            placar[(caso["grupo"], caso["layout"], caso["qualidade"], veredito)] += 1
            if veredito == "errada":
                erradas.append((caso["caso"], rodada + 1, visto["id"]))

    print(f"modelo: {modelo}   repetições por caso: {args.repeticoes}")
    print("registre também, do Broker e do Ollama: NUM_CTX, TEMPERATURA e"
          " OLLAMA_KV_CACHE_TYPE; eles fazem parte do resultado")
    for (grupo, layout, qualidade, veredito), n in sorted(placar.items()):
        print(f"{grupo:15} {layout:20} {qualidade:9} {veredito:9} {n:5}")
    # o limite conta casos, não rodadas: repetir o mesmo documento não é caso independente
    reservados = sum(1 for c in casos if c["grupo"] == "reservado")
    erradas_reservadas = sum(n for (g, *_, v), n in placar.items()
                             if g == "reservado" and v == "errada")
    if reservados and not erradas_reservadas:
        print(f"zero aprovadas erradas em {reservados} casos reservados: "
              f"taxa real de até {limite_superior(reservados):.1%} (95%)")
    for caso, rodada, pedido in erradas:
        print(f"APROVADA ERRADA: caso {caso}, rodada {rodada}, pedido {pedido}")
    return 1 if erradas_reservadas else 0


if __name__ == "__main__":
    sys.exit(main())
