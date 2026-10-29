"""O modelo sabotador: respostas erradas de propósito, para medir as conferências (capítulo 18).

Não mede o modelo. Mede o código que confere o modelo: para cada jeito
conhecido de errar, a conferência pegou ou deixou passar?
"""
import copy
import json
from datetime import date, timedelta


def _trocar_digitos(valor: str) -> str:
    """Troca os dois primeiros algarismos vizinhos diferentes: 98420.15 -> 89420.15."""
    for i in range(len(valor) - 1):
        a, b = valor[i], valor[i + 1]
        if a.isdigit() and b.isdigit() and a != b:
            return valor[:i] + b + a + valor[i + 2:]
    return valor + "1"


def _mes_seguinte(competencia: str) -> str:
    mes, ano = int(competencia[:2]), int(competencia[3:])
    return f"{mes % 12 + 1:02d}/{ano + (mes == 12)}"


def _somar(valor: str, centavos: int) -> str:
    inteiro, frac = valor.split(".")
    total = int(inteiro) * 100 + int(frac) + centavos
    return f"{total // 100}.{total % 100:02d}"


def sabotagens_da_leitura(certa: dict) -> dict[str, str]:
    """Nome do erro -> resposta da IA com aquele erro."""
    def com(mudar):
        r = copy.deepcopy(certa)
        mudar(r)
        return json.dumps(r)
    return {
        "dígitos trocados num débito": com(lambda r: r["debitos"][0].update(
            valor=_trocar_digitos(r["debitos"][0]["valor"]))),
        "débito omitido": com(lambda r: r["debitos"].pop()),
        "débito duplicado": com(lambda r: r["debitos"].append(r["debitos"][0])),
        "débito e total errados juntos": com(lambda r: (
            r["debitos"][0].update(valor=_somar(r["debitos"][0]["valor"], 900)),
            r.update(total_consolidado=_somar(r["total_consolidado"], 900)))),
        "competência do mês seguinte": com(lambda r: r["debitos"][0].update(
            competencia=_mes_seguinte(r["debitos"][0]["competencia"]))),
        "data de consolidação um dia depois": com(lambda r: r.update(data_consolidacao=(
            date.fromisoformat(r["data_consolidacao"]) + timedelta(days=1)).isoformat())),
        "número de parcelas trocado": com(lambda r: r.update(parcelas=r["parcelas"] + 6)),
        "processo trocado": com(lambda r: r.update(processo=_trocar_digitos(r["processo"]))),
        "JSON cortado no meio": json.dumps(certa)[:60],
        "campo faltando": com(lambda r: r.pop("total_consolidado")),
    }


def sabotagens_da_explicacao(valor: str) -> dict[str, str]:
    """valor no formato 3.412,87 (sem R$). Cada resposta cita o valor de um jeito errado."""
    errado = _trocar_digitos(valor)
    return {
        "dígitos trocados, com R$": f"A parcela 14 ficou em R$ {errado}.",
        "valor inventado, com R$": "A parcela 14 ficou em R$ 3.500,00.",
        "dígitos trocados, sem R$": f"A parcela 14 ficou em {errado}.",
        "valor colado, sem pontos": f"A parcela 14 ficou em R${errado.replace('.', '')}.",
        "sem pontos e sem R$": f"A parcela 14 ficou em {errado.replace('.', '')} reais.",
        "valor por extenso": "A parcela 14 ficou em três mil e quinhentos reais.",
    }


def sabotagens_do_plano(certo: dict) -> dict[str, dict]:
    """Planos de cálculo errados (capítulo 13). O primeiro cálculo do plano certo é o alvo."""
    def com(mudar):
        r = copy.deepcopy(certo)
        mudar(r["calculos"][0])
        return r
    return {
        "coluna inventada": com(lambda c: c.update(coluna="Saldo")),
        "aba inventada": com(lambda c: c.update(aba="Plan2")),
        "operação inventada": com(lambda c: c.update(operacao="regressao")),
        "filtro com operador inventado": com(lambda c: c["filtros"][0].update(op="==")),
        "plano mal formado": com(lambda c: c.update(filtros={"coluna": "Situação"})),
        "filtro com o valor errado": com(lambda c: c["filtros"][0].update(valor="identificado")),
        "contagem no lugar de soma": com(lambda c: c.update(operacao="contagem")),
    }


def avaliar(sabotagens: dict[str, str], pegou) -> dict[str, bool]:
    """pegou(resposta) -> True se a conferência recusou. Exceção conta como recusa."""
    resultado = {}
    for nome, resposta in sabotagens.items():
        try:
            resultado[nome] = bool(pegou(resposta))
        except Exception:
            resultado[nome] = True
    return resultado


# ---------- a avaliação do modelo real, com gabarito ----------

def identidade_do_modelo(tags: dict, nome: str, *, completo: bool = False) -> str:
    """O nome não basta: `qwen3:14b` pode apontar para outro arquivo depois de um pull.

    `tags` é a resposta de GET /api/tags do Ollama. O digest identifica o arquivo:
    abreviado para ler, completo para o registro da avaliação.
    """
    for modelo in tags["models"]:
        if modelo["name"] == nome:
            digest = modelo["digest"] if completo else modelo["digest"][:12]
            return f"{nome}@{digest}"
    raise LookupError(f"{nome} não está instalado neste servidor")


def classificar(leitura, gabarito) -> str:
    """Uma rodada: 'certa', 'recusada' ou 'errada' (aprovada, mas fora do gabarito)."""
    if not leitura.aprovada:
        return "recusada"
    r = leitura.recibo
    chave = lambda d: (d.tributo, d.competencia, d.valor)
    igual = ((r.processo, r.data_consolidacao, r.parcelas, r.total_consolidado)
             == (gabarito.processo, gabarito.data_consolidacao, gabarito.parcelas,
                 gabarito.total_consolidado)
             and sorted(map(chave, r.debitos)) == sorted(map(chave, gabarito.debitos)))
    return "certa" if igual else "errada"


def limite_superior(casos: int, confianca: float = 0.95) -> float:
    """Zero erros em `casos` rodadas independentes: até onde a taxa real de erro pode ir.

    É a "regra de três": com 95% de confiança, o limite fica perto de 3 / casos.
    """
    return 1 - (1 - confianca) ** (1 / casos)
