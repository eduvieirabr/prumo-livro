"""A IA planeja, o código calcula (capítulo 13).

A IA recebe só o mapa da planilha (abas, colunas, tipo e três exemplos) e
devolve um plano em JSON. O código confere o plano e calcula, em Decimal,
sobre a planilha inteira. Os números que voltam para a IA são os do código.
"""
import csv
import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

OPERACOES = ("soma", "media", "contagem", "minimo", "maximo", "distintos", "listar")
FILTROS = ("=", "!=", ">", "<", ">=", "<=", "contem")
AGRUPAMENTOS = ("valor", "mes", "ano")
MAX_CALCULOS = 8
MAX_LISTAR = 50


# ---------- ler e entender a planilha ----------

def ler(caminho: str) -> dict[str, list[list[str]]]:
    """Abas -> linhas (a primeira é o cabeçalho), tudo como texto."""
    p = Path(caminho)
    if p.suffix.lower() == ".csv":
        texto = p.read_bytes().decode("utf-8-sig", errors="replace")
        cabecalho = texto.split("\n", 1)[0]               # decide pelo cabeçalho, não pelo arquivo:
        separador = ";" if cabecalho.count(";") > cabecalho.count(",") else ","   # histórico tem vírgula
        return {p.stem: [linha for linha in csv.reader(texto.splitlines(), delimiter=separador)]}
    import openpyxl
    livro = openpyxl.load_workbook(p, read_only=True, data_only=True)
    abas = {}
    for aba in livro.worksheets:
        abas[aba.title] = [["" if c is None else _texto(c) for c in linha]
                           for linha in aba.iter_rows(values_only=True)]
    return abas


def _texto(valor) -> str:
    if isinstance(valor, (datetime, date)):
        return valor.strftime("%d/%m/%Y")
    if isinstance(valor, float):                     # número de verdade sai com vírgula decimal:
        d = Decimal(repr(round(valor, 10)))          # 1.234 do Excel não pode virar "1.234" (milhar)
        return format(d, "f").replace(".", ",")      # e 0,1 + 0,2 não pode virar 0,30000000000000004
    return str(valor)


def numero_br(texto: str) -> Decimal | None:
    """Número do jeito que contador escreve. None quando não é número.

    "1.500" = mil e quinhentos; "(500,00)" e "1.000,00 D" são negativos; "C" é positivo.
    """
    t = texto.strip().replace("R$", "").replace("\xa0", "").strip()   # \xa0: espaço do Excel
    if not t:
        return None
    # um sinal só; dois = não adivinha
    sinais = []
    if t.startswith("(") and t.endswith(")"):
        sinais.append(True); t = t[1:-1].strip()
    m = re.fullmatch(r"(.*?)\s*([DdCc])", t)
    if m:
        sinais.append(m.group(2) in "Dd"); t = m.group(1).strip()
    if t.startswith("-"):
        sinais.append(True); t = t[1:].strip()
    if len(sinais) > 1:
        return None
    negativo = bool(sinais) and sinais[0]
    if re.fullmatch(r"([0-9]{1,3}(\.[0-9]{3})+|[0-9]+),[0-9]+", t):      # 1.500,00  1500,5
        t = t.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"[0-9]{1,3}(\.[0-9]{3})+", t):                    # 1.500 = milhar
        t = t.replace(".", "")
    elif not re.fullmatch(r"[0-9]+(\.[0-9]{1,2})?", t):                  # 1500 ou 1500.50
        # "1,500.50" não adivinha
        return None
    valor = Decimal(t)
    return -valor if negativo else valor


def data_br(texto: str) -> date | None:
    for formato in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y"):
        try:
            return datetime.strptime(texto.strip()[:10], formato).date()
        except ValueError:
            pass
    return None


def _tipo(valores: list[str]) -> str:
    cheios = [v for v in valores if v.strip()]
    if not cheios:
        return "vazio"
    if sum(numero_br(v) is not None for v in cheios) >= 0.8 * len(cheios):
        return "numero"
    if sum(data_br(v) is not None for v in cheios) >= 0.8 * len(cheios):
        return "data"
    return "texto"


