"""Capítulo 10: a busca híbrida da memória, com a segurança por linha ligada.

2 mil itens de quatro pessoas, dez trechos cada (20 mil trechos), índice HNSW nos vetores.
A aplicação conecta como mem_app, sem superpoderes. Mediana de 50 buscas da mesma pergunta,
em linguagem natural, como a Lívia (operadora) e como a Marta (supervisora). Para comparar,
a mesma busca com a primeira versão da parte de palavras, que exigia todas elas (E, não OU).
"""
import random

import psycopg
from psycopg.conninfo import make_conninfo

from prumo.memoria import memoria as mem
from scripts.medicoes._comum import cronometrar, dsn, mediana, ms

ITENS, TRECHOS, BUSCAS = 2_000, 10, 50
PERGUNTA = "juros da parcela do darf"
VOCABULARIO = ("parcelamento parcela darf selic consolidado débito irpj csll pis cofins "
               "extrato conciliação razão fornecedor cliente honorários prazo vencimento "
               "multa juros competência balancete folha férias rescisão").split()
PESSOAS = [("marta", "supervisor"), ("livia", "operador"), ("caio", "operador"),
           ("ana", "leitura")]
TODAS_AS_PALAVRAS = """
SELECT t.id FROM trecho t
 WHERE t.busca @@ plainto_tsquery('portuguese', mem_sem_acento(%(q)s))
 ORDER BY ts_rank_cd(t.busca, plainto_tsquery('portuguese', mem_sem_acento(%(q)s))) DESC
 LIMIT %(n)s
"""


def carregar(app) -> None:
    sorteio = random.Random(1)
    for i in range(ITENS):
        sub, papel = sorteio.choice(PESSOAS)
        with mem.como(app, sub, papel):
            escopo = "empresa" if papel != "leitura" and sorteio.random() < 0.5 else "pessoal"
            restrito = sorteio.random() < 0.1 and escopo == "empresa"   # sorteia sempre
            trechos = [" ".join(sorteio.choices(VOCABULARIO, k=40)) for _ in range(TRECHOS)]
            mem.guardar(app, f"item {i}", trechos, escopo=escopo, restrito=restrito)


def buscar_como(app, sub: str, papel: str) -> tuple[float, int]:
    achados = []

    def uma():
        with mem.como(app, sub, papel):
            achados[:] = mem.buscar(app, PERGUNTA)
    return mediana(cronometrar(uma, BUSCAS)), len(achados)


def main() -> None:
    banco = dsn()
    with psycopg.connect(banco, autocommit=True) as dono:
        dono.execute("DROP TABLE IF EXISTS trecho, item CASCADE")
        mem.criar_schema(dono)
        dono.execute("ALTER ROLE mem_app PASSWORD 'mem_app'")
    app_dsn = make_conninfo(banco, user="mem_app", password="mem_app")
    with psycopg.connect(app_dsn, autocommit=True) as app:
        carregar(app)
        with psycopg.connect(banco, autocommit=True) as dono:
            dono.execute("ANALYZE item")
            dono.execute("ANALYZE trecho")
        with mem.como(app, "livia", "operador"):
            vistos = app.execute("SELECT count(*) FROM trecho").fetchone()[0]
        print(f"{ITENS * TRECHOS} trechos; a Lívia enxerga {vistos}")
        for sub, papel, nome in (("livia", "operador", "Lívia"),
                                 ("marta", "supervisor", "Marta")):
            tempo, n = buscar_como(app, sub, papel)
            print(f"busca híbrida da {nome}: mediana {ms(tempo, 0)}, {n} trechos")
        original, mem.POR_PALAVRAS = mem.POR_PALAVRAS, TODAS_AS_PALAVRAS
        try:
            tempo, n = buscar_como(app, "livia", "operador")
        finally:
            mem.POR_PALAVRAS = original
        with mem.como(app, "livia", "operador"):
            por_palavras = {nome: len(app.execute(sql, {"q": PERGUNTA, "n": 24}).fetchall())
                            for nome, sql in (("todas", TODAS_AS_PALAVRAS),
                                              ("qualquer", mem.POR_PALAVRAS))}
        print(f"exigindo todas as palavras (Lívia): mediana {ms(tempo, 0)}; "
              f"a parte de palavras acha {por_palavras['todas']} trechos "
              f"(com qualquer palavra, {por_palavras['qualquer']})")


if __name__ == "__main__":
    main()
