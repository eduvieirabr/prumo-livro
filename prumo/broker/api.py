"""API do Broker de IA (capítulos 5, 7 e 9). As aplicações falam com ela, nunca com o Ollama.

    BROKER_DSN=postgresql://... uvicorn prumo.broker.api:app --port 8080
"""
import hashlib
import json
import os
import time

import httpx
import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import StreamingResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from prumo.broker.eventos import TERMINAIS, eventos_depois
from prumo.identidade import TokenInvalido, Verificador
from prumo.politica import POLITICA

MODELO = os.getenv("BROKER_MODELO", "qwen3:14b")
VERSAO_PROMPT = {"extrair_debitos": "extracao@3", "assistente": "assistente@2"}
REAPROVEITAR_POR = "48 hours"

app = FastAPI(title="Broker de IA")


def conexao():
    with psycopg.connect(os.environ["BROKER_DSN"], autocommit=True) as conn:
        yield conn


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def integracao_do_token(authorization: str = Header(...), conn=Depends(conexao)):
    token = authorization.removeprefix("Bearer ").strip()
    if token.count(".") == 2:                 # token de serviço do Prumo ID (capítulo 9)
        return integracao_do_servico(token, conn)
    linha = conn.execute(
        "SELECT id, tarefas, interativa, avaliacao FROM integracao"
        " WHERE token_hash = %s AND ativa",
        (hash_token(token),)).fetchone()
    if linha is None:
        raise HTTPException(401, "token inválido")
    return dict(zip(("id", "tarefas", "interativa", "avaliacao"), linha))


class NovoPedido(BaseModel):
    tarefa: str
    conteudo: dict
    interativo: bool = False
    reaproveitar: bool = True        # avaliação do modelo (cap. 18): False exige inferência nova


