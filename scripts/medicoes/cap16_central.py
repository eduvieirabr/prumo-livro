"""Capítulo 16: publicar na central.

Funções da central chamadas sem HTTP, cada número a mediana de três rodadas:
1. um evento de título pago, com conferência do esquema (mediana de 300 publicações);
2. um lote de 500 eventos;
3. um publicador sozinho, 250 eventos um a um, em eventos por segundo;
4. quatro publicadores no mesmo canal ao mesmo tempo, 250 cada.
E pela API HTTP, com token do Prumo ID conferido e conexão nova ao banco por pedido:
mediana e percentil 95 de 200 publicações.
"""
import json
import os
import threading
import time

import jwt
import psycopg
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from prumo.central import central as c
from prumo.identidade import Verificador
from prumo.parcelamentos.baixa import CANAL_PAGOS
from scripts.medicoes._central import pago, preparar
from scripts.medicoes._comum import cronometrar, dsn, mediana, ms, p95

RODADAS = 3
EMISSOR = "https://id.prumo.local/realms/prumo"


def publicadores(banco: str, quantos: int, rodada: int) -> float:
    def publicar(k: int) -> None:
        with psycopg.connect(banco, autocommit=True) as conn:
            for i in range(250):
                c.publicar(conn, "vertice", CANAL_PAGOS, [pago(titulo=f"r{rodada}-{k}-{i}")])
    fios = [threading.Thread(target=publicar, args=(k,)) for k in range(quantos)]
    inicio = time.perf_counter()
    for f in fios:
        f.start()
    for f in fios:
        f.join()
    return quantos * 250 / (time.perf_counter() - inicio)


def pela_api(banco: str) -> list[float]:
    from prumo.central import api
    privada = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(privada.public_key()))
    jwks = {"keys": [{**jwk, "kid": "k1", "use": "sig", "alg": "RS256"}]}
    os.environ["CENTRAL_DSN"] = banco
    api.VERIFICADOR = Verificador(EMISSOR, api.AUDIENCIA, lambda: jwks)
    agora = int(time.time())
    token = jwt.encode({"iss": EMISSOR, "aud": api.AUDIENCIA, "iat": agora,
                        "exp": agora + 3600, "azp": "vertice-erp", "client_id": "vertice-erp"},
                       privada, algorithm="RS256", headers={"kid": "k1"})
    cli = TestClient(api.app)
    cabecalho = {"Authorization": f"Bearer {token}"}
    numero = iter(range(10**9))

    def uma():
        evento = pago(titulo=f"http-{next(numero)}")
        r = cli.post(f"/v1/canais/{CANAL_PAGOS}/eventos", json={"eventos": [evento]},
                     headers=cabecalho)
        assert r.status_code == 201, r.text
    uma()                                             # aquece: chave em memória
    return cronometrar(uma, 200)


def main() -> None:
    banco = dsn()
    um, lote, sozinho, quatro = [], [], [], []
    with psycopg.connect(banco, autocommit=True) as conn:
        for r in range(RODADAS):
            preparar(conn)
            contador = iter(range(10**9))
            um.append(mediana(cronometrar(
                lambda: c.publicar(conn, "vertice", CANAL_PAGOS,
                                   [pago(titulo=f"u{next(contador)}")]), 300)))
            eventos = [pago(titulo=f"l{r}-{i}") for i in range(500)]
            inicio = time.perf_counter()
            c.publicar(conn, "vertice", CANAL_PAGOS, eventos)
            lote.append(time.perf_counter() - inicio)
            preparar(conn)
            sozinho.append(publicadores(banco, 1, r))
            preparar(conn)
            quatro.append(publicadores(banco, 4, r))
        preparar(conn)
    http = pela_api(banco)
    print(f"mediana de {RODADAS} rodadas")
    print(f"publicar um evento:          {ms(mediana(um))}")
    print(f"lote de 500 eventos:         {ms(mediana(lote), 0)}")
    print(f"um publicador, um a um:      {mediana(sozinho):.0f} eventos/s")
    print(f"quatro publicadores juntos:  {mediana(quatro):.0f} eventos/s no total")
    print(f"pela API HTTP (200):         mediana {ms(mediana(http))}, p95 {ms(p95(http))}")


if __name__ == "__main__":
    main()
