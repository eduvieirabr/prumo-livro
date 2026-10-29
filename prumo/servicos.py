"""Os processos que rodam fora da API (apêndice A): o consumidor do Broker e o do Parcelamentos.

    python -m prumo.servicos broker          # vagas de GPU do capítulo 6
    python -m prumo.servicos parcelamentos   # baixas e outbox do capítulo 17
"""
import json
import os
import sys
import threading
import time
from dataclasses import asdict

import httpx
import psycopg

from prumo.avaliacao import identidade_do_modelo
from prumo.broker import consumidor
from prumo.broker.geracao import gerar_ollama
from prumo.central.cliente import CentralFora, Cliente
from prumo.leitura.blocos import ler_longo
from prumo.parcelamentos import baixa


class TokenDeServico:
    """Credenciais de cliente no Prumo ID (capítulo 9), com o token guardado até perto de vencer."""

    def __init__(self, url_token: str, cliente: str, segredo: str, *, http=httpx, agora=time.time):
        self.url, self.cliente, self.segredo = url_token, cliente, segredo
        self.http, self.agora = http, agora
        self._token, self._vence = None, 0.0

    def __call__(self) -> str:
        if self._token is None or self.agora() > self._vence - 30:
            r = self.http.post(self.url, data={"grant_type": "client_credentials",
                                               "client_id": self.cliente, "client_secret": self.segredo},
                               timeout=10)
            r.raise_for_status()
            corpo = r.json()
            self._token, self._vence = corpo["access_token"], self.agora() + corpo["expires_in"]
        return self._token


def chamador(url: str, modelo: str, opcoes: dict, identidade: str | None = None):
    """A função que cada vaga do Broker chama: (tarefa, conteúdo) -> (resultado, tokens).

    Todo resultado leva a identidade do modelo que de fato o produziu (nome@digest).
    """
    identidade = identidade or modelo

    def chamar(tarefa, conteudo, *, parcial, limite_s):
        if tarefa == "ler_documento":
            resultado, entrada, saida = ler_documento(conteudo["paginas"], parcial=parcial,
                                                      limite_s=limite_s)
            return {**resultado, "modelo": identidade}, entrada, saida
        # o prompt é da aplicação e vem pronto no conteúdo; sem ele, vai o conteúdo em JSON
        texto = conteudo.get("prompt") or json.dumps(conteudo, ensure_ascii=False)
        corpo = {"model": modelo, "stream": True, "think": tarefa == "assistente",
                 "options": opcoes, "messages": [{"role": "user", "content": texto}]}
        texto, entrada, saida = gerar_ollama(url, corpo, parcial=parcial, limite_s=limite_s)
        return {"texto": texto, "modelo": identidade}, entrada, saida

    def ler_documento(paginas, *, parcial, limite_s):
        """Caps. 4 e 7: os blocos de um documento longo, num pedido só e num prazo só."""
        fim, tokens = time.monotonic() + limite_s, [0, 0]

        def llm(prompt):
            corpo = {"model": modelo, "stream": True, "think": False, "options": opcoes,
                     "messages": [{"role": "user", "content": prompt}]}
            texto, entrada, saida = gerar_ollama(url, corpo, parcial=parcial,
                                                 limite_s=fim - time.monotonic())
            tokens[0], tokens[1] = tokens[0] + entrada, tokens[1] + saida
            return texto
        leitura = ler_longo(paginas, llm)
        recibo = asdict(leitura.recibo) if leitura.recibo else None
        resultado = {"aprovada": leitura.aprovada, "recibo": recibo,
                     "problemas": leitura.problemas, "falhos": leitura.falhos}
        return json.loads(json.dumps(resultado, default=str)), tokens[0], tokens[1]
    return chamar


def broker() -> None:
    opcoes = {"num_ctx": int(os.getenv("NUM_CTX", "16384")),
              "temperature": float(os.getenv("TEMPERATURA", "0"))}
    url, modelo = os.environ["OLLAMA_URL"], os.getenv("BROKER_MODELO", "qwen3:14b")
    try:                       # o arquivo que este servidor vai usar, e não só o apelido
        tags = httpx.get(f"{url}/api/tags", timeout=10).raise_for_status().json()
        identidade = identidade_do_modelo(tags, modelo, completo=True)
    except (httpx.HTTPError, LookupError) as erro:
        identidade = f"{modelo}@desconhecido"
        print(f"identidade do modelo indisponível: {erro}", flush=True)
    chamar = chamador(url, modelo, opcoes, identidade)
    consumidor.iniciar(os.environ["BROKER_DSN"], os.environ.get("SERVIDOR", "gpu-1"),
                       int(os.getenv("VAGAS", "2")), chamar)
    threading.Event().wait()


def parcelamentos() -> None:
    token = TokenDeServico(os.environ["PRUMO_ID_TOKEN_URL"], os.environ["CLIENTE_ID"],
                           os.environ["CLIENTE_SEGREDO"])
    cliente = Cliente(os.environ["CENTRAL_URL"], token)
    intervalo = float(os.getenv("INTERVALO_S", "60"))
    with psycopg.connect(os.environ["PARCELAMENTOS_DSN"], autocommit=True) as conn:
        while True:
            try:
                for linha in baixa.consumir(conn, cliente):
                    print(linha, flush=True)
                baixa.enviar_saida(conn, cliente)
            except CentralFora as erro:             # a central volta; o cursor espera
                print(f"central fora do ar: {erro}", flush=True)
            except httpx.HTTPStatusError as erro:   # 409 de eventos perdidos, 403 de permissão:
                print(f"central recusou: {erro.response.status_code} {erro.response.text}", flush=True)
            time.sleep(intervalo)


if __name__ == "__main__":
    {"broker": broker, "parcelamentos": parcelamentos}[sys.argv[1]]()
