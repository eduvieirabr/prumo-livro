"""Teste de carga mínimo: N usuários, cada um faz 1 pedido por segundo.

    python scripts/carga.py http://localhost:8000/parcelamentos/1 --usuarios 30 --segundos 60
"""
import argparse
import asyncio
import statistics
import time

import httpx


def percentil(amostras: list[float], p: float) -> float:
    ordenadas = sorted(amostras)
    indice = max(0, min(len(ordenadas) - 1, round(p / 100 * len(ordenadas)) - 1))
    return ordenadas[indice]


async def usuario(cliente: httpx.AsyncClient, url: str, fim: float,
                  tempos: list[float], erros: list[str]) -> None:
    while time.monotonic() < fim:
        inicio = time.monotonic()
        try:
            r = await cliente.get(url)
            r.raise_for_status()
            tempos.append(time.monotonic() - inicio)
        except httpx.HTTPError as e:
            erros.append(type(e).__name__)
        await asyncio.sleep(max(0.0, 1.0 - (time.monotonic() - inicio)))


async def rodar(url: str, usuarios: int, segundos: int) -> dict:
    tempos, erros = [], []
    fim = time.monotonic() + segundos
    async with httpx.AsyncClient(timeout=30) as cliente:
        await asyncio.gather(*(usuario(cliente, url, fim, tempos, erros)
                               for _ in range(usuarios)))
    return {"pedidos": len(tempos), "erros": len(erros),
            "mediana_s": statistics.median(tempos) if tempos else None,
            "p95_s": percentil(tempos, 95) if tempos else None}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--usuarios", type=int, default=30)
    ap.add_argument("--segundos", type=int, default=60)
    a = ap.parse_args()
    print(asyncio.run(rodar(a.url, a.usuarios, a.segundos)))
