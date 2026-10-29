"""Capítulo 9: conferir um token RS256 com a chave pública já em memória.

Assinatura, emissor, audiência e validade; média de 5 mil verificações. Não usa banco.
"""
import json
import time

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from prumo.identidade import Verificador

VEZES = 5_000


def main() -> None:
    privada = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(privada.public_key()))
    jwks = {"keys": [{**jwk, "kid": "k1", "use": "sig"}]}
    verificador = Verificador("https://id.prumo.local", "prumo-broker", lambda: jwks)
    token = jwt.encode({"iss": "https://id.prumo.local", "aud": "prumo-broker",
                        "exp": int(time.time()) + 300}, privada, algorithm="RS256",
                       headers={"kid": "k1"})
    verificador.verificar(token)                       # carrega a chave: fora da conta
    inicio = time.perf_counter()
    for _ in range(VEZES):
        verificador.verificar(token)
    media_us = (time.perf_counter() - inicio) / VEZES * 1e6
    print(f"{VEZES} verificações: {media_us:.0f} µs por token, na média")


if __name__ == "__main__":
    main()
