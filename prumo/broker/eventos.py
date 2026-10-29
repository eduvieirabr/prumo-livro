"""Eventos de cada pedido (capítulo 7).

O evento é gravado na mesma transação que muda o estado do pedido: é uma
outbox. Se a transação não acontecer, o evento também não; se acontecer,
o evento está lá para quem vier ler, agora ou depois de uma reconexão.
"""
from psycopg.types.json import Jsonb

from prumo.broker.fila import concluir, encerrar, falhar

TERMINAIS = ("concluido", "erro")


def publicar(conn, pedido_id, tipo: str, dados: dict | None = None) -> None:
    conn.execute("INSERT INTO evento (pedido, tipo, dados) VALUES (%s, %s, %s)",
                 (pedido_id, tipo, Jsonb(dados or {})))


def concluir_e_avisar(conn, pedido: dict, resultado, **consumo) -> bool:
    with conn.transaction():
        ok = concluir(conn, pedido, resultado, **consumo)
        if ok:
            publicar(conn, pedido["id"], "concluido", {"resultado": resultado})
    return ok


def falhar_e_avisar(conn, pedido: dict, erro: str, ms: int) -> None:
    with conn.transaction():
        if falhar(conn, pedido, erro, ms):
            status = conn.execute("SELECT status FROM pedido WHERE id = %s",
                                  (pedido["id"],)).fetchone()[0]
            publicar(conn, pedido["id"], "erro" if status == "erro" else "nova_tentativa",
                     {"erro": erro})


def encerrar_e_avisar(conn, pedido: dict, erro: str, ms: int) -> None:
    with conn.transaction():
        if encerrar(conn, pedido, erro, ms):
            publicar(conn, pedido["id"], "erro", {"erro": erro})


def eventos_depois(conn, pedido_id, seq: int) -> list[tuple]:
    return conn.execute(
        "SELECT seq, tipo, dados FROM evento WHERE pedido = %s AND seq > %s ORDER BY seq",
        (pedido_id, seq)).fetchall()
