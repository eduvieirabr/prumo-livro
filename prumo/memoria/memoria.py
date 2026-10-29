"""Memória Prumo: guardar, buscar e responder com fontes (capítulo 10).

Quem pode ver o quê é regra do banco (Row Level Security). A aplicação
só informa, a cada transação, quem está pedindo e com qual papel.
"""
import hashlib
import math
import re
from contextlib import contextmanager
from pathlib import Path

import psycopg

DIMENSOES = 64
DISTANCIA_MAXIMA = 0.6       # cosseno: acima disso, "parecido" não quer dizer nada
SEM_FONTES = "Não encontrei isso nos documentos que você pode ver."

_CNPJ = re.compile(r"\b[0-9A-Z]{2}\.?[0-9A-Z]{3}\.?[0-9A-Z]{3}/?[0-9A-Z]{4}-?\d{2}\b")


def criar_schema(conn_dono) -> None:
    conn_dono.execute((Path(__file__).parent / "schema.sql").read_text())


@contextmanager
def como(conn, sub: str, papel: str):
    """Uma transação em nome de alguém. O set_config local some no fim da transação.

    Precisa abrir a transação: dentro de outra, ela viraria um savepoint e a
    identidade sobreviveria até o fim da transação de fora.
    """
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError("como() precisa de uma conexão sem transação aberta")
    with conn.transaction():
        conn.execute("SELECT set_config('mem.sub', %s, true),"
                     " set_config('mem.papel', %s, true)", (sub, papel))
        yield conn


def so_caracteres(cnpj: str) -> str:
    return re.sub(r"[./-]", "", cnpj)


def variantes(texto: str) -> str:
    """Na gravação: os CNPJs do texto, também sem pontuação."""
    return " ".join(so_caracteres(c) for c in _CNPJ.findall(texto.upper()))


def normalizar_consulta(consulta: str) -> str:
    """Na busca: o CNPJ da pergunta vira a forma sem pontuação, que todo trecho guarda."""
    return _CNPJ.sub(lambda m: so_caracteres(m.group(0)), consulta.upper())


def vetor_de_brinquedo(texto: str) -> list[float]:
    """Embedding de mentira, para os testes: palavras espalhadas em 64 posições.

    Em produção, os vetores vêm do Broker, numa tarefa própria. O resto do
    código não muda: só a origem dos números.
    """
    v = [0.0] * DIMENSOES
    for palavra in re.findall(r"\w{4,}", texto.lower()):      # ignora "da", "o", "de"...
        h = int(hashlib.md5(palavra.encode()).hexdigest(), 16)
        v[h % DIMENSOES] += 1.0 if (h >> 8) % 2 else -1.0
    norma = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norma for x in v]


def guardar(conn, titulo: str, trechos: list[str], *, escopo: str = "pessoal",
            restrito: bool = False, empresa: str | None = None,
            embed=vetor_de_brinquedo) -> int:
    """Grava um item e seus trechos em nome de quem está na transação.

    `empresa` é o CNPJ do cliente de quem o item fala; sem ele, o item é conhecimento
    interno do escritório, e quem guarda responde por não pôr dado de cliente nele.
    """
    item = conn.execute(
        """INSERT INTO item (dono, escopo, restrito, titulo, empresa)
           VALUES (mem_sub(), %s, %s, %s, %s) RETURNING id""",
        (escopo, restrito, titulo, empresa)).fetchone()[0]
    for ordem, texto in enumerate(trechos):
        conn.execute(
            "INSERT INTO trecho (item, ordem, texto, extra, vetor) VALUES (%s, %s, %s, %s, %s)",
            (item, ordem, texto, variantes(texto), str(embed(texto)) if embed else None))
    return item


# Qualquer palavra da pergunta serve (OU); quem tem mais delas, e mais perto, sobe.
POR_PALAVRAS = """
WITH q AS (
    SELECT nullif(replace(plainto_tsquery('portuguese', mem_sem_acento(%(q)s))::text,
                          ' & ', ' | '), '')::tsquery AS consulta
)
SELECT t.id FROM trecho t, q
 WHERE t.busca @@ q.consulta AND {do_cliente}
 ORDER BY ts_rank_cd(t.busca, q.consulta) DESC
 LIMIT %(n)s
"""

