"""Formulário de entrada da skill (capítulo 12).

O dono da skill declara os campos; quem usa preenche; o servidor confere e
normaliza. Os dados chegam à IA prontos, e a skill roda sem adivinhação.
"""
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from prumo.ia.explicar import formatar_reais

MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
MESES_POR_EXTENSO = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
                     "agosto", "setembro", "outubro", "novembro", "dezembro"]


@dataclass(frozen=True)
class Campo:
    nome: str
    tipo: str                  # texto, numero, valor, data, competencia, cnpj, lista, simnao, arquivo
    obrigatorio: bool = True
    opcoes: tuple[str, ...] = ()


def dv_cnpj(base12: str) -> str:
    """Dígitos verificadores do CNPJ, numérico ou alfanumérico (valor = código ASCII − 48)."""
    def digito(parte: str, pesos: list[int]) -> str:
        soma = sum((ord(c) - 48) * p for c, p in zip(parte, pesos))
        resto = soma % 11
        return "0" if resto < 2 else str(11 - resto)
    d1 = digito(base12, [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    d2 = digito(base12 + d1, [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    return d1 + d2


def cnpj(valor: str) -> str:
    bruto = re.sub(r"[./\-\s]", "", valor.upper())
    if not re.fullmatch(r"[0-9A-Z]{12}\d{2}", bruto) or len(set(bruto)) == 1:
        raise ValueError("CNPJ em formato inválido")
    if dv_cnpj(bruto[:12]) != bruto[12:]:
        raise ValueError("CNPJ com dígito verificador errado")
    return f"{bruto[:2]}.{bruto[2:5]}.{bruto[5:8]}/{bruto[8:12]}-{bruto[12:]}"


def decimal_br(valor: str) -> Decimal:
    """Número escrito do jeito brasileiro. "1.500" é mil e quinhentos; "1,500.50" é recusado."""
    t = valor.replace("R$", "").replace(" ", "").strip()
    negativo = t.startswith("-")
    t = t.lstrip("-")
    if re.fullmatch(r"(\d{1,3}(\.\d{3})+|\d+)(,\d+)?", t):        # 1.500,50  1500,5  1.500
        numero = Decimal(t.replace(".", "").replace(",", "."))
    elif re.fullmatch(r"\d+\.\d{1,2}", t):                          # 1500.50  1.5 (ponto decimal)
        numero = Decimal(t)
    else:
        raise ValueError("número inválido; use 1.500,50")
    return -numero if negativo else numero


def valor_reais(valor: str) -> str:
    return formatar_reais(decimal_br(valor).quantize(Decimal("0.01")))


def competencia(valor: str) -> str:
    v = valor.strip().lower()
    m = re.fullmatch(r"(\d{1,2}|[a-zç]+)/(\d{4})", v)
    if not m:
        raise ValueError("competência inválida; use mm/aaaa")
    mes_texto, ano = m.groups()
    if mes_texto.isdigit():
        mes = int(mes_texto)
    elif mes_texto in MESES:
        mes = MESES.index(mes_texto) + 1
    elif mes_texto in MESES_POR_EXTENSO:
        mes = MESES_POR_EXTENSO.index(mes_texto) + 1
    else:
        mes = 0
    if not 1 <= mes <= 12 or not 1990 <= int(ano) <= 2100:
        raise ValueError("competência inválida")
    return f"{mes:02d}/{ano}"


def data_br(valor: str) -> str:
    try:
        return datetime.strptime(valor.strip(), "%d/%m/%Y").date().isoformat()
    except ValueError:
        return date.fromisoformat(valor.strip()).isoformat()


NORMALIZAR = {
    "texto": lambda v: v.strip(),
    "numero": lambda v: str(decimal_br(v)),
    "valor": valor_reais,
    "data": data_br,
    "competencia": competencia,
    "cnpj": cnpj,
    "simnao": lambda v: {"sim": True, "não": False, "nao": False}[v.strip().lower()],
}


def validar(campos: list[Campo], valores: dict) -> tuple[dict, dict]:
    """Devolve (dados normalizados, erros por campo). Erro = a skill não roda."""
    dados, erros = {}, {}
    for c in campos:
        bruto = valores.get(c.nome)
        if bruto in (None, "", []):
            if c.obrigatorio:
                erros[c.nome] = "obrigatório"
            continue
        try:
            if c.tipo == "lista":
                if bruto not in c.opcoes:
                    raise ValueError(f"escolha uma de: {', '.join(c.opcoes)}")
                dados[c.nome] = bruto
            elif c.tipo == "arquivo":
                dados[c.nome] = {"anexo": str(bruto)}     # o arquivo vai como anexo marcado
            else:
                dados[c.nome] = NORMALIZAR[c.tipo](str(bruto))
        except (ValueError, KeyError, InvalidOperation) as e:
            erros[c.nome] = str(e) or "inválido"
    return dados, erros
