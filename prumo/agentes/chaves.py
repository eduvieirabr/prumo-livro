"""Chaves pessoais para IAs conectadas por MCP (capítulo 15).

A chave age como a pessoa. Só o hash é guardado; vale 90 dias; a pessoa
revoga quando quiser; no máximo 10 ativas; 60 usos por hora. A conta da
pessoa no Prumo ID é conferida de novo a cada 5 minutos: perdeu o acesso,
as chaves dela morrem.
"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from prumo.politica import aceite_vigente

VALIDADE = timedelta(days=90)
MAX_ATIVAS = 10
USOS_POR_HORA = 60
RECONFERIR = timedelta(minutes=5)

SCHEMA = """
CREATE TABLE IF NOT EXISTS chave_mcp (
    id             bigserial PRIMARY KEY,
    dono           text NOT NULL,
    hash           text NOT NULL UNIQUE,
    prefixo        text NOT NULL,                 -- os primeiros caracteres, para a tela
    criada_em      timestamptz NOT NULL DEFAULT now(),
    expira_em      timestamptz NOT NULL,
    revogada_em    timestamptz,
    conferida_em   timestamptz,
    ciencia_em     timestamptz NOT NULL           -- a pessoa registrou que as respostas saem da casa
);
CREATE TABLE IF NOT EXISTS uso_chave (
    chave bigint NOT NULL REFERENCES chave_mcp(id) ON DELETE CASCADE,
    em    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS uso_chave_em ON uso_chave (chave, em);
"""


class ChaveRecusada(Exception):
    pass


def _hash(chave: str) -> str:
    return hashlib.sha256(chave.encode()).hexdigest()


def criar(conn, dono: str, agora: datetime | None = None, *, ciencia: bool = False) -> str:
    """Devolve a chave uma única vez. Depois disso, nem o sistema a conhece."""
    if not ciencia:
        raise ChaveRecusada("sem a ciência de que as respostas vão para a IA conectada, não há chave")
    agora = agora or datetime.now(timezone.utc)
    with conn.transaction():
        # uma trava por pessoa: contar e inserir sem que outro pedido dela passe no meio
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("chave_mcp:" + dono,))
        ativas = conn.execute(
            """SELECT count(*) FROM chave_mcp
                WHERE dono = %s AND revogada_em IS NULL AND expira_em > %s""", (dono, agora)).fetchone()[0]
        if ativas >= MAX_ATIVAS:
            raise ChaveRecusada(f"já há {MAX_ATIVAS} chaves ativas; revogue uma antes")
        chave = "agk_" + secrets.token_urlsafe(32)
        conn.execute("""INSERT INTO chave_mcp (dono, hash, prefixo, expira_em, ciencia_em)
                        VALUES (%s, %s, %s, %s, %s)""",
                     (dono, _hash(chave), chave[:10], agora + VALIDADE, agora))
    return chave


def revogar(conn, dono: str, prefixo: str) -> int:
    return conn.execute(
        "UPDATE chave_mcp SET revogada_em = now() WHERE dono = %s AND prefixo = %s AND revogada_em IS NULL",
        (dono, prefixo)).rowcount


def usar(conn, chave: str, conta_ativa, agora: datetime | None = None) -> str:
    """Confere a chave e devolve o dono, em nome de quem a IA conectada vai agir.

    conta_ativa(dono) -> bool   pergunta ao Prumo ID se a pessoa ainda tem acesso
    """
    agora = agora or datetime.now(timezone.utc)
    perdeu_acesso = False
    with conn.transaction():
        linha = conn.execute(
            """SELECT id, dono, expira_em, revogada_em, conferida_em FROM chave_mcp
                WHERE hash = %s FOR UPDATE""", (_hash(chave),)).fetchone()
        if linha is None or linha[3] is not None:
            raise ChaveRecusada("chave inválida ou revogada")
        ident, dono, expira, _, conferida = linha
        if expira <= agora:
            raise ChaveRecusada("chave vencida")
        if conferida is None or agora - conferida >= RECONFERIR:
            if not conta_ativa(dono):
                conn.execute("UPDATE chave_mcp SET revogada_em = %s"
                             " WHERE dono = %s AND revogada_em IS NULL", (agora, dono))
                # recusa DEPOIS do commit, ou a revogação seria desfeita
                perdeu_acesso = True
            else:
                conn.execute("UPDATE chave_mcp SET conferida_em = %s WHERE id = %s",
                             (agora, ident))
        if not perdeu_acesso:
            usos = conn.execute("SELECT count(*) FROM uso_chave WHERE chave = %s AND em > %s",
                                (ident, agora - timedelta(hours=1))).fetchone()[0]
            if usos >= USOS_POR_HORA:
                raise ChaveRecusada(f"limite de {USOS_POR_HORA} usos por hora")
            conn.execute("INSERT INTO uso_chave (chave, em) VALUES (%s, %s)", (ident, agora))
    if perdeu_acesso:
        raise ChaveRecusada("a pessoa perdeu o acesso; as chaves dela foram revogadas")
    return dono


def liberar_cliente(conn, empresa: str | None, hoje) -> None:
    """Pelo MCP, pergunta e agente só rodam para um cliente informado, com aceite vigente.

    O servidor MCP chama isto depois de `usar`, antes de rodar. A chave age como a
    pessoa; o aceite `ia_conectada` é do cliente, e a chave não o substitui.
    """
    if not empresa:
        raise ChaveRecusada("informe o cliente: pelo MCP, nada roda sem cliente")
    if not aceite_vigente(conn, empresa, "ia_conectada", hoje):
        raise ChaveRecusada(f"o cliente {empresa} não autorizou o uso por IA conectada")


def liberar_saida(conn, empresa: str | None, fontes: list[dict], hoje) -> None:
    """Na hora de devolver qualquer conteúdo pela chave (perguntar, ver resposta, ver
    execução): o aceite vale hoje, e toda fonte é do cliente informado ou interna.

    Conferir só o cliente do formulário não basta: a pessoa pode ver outros clientes,
    e uma fonte deles dentro da resposta sairia sem o aceite deles.
    """
    liberar_cliente(conn, empresa, hoje)
    outros = {f.get("empresa") for f in fontes} - {empresa, None}
    if outros:
        raise ChaveRecusada(f"a resposta usa dados de outro cliente ({len(outros)}); não sai")
