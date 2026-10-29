"""Geração em streaming, com dois relógios (capítulo 7).

Silêncio: tempo máximo sem receber nada do modelo. Pega o modelo travado.
Duração: tempo máximo da tarefa inteira, com prazo absoluto. Pega o modelo que não
para de falar e também o que para de falar perto do fim: no prazo, um relógio à parte
corta a conexão, mesmo com a leitura parada esperando o próximo pedaço.
Fechar a conexão faz o Ollama interromper a geração.
"""
import json
import socket
import threading
import time
from collections.abc import Callable

import httpx


class Silencio(Exception):
    """O modelo parou de responder. Vale tentar de novo."""


class DuracaoEstourada(Exception):
    """A tarefa passou do limite. Tentar de novo só repetiria o estouro."""


def _cortar(resposta: httpx.Response) -> None:
    """Desliga o socket por baixo da leitura: a thread presa no recv acorda na hora."""
    rede = resposta.extensions.get("network_stream")
    sock = rede.get_extra_info("socket") if rede is not None else None
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    resposta.close()


def gerar_ollama(url: str, corpo: dict, *, parcial: Callable[[str], None],
                 limite_s: float, silencio_s: float = 300) -> tuple[str, int, int]:
    inicio, partes = time.monotonic(), []
    espera = min(silencio_s, limite_s)               # nenhum silêncio dura mais que a tarefa
    # read = espera máxima entre dois pedaços
    tempo = httpx.Timeout(10.0, read=espera)
    try:
        with httpx.stream("POST", f"{url}/api/chat", json={**corpo, "stream": True},
                          timeout=tempo) as resposta:
            # o prazo é absoluto, não recomeça a cada pedaço
            restante = limite_s - (time.monotonic() - inicio)
            corte = threading.Timer(restante, _cortar, (resposta,))
            corte.daemon = True
            corte.start()
            try:
                resposta.raise_for_status()
                for linha in resposta.iter_lines():
                    if time.monotonic() - inicio > limite_s:
                        raise DuracaoEstourada(f"passou de {limite_s:.0f} s")
                    if not linha:
                        continue
                    msg = json.loads(linha)
                    pedaco = msg.get("message", {}).get("content", "")
                    if pedaco:
                        partes.append(pedaco)
                        parcial(pedaco)
                    if msg.get("done"):
                        return ("".join(partes), msg.get("prompt_eval_count", 0),
                                msg.get("eval_count", 0))
            finally:
                corte.cancel()
    except (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError,
            httpx.StreamClosed):
        if time.monotonic() - inicio >= limite_s:
            raise DuracaoEstourada(f"passou de {limite_s:.0f} s; conexão cortada")
        raise Silencio(f"nada do modelo em {espera:.0f} s")
    if time.monotonic() - inicio >= limite_s:
        raise DuracaoEstourada(f"passou de {limite_s:.0f} s")
    raise Silencio("o modelo encerrou a resposta sem concluir")


class Parciais:
    """Junta pedaços e publica no máximo um evento por intervalo."""

    def __init__(self, publicar: Callable[[str], None], intervalo_s: float = 1.0):
        self._publicar, self._intervalo = publicar, intervalo_s
        self._buffer, self._ultimo = [], time.monotonic()

    def __call__(self, pedaco: str) -> None:
        self._buffer.append(pedaco)
        if time.monotonic() - self._ultimo >= self._intervalo:
            self.esvaziar()

    def esvaziar(self) -> None:
        if self._buffer:
            self._publicar("".join(self._buffer))
            self._buffer, self._ultimo = [], time.monotonic()