def chave_do_pedido(p: NovoPedido) -> str:
    material = json.dumps([p.tarefa, VERSAO_PROMPT.get(p.tarefa), MODELO, p.conteudo],
                          sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(material.encode()).hexdigest()


@app.post("/v1/pedidos", status_code=202)
def criar_pedido(p: NovoPedido, integ=Depends(integracao_do_token), conn=Depends(conexao)):
    if p.tarefa not in integ["tarefas"]:
        raise HTTPException(403, f"integração sem permissão para '{p.tarefa}'")
    if p.tarefa not in POLITICA:
        raise HTTPException(403, f"tarefa '{p.tarefa}' sem regra na política")
    if not p.reaproveitar and not integ["avaliacao"]:
        raise HTTPException(403, "só a integração de avaliação desliga o reaproveitamento")
    chave = chave_do_pedido(p)
    anterior = p.reaproveitar and conn.execute(
        """SELECT id, resultado FROM pedido
            WHERE integracao = %s AND chave = %s AND status = 'concluido'
              AND resultado IS NOT NULL AND concluido_em > now() - %s::interval
            ORDER BY concluido_em DESC LIMIT 1""",
        (integ["id"], chave, REAPROVEITAR_POR)).fetchone()
    if anterior:
        novo = conn.execute(
            """INSERT INTO pedido (integracao, tarefa, chave, status, origem, concluido_em)
               VALUES (%s, %s, %s, 'reaproveitado', %s, now()) RETURNING id""",
            (integ["id"], p.tarefa, chave, anterior[0])).fetchone()
        return {"id": str(novo[0]), "status": "reaproveitado", "resultado": anterior[1]}
    prioridade = 0 if p.interativo and integ["interativa"] else 1
    novo = conn.execute(
        """INSERT INTO pedido (integracao, tarefa, prioridade, chave, conteudo)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (integ["id"], p.tarefa, prioridade, chave, Jsonb(p.conteudo))).fetchone()
    return {"id": str(novo[0]), "status": "pendente"}


@app.get("/v1/pedidos/{pedido_id}")
def ver_pedido(pedido_id: str, integ=Depends(integracao_do_token), conn=Depends(conexao)):
    linha = conn.execute(
        """SELECT p.status, coalesce(p.resultado, o.resultado)
             FROM pedido p LEFT JOIN pedido o ON o.id = p.origem
            WHERE p.id = %s AND p.integracao = %s""", (pedido_id, integ["id"])).fetchone()
    if linha is None:
        raise HTTPException(404, "pedido não encontrado")
    return {"id": pedido_id, "status": linha[0], "resultado": linha[1]}


def apagar_conteudo_antigo(conn) -> int:
    """Conteúdo e resultado somem 48 h depois de concluídos; o registro de consumo fica.

    Chamada por uma tarefa agendada, a cada hora. Pedidos na fila não são tocados.
    """
    cur = conn.execute(
        """UPDATE pedido SET conteudo = NULL, resultado = NULL
            WHERE status IN ('concluido', 'erro', 'reaproveitado')
              AND concluido_em < now() - %s::interval
              AND (conteudo IS NOT NULL OR resultado IS NOT NULL)""", (REAPROVEITAR_POR,))
    conn.execute(                        # os eventos carregam resultado e pedaços de texto
        """DELETE FROM evento WHERE pedido IN (
               SELECT id FROM pedido WHERE status IN ('concluido', 'erro', 'reaproveitado')
                  AND concluido_em < now() - %s::interval)""", (REAPROVEITAR_POR,))
    return cur.rowcount


# ---------- capítulo 7: o resultado chega por eventos ----------

ESPERA_S = float(os.getenv("BROKER_ESPERA_S", "1800"))


@app.get("/v1/pedidos/{pedido_id}/eventos")
def eventos(pedido_id: str, integ=Depends(integracao_do_token), conn=Depends(conexao),
            last_event_id: str | None = Header(None)):
    linha = conn.execute("SELECT status FROM pedido WHERE id = %s AND integracao = %s",
                         (pedido_id, integ["id"])).fetchone()
    if linha is None:
        raise HTTPException(404, "pedido não encontrado")
    inicio = int(last_event_id or 0)
    if linha[0] in TERMINAIS + ("reaproveitado",) and not eventos_depois(conn, pedido_id, inicio):
        return Response(status_code=204)    # nada mais a mandar: o EventSource para de reconectar

    def fluxo():
        visto, fim, ultimo_envio = inicio, time.monotonic() + ESPERA_S, time.monotonic()
        with psycopg.connect(os.environ["BROKER_DSN"], autocommit=True) as c:
            while time.monotonic() < fim:
                for seq, tipo, dados in eventos_depois(c, pedido_id, visto):
                    visto, ultimo_envio = seq, time.monotonic()
                    corpo = json.dumps(dados, ensure_ascii=False)
                    yield f"id: {seq}\nevent: {tipo}\ndata: {corpo}\n\n"
                    if tipo in TERMINAIS:
                        return
                if time.monotonic() - ultimo_envio > 15:
                    ultimo_envio = time.monotonic()
                    yield ": ping\n\n"        # mantém proxies acordados e descobre quem saiu
                time.sleep(0.3)
        yield ": tempo de espera esgotado\n\n"

    return StreamingResponse(fluxo(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- capítulo 9: token de serviço do Prumo ID ----------

AUDIENCIA = "prumo-broker"
VERIFICADOR: Verificador | None = None


def verificador() -> Verificador:
    global VERIFICADOR
    if VERIFICADOR is None:
        jwks = os.environ["PRUMO_ID_JWKS"]
        VERIFICADOR = Verificador(os.environ["PRUMO_ID_EMISSOR"], AUDIENCIA,
                                  lambda: httpx.get(jwks, timeout=10).json())
    return VERIFICADOR


def integracao_do_servico(token: str, conn) -> dict:
    try:
        claims = verificador().verificar(token)
    except TokenInvalido:
        raise HTTPException(401, "token inválido")
    if claims.get("client_id") != claims.get("azp"):   # só conta de serviço, não pessoa
        raise HTTPException(401, "token de pessoa não vale como token de sistema")
    linha = conn.execute(
        "SELECT id, tarefas, interativa, avaliacao FROM integracao"
        " WHERE cliente_sso = %s AND ativa",
        (claims.get("azp"),)).fetchone()
    if linha is None:
        raise HTTPException(403, "cliente sem integração cadastrada")
    return dict(zip(("id", "tarefas", "interativa", "avaliacao"), linha))
