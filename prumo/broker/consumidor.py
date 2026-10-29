"""Consumidor do Broker: uma thread por vaga de GPU (capítulos 6 e 7).

Vagas por servidor = OLLAMA_NUM_PARALLEL. Mais threads que vagas só
empurram a fila para dentro do Ollama, onde ninguém enxerga a prioridade.

A vaga é curta e renovada por um batimento enquanto o modelo trabalha.
Quem limita a tarefa é a duração dela, não o prazo da vaga.
"""
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager

import psycopg

from prumo.broker.eventos import (concluir_e_avisar, encerrar_e_avisar, falhar_e_avisar,
                                  publicar)
from prumo.broker.fila import DESCARTAR, pegar_proximo, renovar
from prumo.broker.geracao import DuracaoEstourada, Parciais, Silencio

DURACAO_POR_TAREFA = {"assistente": 180, "extrair_debitos": 1800, "ler_documento": 3600}
DURACAO_PADRAO = 600
TETO_DURACAO = 4 * 3600      # nenhuma tarefa passa disto, configure o que configurar
PRAZO_VAGA_S = 120           # sem batimento por 2 min, o consumidor é dado como morto
BATIMENTO_S = 30
QUARENTENA_S = 120           # depois de um corte, a vaga descansa antes do próximo pedido

# chamar_modelo(tarefa, conteudo, parcial=..., limite_s=...) -> (resultado, entrada, saida)
ChamarModelo = Callable[..., tuple[dict, int, int]]


class VagaPerdida(Exception):
    """O prazo venceu e outro consumidor pegou o pedido."""


def duracao_da(tarefa: str) -> int:
    return min(DURACAO_POR_TAREFA.get(tarefa, DURACAO_PADRAO), TETO_DURACAO)


@contextmanager
def batimento(conn, pedido: dict, prazo_s: int, a_cada_s: float, perdida: threading.Event):
    parar = threading.Event()

    def bater():
        while not parar.wait(a_cada_s):
            try:
                ok = renovar(conn, pedido, prazo_s)
            except Exception:
                ok = False               # sem conseguir renovar, a vaga não é mais garantida
            if not ok:
                perdida.set()
                return

    t = threading.Thread(target=bater, daemon=True)
    t.start()
    try:
        yield
    finally:
        parar.set()
        t.join()


def processar_um(conn, servidor: str, chamar_modelo: ChamarModelo,
                 prazo_s: int = PRAZO_VAGA_S) -> bool:
    pedido = pegar_proximo(conn, servidor, prazo_s)
    if pedido is None:
        return False
    inicio, perdida = time.monotonic(), threading.Event()
    parciais = Parciais(lambda texto: publicar(conn, pedido["id"], "parcial",
                                               {"texto": texto, "tentativa": pedido["tentativa"]}))

    def parcial(pedaco: str) -> None:
        if perdida.is_set():
            raise VagaPerdida()          # para de gerar: o resultado iria para o lixo
        parciais(pedaco)

    try:
        with batimento(conn, pedido, prazo_s, BATIMENTO_S, perdida):
            resultado, entrada, saida = chamar_modelo(
                pedido["tarefa"], pedido["conteudo"],
                parcial=parcial, limite_s=duracao_da(pedido["tarefa"]))
    except DuracaoEstourada as erro:
        encerrar_e_avisar(conn, pedido, f"duração estourada: {erro}", _ms(inicio))
        time.sleep(QUARENTENA_S)
        return True
    except VagaPerdida:
        conn.execute(DESCARTAR, {"id": pedido["id"], "ms": _ms(inicio)})
        time.sleep(QUARENTENA_S)
        return True
    except Silencio as erro:             # modelo travado: nova tentativa, servidor descansa
        falhar_e_avisar(conn, pedido, f"silêncio: {erro}", _ms(inicio))
        time.sleep(QUARENTENA_S)
        return True
    except Exception as erro:            # modelo fora, resposta quebrada
        falhar_e_avisar(conn, pedido, f"{type(erro).__name__}: {erro}", _ms(inicio))
        return True
    parciais.esvaziar()
    if not concluir_e_avisar(conn, pedido, resultado, entrada=entrada, saida=saida,
                             ms=_ms(inicio)):
        print(f"vaga de {pedido['id']} vencida: resultado descartado")
    return True


def _ms(inicio: float) -> int:
    return int((time.monotonic() - inicio) * 1000)


def vaga(dsn: str, servidor: str, chamar_modelo: ChamarModelo, parar: threading.Event,
         espera_s: float = 0.5) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        while not parar.is_set():
            if not processar_um(conn, servidor, chamar_modelo):
                parar.wait(espera_s)


def iniciar(dsn: str, servidor: str, vagas: int, chamar_modelo: ChamarModelo) -> threading.Event:
    parar = threading.Event()
    for _ in range(vagas):
        threading.Thread(target=vaga, args=(dsn, servidor, chamar_modelo, parar),
                         daemon=True).start()
    return parar
