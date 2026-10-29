"""O que os scripts de medição têm em comum: o banco, a mediana e o relógio."""
import os
import statistics
import sys
import time

AVISO = """Defina PRUMO_MEDIR_PG com um banco PostgreSQL 16 só para medições, por exemplo:
    export PRUMO_MEDIR_PG=postgresql://postgres@127.0.0.1/prumo_medir
Os scripts apagam e recriam as tabelas que usam. Não aponte para um banco com dados."""


def dsn() -> str:
    valor = os.getenv("PRUMO_MEDIR_PG")
    if not valor:
        sys.exit(AVISO)
    return valor


def ms(segundos: float, casas: int = 1) -> str:
    """Milissegundos no formato do livro: 3,8 ms."""
    return f"{segundos * 1000:.{casas}f}".replace(".", ",") + " ms"


def mediana(valores) -> float:
    return statistics.median(valores)


def p95(valores) -> float:
    ordenados = sorted(valores)
    return ordenados[max(0, int(len(ordenados) * 0.95) - 1)]


def cronometrar(funcao, vezes: int) -> list[float]:
    """Roda `funcao()` `vezes` vezes e devolve a duração de cada uma, em segundos."""
    tempos = []
    for _ in range(vezes):
        inicio = time.perf_counter()
        funcao()
        tempos.append(time.perf_counter() - inicio)
    return tempos