def mapa(abas: dict[str, list[list[str]]]) -> dict:
    """O que a IA vê: estrutura e três exemplos por coluna. Nunca a planilha."""
    resultado = {}
    for nome, linhas in abas.items():
        if not linhas:
            continue
        cabecalho, corpo = linhas[0], linhas[1:]
        colunas = []
        for i, coluna in enumerate(cabecalho):
            valores = [l[i] if i < len(l) else "" for l in corpo]
            exemplos = [v for v in valores if v.strip()][:3]
            colunas.append({"nome": coluna, "tipo": _tipo(valores[:200]),
                            "exemplos": exemplos})
        resultado[nome] = {"linhas": len(corpo), "colunas": colunas}
    return resultado


PEDIR_PLANO = """Você planeja cálculos sobre uma planilha. Você NÃO vê a planilha, só o mapa.
Responda SOMENTE com JSON: {"calculos": [{"nome": "...", "aba": "...",
 "operacao": "soma|media|contagem|minimo|maximo|distintos|listar", "coluna": "...",
 "filtros": [{"coluna": "...", "op": "=|!=|>|<|>=|<=|contem", "valor": "..."}],
 "agrupar_por": {"coluna": "...", "por": "valor|mes|ano"}}]}
No máximo 8 cálculos. Use só abas e colunas do mapa. Não calcule nada."""


# ---------- conferir o plano ----------

def conferir_plano(plano: dict, m: dict) -> list[str]:
    erros = []
    calculos = plano.get("calculos")
    if not isinstance(calculos, list) or not calculos:
        return ["plano sem cálculos"]
    if len(calculos) > MAX_CALCULOS:
        erros.append(f"mais de {MAX_CALCULOS} cálculos")
    for i, c in enumerate(calculos[:MAX_CALCULOS]):
        if not isinstance(c, dict):
            erros.append(f"cálculo {i + 1}: não é um objeto")
            continue
        nome = c.get("nome") or f"cálculo {i + 1}"
        filtros, agr = c.get("filtros", []), c.get("agrupar_por")
        if not isinstance(filtros, list) or not all(isinstance(f, dict) for f in filtros):
            erros.append(f"{nome}: filtros precisam ser uma lista de objetos")
            continue
        if agr is not None and not isinstance(agr, dict):
            erros.append(f"{nome}: agrupar_por precisa ser um objeto")
            continue
        aba = m.get(c.get("aba")) if isinstance(c.get("aba"), str) else None
        if aba is None:
            erros.append(f"{nome}: aba {c.get('aba')!r} não existe")
            continue
        nomes = {col["nome"] for col in aba["colunas"]}
        if c.get("operacao") not in OPERACOES:
            erros.append(f"{nome}: operação {c.get('operacao')!r} não existe")
        erros += [f"{nome}: filtro {f.get('op')!r} não existe" for f in filtros
                  if f.get("op", "=") not in FILTROS]
        if agr and agr.get("por", "valor") not in AGRUPAMENTOS:
            erros.append(f"{nome}: agrupamento {agr.get('por')!r} não existe")
        usadas = [c.get("coluna")] + [f.get("coluna") for f in filtros] + ([agr.get("coluna")] if agr else [])
        for col in usadas:
            if col is not None and (not isinstance(col, str) or col not in nomes):
                erros.append(f"{nome}: coluna {col!r} não existe")
    return erros


class PlanoInvalido(Exception):
    pass


def planejar(pergunta: str, m: dict, llm) -> dict:
    """Pede o plano à IA local (tarefa planejar_calculo), com uma chance de corrigir."""
    # célula não fecha marca
    mapa_json = json.dumps(m, ensure_ascii=False).replace("<", "\\u003c")
    pedido = (f"{PEDIR_PLANO}\n\n<mapa>\n{mapa_json}\n</mapa>\n\n"
              f"<pergunta>\n{pergunta}\n</pergunta>")
    erros: list[str] = []
    for _ in range(2):
        bruto = llm(pedido if not erros else
                    f"{pedido}\n\nO plano anterior tinha erros: {'; '.join(erros)}. Corrija.")
        try:
            plano = json.loads(bruto)
        except ValueError:
            erros = ["a resposta não era JSON"]
            continue
        if not isinstance(plano, dict):
            erros = ["o plano não é um objeto"]
            continue
        erros = conferir_plano(plano, m)
        if not erros:
            return plano
    raise PlanoInvalido("; ".join(erros))


