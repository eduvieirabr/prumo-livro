"""Quem lê o documento: parser de layout conhecido primeiro, LLM depois (capítulo 4).

Seja quem for, o resultado passa pela mesma conferência. O LLM ainda tem
uma regra a mais: todo valor que ele extrair tem de existir no texto.
"""
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from prumo.ia.explicar import formatar_reais
from prumo.leitura.recibo import Debito, Recibo, ler_recibo

PARSERS = [
    ("recibo-rfb-v1", lambda texto: "RECIBO DE CONSOLIDAÇÃO" in texto, ler_recibo),
]

PROMPT_EXTRACAO = """Extraia do documento abaixo os dados do parcelamento.
Responda SOMENTE com JSON, sem comentários, neste formato:
{"processo": "...", "data_consolidacao": "AAAA-MM-DD", "parcelas": 60,
 "total_consolidado": "178684.20",
 "debitos": [{"tributo": "IRPJ", "competencia": "MM/AAAA", "valor": "98420.15"}]}
Copie os valores do documento. Não some, não arredonde, não invente.

<documento>
"""


@dataclass
class Leitura:
    recibo: Recibo | None
    origem: str
    problemas: list[str] = field(default_factory=list)

    @property
    def aprovada(self) -> bool:
        return self.recibo is not None and not self.problemas


def _decimal(valor) -> Decimal:
    try:
        return Decimal(str(valor))
    except InvalidOperation:
        raise ValueError(f"valor inválido: {valor!r}")


def recibo_de_json(bruto: str) -> Recibo:
    dados = json.loads(bruto)
    return Recibo(
        processo=str(dados["processo"]),
        data_consolidacao=date.fromisoformat(dados["data_consolidacao"]),
        parcelas=int(dados["parcelas"]),
        total_consolidado=_decimal(dados["total_consolidado"]),
        debitos=[Debito(d["tributo"], d["competencia"], _decimal(d["valor"]))
                 for d in dados["debitos"]],
    )


def aparece(valor: str, texto: str) -> bool:
    """O valor está no texto como número inteiro, não como pedaço de outro."""
    return re.search(rf"(?<![\d.,]){re.escape(valor)}(?![\d]|[.,]\d)", texto) is not None


def valores_inventados(recibo: Recibo, texto: str) -> list[str]:
    """Valores extraídos que não aparecem no documento, no formato brasileiro."""
    valores = [recibo.total_consolidado] + [d.valor for d in recibo.debitos]
    return [v for v in (formatar_reais(x).removeprefix("R$ ") for x in valores)
            if not aparece(v, texto)]


def ler(texto: str, llm: Callable[[str], str] | None = None) -> Leitura:
    avisos = []
    for nome, reconhece, parser in PARSERS:
        if reconhece(texto):
            try:
                recibo = parser(texto)
                return Leitura(recibo, nome, recibo.conferir())
            except ValueError as erro:       # parece o layout, mas mudou
                avisos.append(f"{nome} não leu: {erro}")
    if llm is None:
        return Leitura(None, "desconhecido",
                       avisos + ["Layout desconhecido e IA indisponível"])
    try:
        bruto = llm(PROMPT_EXTRACAO + texto + "\n</documento>")
    except Exception as erro:                # IA fora do ar, tempo esgotado
        return Leitura(None, "desconhecido", avisos + [f"IA indisponível: {erro}"])
    try:
        recibo = recibo_de_json(bruto)
    except (ValueError, KeyError, TypeError) as erro:
        return Leitura(None, "llm", avisos + [f"Resposta da IA fora do formato: {erro}"])
    problemas = recibo.conferir()
    problemas += [f"Valor {v} não aparece no documento"
                  for v in valores_inventados(recibo, texto)]
    return Leitura(recibo, "llm", problemas)
