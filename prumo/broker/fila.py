"""Fila de pedidos no PostgreSQL (capítulo 6).

Um pedido só sai da fila com uma vaga de GPU, e a vaga tem prazo e dono.
Se o consumidor morrer, o prazo vence e o pedido volta para a fila.
Quem perdeu a vaga não consegue mais gravar o resultado.
"""
import uuid
from pathlib import Path

ENVELHECER = "10 minutes"   # lote mais velho que isso ganha uma vaga, nunca todas
MAX_TENTATIVAS = 3
TRAVA_FILA = 7_140_002      # uma decisão de vaga por vez, em todos os consumidores

PEGAR = """
WITH proximo AS (
    SELECT id FROM pedido
    WHERE (status = 'pendente'
           OR (status = 'executando' AND vaga_ate < now()))
      AND tentativas < %(max)s
    ORDER BY CASE WHEN prioridade = 0 THEN 0
                  WHEN criado_em < now() - %(envelhecer)s::interval
                       AND NOT EXISTS (SELECT 1 FROM pedido em
                                        WHERE em.status = 'executando' AND em.prioridade = 1
                                          AND em.vaga_ate >= now())
                  THEN 0
                  ELSE 1 END,
             criado_em
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
UPDATE pedido p
   SET status = 'executando', servidor = %(servidor)s, vaga_token = %(token)s,
       vaga_ate = now() + make_interval(secs => %(prazo)s), tentativas = tentativas + 1
  FROM proximo
 WHERE p.id = proximo.id
RETURNING p.id, p.tarefa, p.conteudo, p.vaga_token, p.tentativas
"""

CONCLUIR = """
UPDATE pedido
   SET status = 'concluido', resultado = %(resultado)s, concluido_em = now(),
       tokens_entrada = %(entrada)s, tokens_saida = %(saida)s, duracao_ms = %(ms)s,
       vaga_token = NULL, vaga_ate = NULL
 WHERE id = %(id)s AND vaga_token = %(token)s AND status = 'executando'
"""

DESCARTAR = """
UPDATE pedido SET descartes = descartes + 1, ms_descartados = ms_descartados + %(ms)s
 WHERE id = %(id)s
"""

FALHAR = """
UPDATE pedido
   SET status = CASE WHEN tentativas >= %(max)s THEN 'erro' ELSE 'pendente' END,
       concluido_em = CASE WHEN tentativas >= %(max)s THEN now() END,
       resultado = jsonb_build_object('erro', %(erro)s::text),
       ms_descartados = ms_descartados + %(ms)s,
       vaga_token = NULL, vaga_ate = NULL
 WHERE id = %(id)s AND vaga_token = %(token)s AND status = 'executando'
"""

ENCERRAR_VENCIDOS = """
WITH e AS (
    UPDATE pedido
       SET status = 'erro', concluido_em = now(), vaga_token = NULL, vaga_ate = NULL,
           resultado = jsonb_build_object('erro', 'vaga vencida em todas as tentativas')
     WHERE status = 'executando' AND vaga_ate < now() AND tentativas >= %(max)s
    RETURNING id
)
INSERT INTO evento (pedido, tipo, dados)
SELECT id, 'erro', jsonb_build_object('erro', 'vaga vencida em todas as tentativas') FROM e
"""


def criar_schema(conn) -> None:
    conn.execute((Path(__file__).parent / "schema.sql").read_text())


def pegar_proximo(conn, servidor: str, prazo_s: int = 600) -> dict | None:
    """Reserva o próximo pedido para este servidor, ou None se a fila estiver vazia.

    A regra "um lote velho por vez" lê o estado da fila inteira: dois consumidores que a
    avaliassem juntos promoveriam dois lotes. A trava faz um decidir depois do outro.
    """
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (TRAVA_FILA,))
        encerrar_vencidos(conn)          # quem esgotou as tentativas sai da fila como erro
        linha = conn.execute(PEGAR, {"servidor": servidor, "token": uuid.uuid4(),
                                     "prazo": prazo_s, "envelhecer": ENVELHECER,
                                     "max": MAX_TENTATIVAS}).fetchone()
    if linha is None:
        return None
    return {"id": linha[0], "tarefa": linha[1], "conteudo": linha[2], "token": linha[3],
            "tentativa": linha[4]}


def concluir(conn, pedido: dict, resultado, *, entrada: int, saida: int, ms: int) -> bool:
    """Grava o resultado. False quando a vaga já não é deste consumidor."""
    from psycopg.types.json import Jsonb
    cur = conn.execute(CONCLUIR, {"id": pedido["id"], "token": pedido["token"],
                                  "resultado": Jsonb(resultado),
                                  "entrada": entrada, "saida": saida, "ms": ms})
    if cur.rowcount == 1:
        return True
    # trabalho jogado fora também é consumo
    conn.execute(DESCARTAR, {"id": pedido["id"], "ms": ms})
    return False


def falhar(conn, pedido: dict, erro: str, ms: int) -> bool:
    """Devolve à fila, ou encerra com erro depois de MAX_TENTATIVAS."""
    return conn.execute(FALHAR, {"id": pedido["id"], "token": pedido["token"], "erro": erro,
                                 "ms": ms, "max": MAX_TENTATIVAS}).rowcount == 1


def encerrar_vencidos(conn) -> int:
    """Pedido cuja vaga venceu em todas as tentativas vira erro, em vez de ficar na fila."""
    return conn.execute(ENCERRAR_VENCIDOS, {"max": MAX_TENTATIVAS}).rowcount


# ---------- capítulo 7 ----------

RENOVAR = """
UPDATE pedido SET vaga_ate = now() + make_interval(secs => %(prazo)s)
 WHERE id = %(id)s AND vaga_token = %(token)s AND status = 'executando'
"""

ENCERRAR = """
UPDATE pedido
   SET status = 'erro', concluido_em = now(), resultado = jsonb_build_object('erro', %(erro)s::text),
       ms_descartados = ms_descartados + %(ms)s, vaga_token = NULL, vaga_ate = NULL
 WHERE id = %(id)s AND vaga_token = %(token)s AND status = 'executando'
"""


def renovar(conn, pedido: dict, prazo_s: int) -> bool:
    """Estende a vaga. False quando ela já não é deste consumidor."""
    return conn.execute(RENOVAR, {"id": pedido["id"], "token": pedido["token"],
                                  "prazo": prazo_s}).rowcount == 1


def encerrar(conn, pedido: dict, erro: str, ms: int) -> bool:
    """Erro sem nova tentativa: repetir só repetiria o estouro."""
    return conn.execute(ENCERRAR, {"id": pedido["id"], "token": pedido["token"],
                                   "erro": erro, "ms": ms}).rowcount == 1
