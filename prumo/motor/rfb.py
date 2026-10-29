"""Motor de cálculo das parcelas pelo método da RFB (Selic acumulada + 1%).

Código determinístico: recebe números, devolve números. Não fala com banco,
com a web nem com o LLM. É a camada que DECIDE (capítulo 1).
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

CENTAVO = Decimal("0.01")
CEM = Decimal("100")
UM_POR_CENTO = Decimal("1")

Competencia = tuple[int, int]  # (ano, mês)


def competencia(d: date) -> Competencia:
    return (d.year, d.month)


def somar_meses(c: Competencia, n: int) -> Competencia:
    total = c[0] * 12 + (c[1] - 1) + n
    return (total // 12, total % 12 + 1)


def selic_acumulada(selic: dict[Competencia, Decimal],
                    inicio: Competencia, fim: Competencia) -> Decimal:
    """Soma das taxas mensais (em %) de inicio a fim, inclusive."""
    soma, atual = Decimal("0"), inicio
    while atual <= fim:
        if atual not in selic:
            raise KeyError(f"Selic de {atual[1]:02d}/{atual[0]} não cadastrada")
        soma += selic[atual]
        atual = somar_meses(atual, 1)
    return soma


@dataclass(frozen=True)
class Parcelamento:
    consolidado: Decimal
    entrada: Decimal
    parcelas: int
    data_consolidacao: date

    @property
    def parcela_base(self) -> Decimal:
        bruto = (self.consolidado - self.entrada) / self.parcelas
        return bruto.quantize(CENTAVO, ROUND_HALF_UP)


def valor_parcela(p: Parcelamento, pagamento: date,
                  selic: dict[Competencia, Decimal]) -> Decimal:
    """Parcela-base × (1 + Selic acumulada + 1%).

    Selic do mês seguinte ao da consolidação até o mês anterior ao do
    pagamento; mais 1% referente ao mês do pagamento.
    """
    inicio = somar_meses(competencia(p.data_consolidacao), 1)
    fim = somar_meses(competencia(pagamento), -1)
    acumulada = selic_acumulada(selic, inicio, fim) if inicio <= fim else Decimal("0")
    fator = 1 + (acumulada + UM_POR_CENTO) / CEM
    return (p.parcela_base * fator).quantize(CENTAVO, ROUND_HALF_UP)