# ---------- calcular ----------

def _passa(valor: str, f: dict) -> bool:
    op, alvo = f.get("op", "="), str(f.get("valor", ""))
    if op == "contem":
        return alvo.lower() in valor.lower()
    a, b = numero_br(valor), numero_br(alvo)
    if b is None:
        a, b = data_br(valor), data_br(alvo)       # datas comparam como datas, não como texto
        if b is None:
            a, b = valor.strip().lower(), alvo.strip().lower()
    if a is None:                                  # filtro de número ou data: célula que não é
        return False                               # ("", "ver anexo") não passa em comparação nenhuma
    return {"=": a == b, "!=": a != b, ">": a > b, "<": a < b,
            ">=": a >= b, "<=": a <= b}.get(op, False)


def _grupo(valor: str, por: str) -> str:
    if por in ("mes", "ano"):
        d = data_br(valor)
        if d is None:
            return "(sem data)"
        return f"{d.month:02d}/{d.year}" if por == "mes" else str(d.year)
    return valor.strip() or "(vazio)"


def _aplicar(op: str, valores: list[str]) -> tuple[object, int]:
    """Devolve (resultado, células ignoradas)."""
    if op == "contagem":
        return len(valores), 0
    if op == "distintos":
        return len({v.strip() for v in valores if v.strip()}), 0
    if op == "listar":
        return valores[:MAX_LISTAR], 0
    numeros = [numero_br(v) for v in valores]
    validos = [n for n in numeros if n is not None]
    ignoradas = sum(1 for v, n in zip(valores, numeros) if n is None and v.strip())
    if not validos:
        return None, ignoradas
    soma = sum(validos, Decimal("0"))
    resultado = {"soma": soma, "minimo": min(validos), "maximo": max(validos),
                 "media": (soma / len(validos)).quantize(Decimal("0.01"))}[op]
    return resultado, ignoradas


def calcular(plano: dict, abas: dict[str, list[list[str]]]) -> list[dict]:
    """Um resultado por cálculo, com o que foi filtrado e o que ficou de fora."""
    saida = []
    for c in plano["calculos"][:MAX_CALCULOS]:
        try:
            linhas = abas[c["aba"]]
            cab = linhas[0]
            idx = {nome: i for i, nome in enumerate(cab)}
            corpo = [l + [""] * (len(cab) - len(l)) for l in linhas[1:]]
            for f in c.get("filtros", []):
                corpo = [l for l in corpo if _passa(l[idx[f["coluna"]]], f)]
            coluna = c.get("coluna") or cab[0]
            grupos: dict[str, list[str]] = {}
            agr = c.get("agrupar_por")
            for l in corpo:
                chave = _grupo(l[idx[agr["coluna"]]], agr.get("por", "valor")) if agr else "total"
                grupos.setdefault(chave, []).append(l[idx[coluna]])
            resultados, ignoradas = {}, 0
            for chave, valores in sorted(grupos.items()):
                resultados[chave], ign = _aplicar(c["operacao"], valores)
                ignoradas += ign
            saida.append({"nome": c["nome"], "linhas_usadas": len(corpo), "resultado": resultados,
                          "celulas_ignoradas": ignoradas})
        except (KeyError, IndexError, TypeError) as e:
            saida.append({"nome": c.get("nome"), "erro": f"não foi possível calcular: {e}"})
    return saida


def para_a_ia(resultados: list[dict]) -> str:
    """O bloco <calculos> que vai junto com a pergunta. A IA explica; não refaz."""
    def texto(v):
        if isinstance(v, Decimal):
            return f"{v:.2f}".replace(".", ",")
        return v
    limpo = [{k: ({g: texto(x) for g, x in v.items()} if k == "resultado" else v)
              for k, v in r.items()} for r in resultados]
    corpo = json.dumps(limpo, ensure_ascii=False, indent=1).replace("<", "\\u003c")
    return "<calculos>\n" + corpo + "\n</calculos>"
