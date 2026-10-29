"""Parser determinístico do recibo de consolidação (layout fictício do livro).

O LLM só entra quando o layout é desconhecido (capítulo 4). Mesmo assim, o
resultado passa pela mesma conferência: soma dos débitos = total consolidado.
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal


def valor_br(texto: str) -> Decimal:
    """'178.684,20' -> Decimal('178684.20')"""
    return Decimal(texto.strip().replace(".", "").replace(",", "."))


@dataclass
class Debito:
    tributo: str
    competencia: str
    valor: Decimal


@dataclass
class Recibo:
    processo: str
    data_consolidacao: date
    parcelas: int
    total_consolidado: Decimal
    debitos: list[Debito] = field(default_factory=list)

    def conferir(self) -> list[str]:
        """Devolve a lista de problemas; vazia quando o recibo fecha."""
        problemas = []
        soma = sum((d.valor for d in self.debitos), Decimal("0"))
        if soma != self.total_consolidado:
            problemas.append(
                f"Soma dos débitos ({soma}) diferente do total "
                f"consolidado ({self.total_consolidado})")
        if not self.debitos:
            problemas.append("Nenhum débito lido")
        return problemas


_CAMPOS = {
    "processo": r"Processo:\s*(\S+)",
    "data": r"Data da consolidação:\s*(\d{2}/\d{2}/\d{4})",
    "parcelas": r"Quantidade de parcelas:\s*(\d+)",
    "total": r"TOTAL CONSOLIDADO:\s*([\d.]+,\d{2})",
}
_DEBITO = re.compile(r"^\s*(IRPJ|CSLL|PIS|COFINS|IPI)\s+(\d{2}/\d{4})\s+([\d.]+,\d{2})\s*$",
                     re.MULTILINE)


def ler_recibo(texto: str) -> Recibo:
    achados = {}
    for campo, padrao in _CAMPOS.items():
        m = re.search(padrao, texto)
        if not m:
            raise ValueError(f"Campo '{campo}' não encontrado: layout desconhecido")
        achados[campo] = m.group(1)
    return Recibo(
        processo=achados["processo"],
        data_consolidacao=datetime.strptime(achados["data"], "%d/%m/%Y").date(),
        parcelas=int(achados["parcelas"]),
        total_consolidado=valor_br(achados["total"]),
        debitos=[Debito(t, c, valor_br(v)) for t, c, v in _DEBITO.findall(texto)],
    )
