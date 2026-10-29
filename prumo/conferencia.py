"""A segunda camada de conferência, achada pelo modelo sabotador (capítulo 18).

A primeira camada (capítulos 1 e 4) confere valores em reais. O sabotador
mostrou o que ela deixava passar: datas, competências, número de parcelas e
processo inventados na leitura; valores sem "R$" ou sem ponto de milhar na explicação.
"""
import re

from prumo.ia.explicar import conferir_explicacao  # noqa: F401  (mora no capítulo 1)
from prumo.leitura.extrair import Leitura, ler, recibo_de_json, valores_inventados
from prumo.leitura.recibo import Recibo



def campos_inventados(recibo: Recibo, texto: str) -> list[str]:
    """Os campos que não são dinheiro também têm de estar no documento."""
    procurados = [("processo", recibo.processo),
                  ("data de consolidação", recibo.data_consolidacao.strftime("%d/%m/%Y")),
                  ("parcelas", str(recibo.parcelas))]
    procurados += [(f"competência do {d.tributo}", d.competencia) for d in recibo.debitos]
    return [f"{nome} {valor} não aparece no documento" for nome, valor in procurados
            if not re.search(rf"(?<![\w/.]){re.escape(valor)}(?![\w/])", texto)]


def ler_conferido(texto: str, llm=None) -> Leitura:
    leitura = ler(texto, llm)
    if leitura.origem == "llm" and leitura.recibo is not None:
        leitura.problemas += campos_inventados(leitura.recibo, texto)
    return leitura



def conferir_resposta(texto: str, bruto: str) -> Leitura:
    """A resposta do modelo pela conferência da produção, sem passar pelos parsers.

    É o que a avaliação do capítulo 18 precisa: medir o modelo, e não o parser.
    """
    try:
        recibo = recibo_de_json(bruto)
    except (ValueError, KeyError, TypeError) as erro:
        return Leitura(None, "llm", [f"Resposta da IA fora do formato: {erro}"])
    problemas = recibo.conferir()
    problemas += [f"Valor {v} não aparece no documento"
                  for v in valores_inventados(recibo, texto)]
    return Leitura(recibo, "llm", problemas + campos_inventados(recibo, texto))