POR_SIGNIFICADO = """
SELECT t.id FROM trecho t
 WHERE t.vetor IS NOT NULL AND t.vetor <=> %(v)s::vector < %(max)s AND {do_cliente}
 ORDER BY t.vetor <=> %(v)s::vector
 LIMIT %(n)s
"""


# com um cliente informado (o MCP, cap. 15), só itens dele ou internos entram na busca
DO_CLIENTE = """(%(empresa)s::text IS NULL OR t.item IN (
    SELECT id FROM item WHERE empresa = %(empresa)s OR empresa IS NULL))"""
POR_PALAVRAS = POR_PALAVRAS.format(do_cliente=DO_CLIENTE)
POR_SIGNIFICADO = POR_SIGNIFICADO.format(do_cliente=DO_CLIENTE)


def rrf(*listas: list[int], k: int = 60) -> list[int]:
    """Reciprocal Rank Fusion: soma 1/(k + posição) de cada lista em que o trecho aparece."""
    pontos: dict[int, float] = {}
    for lista in listas:
        for posicao, ident in enumerate(lista, start=1):
            pontos[ident] = pontos.get(ident, 0.0) + 1.0 / (k + posicao)
    return sorted(pontos, key=lambda i: -pontos[i])


def buscar(conn, consulta: str, *, n: int = 8, embed=vetor_de_brinquedo,
           empresa: str | None = None) -> list[dict]:
    """Busca híbrida, dentro do que a pessoa da transação pode ver."""
    q = normalizar_consulta(consulta)
    palavras = [r[0] for r in conn.execute(POR_PALAVRAS,
                                           {"q": q, "n": n * 3, "empresa": empresa})]
    conn.execute("SET LOCAL hnsw.ef_search = 200")   # o índice filtra a política DEPOIS
    significado = [r[0] for r in conn.execute(
        POR_SIGNIFICADO, {"v": str(embed(consulta)), "n": n * 3,
                          "max": DISTANCIA_MAXIMA, "empresa": empresa})] if embed else []
    ids = rrf(palavras, significado)[:n]
    if not ids:
        return []
    linhas = conn.execute(
        """SELECT t.id, t.texto, i.id, i.titulo, i.empresa
             FROM trecho t JOIN item i ON i.id = t.item
            WHERE t.id = ANY(%s)""", (ids,)).fetchall()
    por_id = {l[0]: {"trecho": l[0], "texto": l[1], "item": l[2], "titulo": l[3],
                     "empresa": l[4]} for l in linhas}
    return [por_id[i] for i in ids if i in por_id]


def visiveis(conn, itens) -> set[int]:
    """Dos itens pedidos, os que a pessoa da transação ainda pode ver. Quem decide é a RLS."""
    return {r[0] for r in conn.execute("SELECT id FROM item WHERE id = ANY(%s)", (list(itens),))}


# ---------- responder com fontes ----------

INSTRUCOES = """Responda usando SOMENTE as fontes numeradas.
Cite a fonte de cada afirmação assim: [1], [2].
Se a resposta não estiver nas fontes, diga que não encontrou."""


def montar_fontes(trechos: list[dict]) -> str:
    return "\n\n".join(f"[{n}] {t['titulo']}\n{t['texto']}" for n, t in enumerate(trechos, 1))


def citacoes_invalidas(resposta: str, total: int) -> list[int]:
    return sorted({int(n) for n in re.findall(r"\[(\d+)\]", resposta)
                   if not 1 <= int(n) <= total})


def responder(conn, pergunta: str, llm, **busca) -> dict:
    fontes = buscar(conn, pergunta, **busca)
    if not fontes:
        return {"resposta": SEM_FONTES, "fontes": []}       # sem fonte, nem chama o modelo
    texto = llm(f"{INSTRUCOES}\n\n<fontes>\n{montar_fontes(fontes)}\n</fontes>\n\n{pergunta}")
    ruins = citacoes_invalidas(texto, len(fontes))
    if ruins or not re.search(r"\[\d+\]", texto):
        return {"resposta": SEM_FONTES, "fontes": fontes, "recusada": texto}
    return {"resposta": texto, "fontes": fontes}
