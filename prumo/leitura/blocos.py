"""Documentos longos: a leitura por blocos de páginas (capítulo 4).

Quando o documento não cabe na janela, ele é lido em blocos de páginas inteiras.
Cada débito volta com a página de onde saiu; o código confere a página, o valor e a
competência naquela página, consolida os blocos e, no fim, faz a conferência de sempre.
Bloco que falha é refeito sozinho, sem repetir os que deram certo.
"""
import json
import re
from dataclasses import dataclass, field
from datetime import date

from prumo.broker.pagos import tokens_estimados
from prumo.conferencia import campos_inventados
from prumo.leitura.extrair import _decimal, aparece
from prumo.ia.explicar import formatar_reais
from prumo.leitura.recibo import Debito, Recibo

JANELA = 16_384
RESPOSTA = 4_000                       # tokens reservados para o JSON que volta
INSTRUCOES = 600                       # o prompt fixo, por cima
ORCAMENTO = JANELA - RESPOSTA - INSTRUCOES

PROMPT_BLOCO = """Extraia os débitos das páginas abaixo. Responda SOMENTE com JSON:
{"debitos": [{"pagina": 3, "tributo": "IRPJ", "competencia": "MM/AAAA", "valor": "98420.15"}]}
Uma entrada por linha de débito, com a página onde ela está. Não inclua totais nem subtotais.
Copie os valores. Não some, não arredonde, não invente.

<paginas>
"""

PROMPT_CABECALHO = """Do documento abaixo, extraia SOMENTE o cabeçalho, em JSON:
{"processo": "...", "data_consolidacao": "AAAA-MM-DD", "parcelas": 60, "total_consolidado": "178684.20"}

<documento>
"""


TOKENS_POR_DEBITO = 40                 # um objeto do JSON de resposta, contado por cima
_VALOR = re.compile(r"\d,\d{2}(?!\d)")


def saida_estimada(texto: str) -> int:
    """A resposta também tem de caber: cada valor da página pode virar um débito."""
    return len(_VALOR.findall(texto)) * TOKENS_POR_DEBITO


def dividir(paginas: list[str], orcamento: int = ORCAMENTO,
            resposta: int = RESPOSTA) -> list[tuple[int, int]]:
    """Blocos de páginas inteiras, em ordem; a entrada e a resposta de cada um cabem."""
    blocos, inicio, entrada, saida = [], 0, 0, 0
    for i, texto in enumerate(paginas):
        e, s = tokens_estimados(texto), saida_estimada(texto)
        if e > orcamento or s > resposta:
            raise ValueError(f"a página {i + 1} sozinha não cabe num bloco")
        if (entrada + e > orcamento or saida + s > resposta) and i > inicio:
            blocos.append((inicio, i))
            inicio, entrada, saida = i, 0, 0
        entrada, saida = entrada + e, saida + s
    blocos.append((inicio, len(paginas)))
    return blocos


def _marcado(paginas: list[str], inicio: int, fim: int) -> str:
    return "\n".join(f"=== página {n + 1} ===\n{paginas[n]}" for n in range(inicio, fim))


@dataclass
class LeituraLonga:
    recibo: Recibo | None
    problemas: list[str] = field(default_factory=list)
    falhos: list[tuple[int, int]] = field(default_factory=list)   # blocos para refazer
    debitos_por_bloco: dict = field(default_factory=dict)

    @property
    def aprovada(self) -> bool:
        return self.recibo is not None and not self.problemas and not self.falhos


def _ler_bloco(paginas, inicio, fim, llm, aviso: str = "") -> tuple[list, list[str]]:
    pedido = PROMPT_BLOCO + _marcado(paginas, inicio, fim) + "\n</paginas>"
    # o motivo da recusa muda o pedido: nem o modelo nem o Broker repetem a resposta
    if aviso:
        pedido += f"\n\nLeitura anterior recusada: {aviso}. Confira e refaça."
    resposta = llm(pedido)
    dados = json.loads(resposta)
    lidos, problemas, usados = [], [], {}
    for d in dados["debitos"]:
        pagina = int(d["pagina"])
        deb = Debito(str(d["tributo"]), str(d["competencia"]), _decimal(d["valor"]))
        if not inicio < pagina <= fim:
            problemas.append(f"página {pagina} fora do bloco {inicio + 1}–{fim}")
            continue
        texto, valor = paginas[pagina - 1], formatar_reais(deb.valor).removeprefix("R$ ")
        if not aparece(valor, texto) or deb.competencia not in texto:
            problemas.append(f"página {pagina}: {deb.competencia} {valor} não está nela")
            continue
        usados[(pagina, valor)] = usados.get((pagina, valor), 0) + 1
        no_texto = len(re.findall(rf"(?<![\d.,]){re.escape(valor)}(?![\d])", texto))
        if usados[(pagina, valor)] > no_texto:
            problemas.append(f"página {pagina}: {valor} lido mais vezes do que aparece")
            continue
        lidos.append((pagina, deb))
    return lidos, problemas


def ler_longo(paginas: list[str], llm, *, orcamento: int = ORCAMENTO, tentativas: int = 2,
              anterior: LeituraLonga | None = None) -> LeituraLonga:
    """Lê bloco por bloco. Com `anterior`, refaz só os blocos que falharam nela."""
    blocos = anterior.falhos if anterior else dividir(paginas, orcamento)
    por_bloco = dict(anterior.debitos_por_bloco) if anterior else {}
    problemas, falhos = [], []
    for inicio, fim in blocos:
        aviso = "; ".join(anterior.problemas[:3]) if anterior else ""
        for _ in range(tentativas):
            try:
                lidos, ruins = _ler_bloco(paginas, inicio, fim, llm, aviso)
            except (ValueError, KeyError, TypeError) as erro:
                lidos, ruins = None, [f"páginas {inicio + 1}–{fim}: fora do formato ({erro})"]
            if lidos is not None and not ruins:
                por_bloco[(inicio, fim)] = lidos
                break
            aviso = "; ".join(ruins[:3])
        else:
            falhos.append((inicio, fim))
            problemas += ruins
    leitura = LeituraLonga(None, problemas, falhos, por_bloco)
    if not falhos:
        _fechar(leitura, paginas, llm)
    return leitura


def _fechar(leitura: LeituraLonga, paginas: list[str], llm) -> None:
    """Cabeçalho, soma dos blocos e a segunda camada do capítulo 18 sobre o documento inteiro."""
    por_bloco = leitura.debitos_por_bloco
    debitos = [d for b in sorted(por_bloco) for _, d in sorted(por_bloco[b], key=lambda x: x[0])]
    try:
        cab = json.loads(llm(PROMPT_CABECALHO + paginas[0] + "\n...\n" + paginas[-1]
                             + "\n</documento>"))
        recibo = Recibo(str(cab["processo"]), date.fromisoformat(cab["data_consolidacao"]),
                        int(cab["parcelas"]), _decimal(cab["total_consolidado"]), debitos)
    except (ValueError, KeyError, TypeError) as erro:
        leitura.problemas.append(f"cabeçalho fora do formato ({erro})")
        return
    texto = "\n".join(paginas)
    leitura.recibo = recibo
    leitura.problemas += recibo.conferir()          # soma dos blocos = total do documento
    total = formatar_reais(recibo.total_consolidado).removeprefix("R$ ")
    if not aparece(total, texto):
        leitura.problemas.append(f"total {total} não aparece no documento")
    leitura.problemas += campos_inventados(recibo, texto)   # processo, data, parcelas
