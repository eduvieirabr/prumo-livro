"""API HTTP da Central de Eventos (capítulo 16). JSON sobre HTTP: qualquer linguagem fala.

    CENTRAL_DSN=postgresql://... uvicorn prumo.central.api:app --port 8090
"""
import os

import httpx
import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from prumo.central import central as c
from prumo.identidade import TokenInvalido, Verificador

AUDIENCIA = "prumo-central"
VERIFICADOR: Verificador | None = None

app = FastAPI(title="Central de Eventos")


def verificador() -> Verificador:
    global VERIFICADOR
    if VERIFICADOR is None:
        jwks = os.environ["PRUMO_ID_JWKS"]
        VERIFICADOR = Verificador(os.environ["PRUMO_ID_EMISSOR"], AUDIENCIA,
                                  lambda: httpx.get(jwks, timeout=10).json())
    return VERIFICADOR


def conexao():
    with psycopg.connect(os.environ["CENTRAL_DSN"], autocommit=True) as conn:
        yield conn


def aplicacao(authorization: str | None = Header(None), conn=Depends(conexao)) -> str:
    """Token de serviço do Prumo ID, com aud da central. Quem publica é o azp, não o corpo."""
    token = (authorization or "").removeprefix("Bearer ").strip()
    try:
        claims = verificador().verificar(token)
    except TokenInvalido:
        raise HTTPException(401, "token inválido")
    if not claims.get("azp") or claims.get("client_id") != claims["azp"]:
        raise HTTPException(401, "token de pessoa não publica nem lê eventos")
    linha = conn.execute("SELECT id FROM central.aplicacao WHERE cliente_sso = %s",
                         (claims["azp"],)).fetchone()
    if linha is None:
        raise HTTPException(403, "aplicação não cadastrada")
    return linha[0]


@app.exception_handler(c.SemPermissao)
def _sem_permissao(_, erro):
    return JSONResponse({"detail": str(erro)}, status_code=403)


@app.exception_handler(c.EventoInvalido)
def _invalido(_, erro):
    return JSONResponse({"detail": str(erro)}, status_code=422)


@app.exception_handler(c.CanalInexistente)
def _inexistente(_, erro):
    return JSONResponse({"detail": str(erro)}, status_code=404)


@app.exception_handler(c.EventosPerdidos)
def _perdidos(_, erro):
    return JSONResponse({"detail": str(erro), "cursor": erro.cursor,
                         "primeiro_disponivel": erro.primeiro}, status_code=409)


class Lote(BaseModel):
    eventos: list[dict]


class Confirmacao(BaseModel):
    ate: int


@app.post("/v1/canais/{canal}/eventos", status_code=201)
def publicar(canal: str, lote: Lote, app_id: str = Depends(aplicacao), conn=Depends(conexao)):
    return {"seqs": c.publicar(conn, app_id, canal, lote.eventos)}


@app.get("/v1/inscricoes/{canal}/eventos")
def ler(canal: str, maximo: int = 100, app_id: str = Depends(aplicacao), conn=Depends(conexao)):
    return {"eventos": c.ler(conn, app_id, canal, maximo)}


@app.post("/v1/inscricoes/{canal}/cursor")
def confirmar(canal: str, corpo: Confirmacao, app_id: str = Depends(aplicacao),
              conn=Depends(conexao)):
    return {"cursor": c.confirmar(conn, app_id, canal, corpo.ate)}
