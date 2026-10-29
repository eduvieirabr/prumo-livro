"""O LLM INTERPRETA: recebe os números prontos e explica. Não calcula.

E o código confere a explicação: todo valor em reais que aparecer na
resposta tem de estar entre os valores que o motor entregou.
"""
import re
from decimal import Decimal

import httpx

_REAIS = re.compile(r"R\$\s*([\d.]+,\d{2})")

INSTRUCOES = """Você explica parcelamentos de impostos para contadores.
Use SOMENTE os valores do bloco <calculo>. Não faça contas.
Se a pergunta exigir um valor que não está no bloco, diga que não está disponível.
Escreva valores no formato R$ 1.234,56."""


def formatar_reais(v: Decimal) -> str:
    sinal = "-" if v < 0 else ""
    inteiro, centavos = f"{abs(v):.2f}".split(".")
    return f"{sinal}R$ {int(inteiro):,}".replace(",", ".") + f",{centavos}"


def montar_pedido(pergunta: str, calculo: dict[str, Decimal], modelo: str,
                  num_ctx: int = 16384) -> dict:
    bloco = "\n".join(f"{nome}: {formatar_reais(v)}" for nome, v in calculo.items())
    return {
        "model": modelo,
        "stream": False,
        "options": {"num_ctx": num_ctx, "temperature": 0},
        "messages": [
            {"role": "system", "content": INSTRUCOES},
            {"role": "user", "content": f"<calculo>\n{bloco}\n</calculo>\n\n{pergunta}"},
        ],
    }


def valores_estranhos(resposta: str, calculo: dict[str, Decimal]) -> set[str]:
    """Valores em reais citados pelo LLM que o motor não entregou."""
    permitidos = {formatar_reais(v) for v in calculo.values()}
    citados = {f"R$ {m}" for m in _REAIS.findall(resposta)}
    return citados - permitidos


class ExplicacaoRecusada(Exception):
    pass


_NUMERO_BR = re.compile(                    # 3.412,87  3412,87  0,36; percentual não é valor em reais
    r"(?<![\d.,])(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2}(?![\d])(?!\s*(?:%|por\s*cento))", re.I)


def conferir_explicacao(resposta: str, calculo: dict[str, Decimal]) -> set[str]:
    """Todo número com centavos na resposta, com ou sem "R$", com ou sem ponto de milhar,
    tem de ser um valor do cálculo. O sinal fica de fora: "-R$ 0,36" cita o valor 0,36."""
    permitidos = {abs(v).quantize(Decimal("0.01")) for v in calculo.values()}
    return {formatar_reais(n) for n in (Decimal(t.replace(".", "").replace(",", "."))
                                       for t in _NUMERO_BR.findall(resposta)) if n not in permitidos}


def explicar(pergunta: str, calculo: dict[str, Decimal], *, url: str,
             modelo: str, timeout: float = 120.0) -> str:
    pedido = montar_pedido(pergunta, calculo, modelo)
    r = httpx.post(f"{url}/api/chat", json=pedido, timeout=timeout)
    r.raise_for_status()
    texto = r.json()["message"]["content"]
    estranhos = conferir_explicacao(texto, calculo)    # a do capítulo 18, por valor
    if estranhos:
        raise ExplicacaoRecusada(
            f"O modelo citou valores fora do cálculo: {sorted(estranhos)}")
    return texto
