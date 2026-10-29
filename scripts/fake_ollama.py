"""Ollama simulado: responde sem GPU, para rodar os exemplos.

Modo 'fiel' devolve os valores do bloco <calculo>; modo 'distraido' troca
dois dígitos do primeiro valor, como o protótipo do capítulo 1.
    FAKE_OLLAMA_MODO=distraido uvicorn scripts.fake_ollama:app --port 11434

Streaming (capítulo 7): FAKE_OLLAMA_PEDACOS pedaços, FAKE_OLLAMA_PAUSA_S entre
eles, e FAKE_OLLAMA_TRAVA_S de silêncio depois do primeiro (modelo travado).
Também responde no formato OpenAI em /v1/chat/completions (capítulo 8).
"""
import asyncio
import json
import os
import re

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()
_REAIS = re.compile(r"R\$ [\d.]+,\d{2}")


def _troca_centavos(valor: str) -> str:
    return valor[:-2] + valor[-1] + valor[-2]


def _resposta(pedido: dict) -> str:
    texto = pedido["messages"][-1]["content"]
    valores = _REAIS.findall(texto)
    if os.getenv("FAKE_OLLAMA_MODO", "fiel") == "distraido" and valores:
        valores[0] = _troca_centavos(valores[0])
    return "Valores do cálculo: " + "; ".join(valores) if valores else "Sem valores."


@app.get("/api/tags")
async def tags():
    return {"models": [{"name": "qwen3:14b"}, {"name": "qwen2.5:7b"}]}


async def _fluxo(pedido: dict):
    pedacos = int(os.getenv("FAKE_OLLAMA_PEDACOS", "5"))
    pausa = float(os.getenv("FAKE_OLLAMA_PAUSA_S", "0"))
    trava = float(os.getenv("FAKE_OLLAMA_TRAVA_S", "0"))
    for i in range(pedacos):
        await asyncio.sleep(pausa)
        yield json.dumps({"message": {"content": f"parte {i + 1}. "}, "done": False}) + "\n"
        if i == 0 and trava:
            await asyncio.sleep(trava)
    yield json.dumps({"message": {"content": ""}, "done": True,
                      "prompt_eval_count": 120, "eval_count": pedacos}) + "\n"


@app.post("/api/chat")
async def chat(pedido: dict):
    await asyncio.sleep(float(os.getenv("FAKE_OLLAMA_ATRASO_S", "0")))
    if pedido.get("stream"):
        return StreamingResponse(_fluxo(pedido), media_type="application/x-ndjson")
    return {"model": pedido.get("model"), "done": True,
            "message": {"role": "assistant", "content": _resposta(pedido)}}


@app.post("/v1/chat/completions")
async def openai(pedido: dict):
    texto = _resposta(pedido)
    uso = None if os.getenv("FAKE_GATEWAY_SEM_USO") else {
        "prompt_tokens": 900, "completion_tokens": 150, "total_tokens": 1050}
    return {"choices": [{"message": {"role": "assistant", "content": texto}}], "usage": uso}
