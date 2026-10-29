"""Passo "fluxo n8n": automação sem IA, escrita pela TI (capítulo 15).

O agente chama um webhook do n8n com um segredo e uma chave de idempotência.
O que volta é dado: marcas neutralizadas, sem NUL, com tamanho limitado.
Só "n8n fora do ar" é tentado de novo.
"""
import html
import re
import time

import httpx

from prumo.agentes.laco import Aguardando

MAX_RESPOSTA = 20_000
MAX_AVISO = 500
_MARCAS = re.compile(r"<\s*/?\s*(dados|dados_informados|anexo|calculos|fontes)\b", re.I)


class FluxoRecusou(Exception):
    """O fluxo respondeu, e a resposta é um erro do negócio. Não adianta repetir."""


class FluxoFora(Exception):
    """O n8n não respondeu. Vale tentar de novo."""


def como_dado(texto: str, limite: int = MAX_RESPOSTA) -> str:
    texto = texto.replace("\x00", "")[:limite]
    return _MARCAS.sub(lambda m: m.group(0).replace("<", "&lt;"), texto)


def chamar_fluxo(url_base: str, caminho: str, *, segredo: str, idempotencia: str,
                 corpo: dict, timeout_s: float = 30) -> dict:
    try:
        cabecalhos = {"X-Prumo-Segredo": segredo, "X-Prumo-Idempotencia": idempotencia}
        r = httpx.post(f"{url_base}/webhook/{caminho}", json=corpo, timeout=timeout_s,
                       headers=cabecalhos)
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise FluxoFora(str(e)) from e                 # nem chegou: tentar de novo é seguro
    except httpx.TimeoutException as e:
        # chegou e não voltou: quem decide é a pessoa
        raise FluxoRecusou(f"o fluxo não respondeu em {timeout_s:.0f} s") from e
    # o n8n disse que não aceitou: repetir é seguro
    if r.status_code == 503:
        # 502 e 504 podem ter chegado: viram recusa
        raise FluxoFora("n8n indisponível (503)")
    try:
        dados = r.json()
    except ValueError:
        dados = {"texto": r.text}
    if not isinstance(dados, dict):
        dados = {"texto": str(dados)}
    if r.status_code >= 400 or "erro" in dados:
        raise FluxoRecusou(como_dado(str(dados.get("erro", f"HTTP {r.status_code}"))))
    return {"texto": como_dado(str(dados.get("texto", ""))),
            "avisos": [como_dado(str(a), MAX_AVISO) for a in dados.get("avisos", [])][:20]}


def _texto_puro(modelo: str, contexto: dict) -> str:
    """Como o preencher do laço, mas sem as marcas de dado: o texto vai para um fluxo, não para a IA."""
    def troca(m):
        valor = contexto
        for parte in m.group(1).split("."):
            if not isinstance(valor, dict) or parte not in valor:
                raise Aguardando(f"falta o valor de {m.group(1)}")
            valor = valor[parte]
        if isinstance(valor, dict):                    # saída de passo: prompt e skill têm "texto",
            for chave in ("texto", "resposta"):        # consulta à memória tem "resposta"
                if chave in valor:
                    return str(valor[chave])
            raise Aguardando(f"{m.group(1)} não tem texto para mandar ao fluxo")
        return str(valor)
    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", troca, modelo)


def executor(url_base: str, segredo: str, origem: str, quem_roda: str, *,
             tentativas: int = 3, espera_s: float = 2.0, dormir=time.sleep):
    """O executor do passo fluxo_n8n para o laço do capítulo 11.

    origem: a primeira execução da cadeia (laco.execucao_raiz). Um "continuar daqui" cria
    uma execução nova, mas a chave de idempotência continua a mesma.
    """
    def executar(passo, contexto: dict) -> dict:
        # sem e-mail e sem arquivo: só o texto, os campos e quem roda
        corpo = {"texto": _texto_puro(passo.config.get("texto", ""), contexto),
                 "campos": passo.config.get("campos", {}), "login": quem_roda}
        limite = min(max(passo.config.get("tempo_limite_s", 30), 10), 600)
        for tentativa in range(1, tentativas + 1):
            try:
                return chamar_fluxo(url_base, passo.config["caminho"], segredo=segredo,
                                    idempotencia=f"{origem}:{passo.nome}", corpo=corpo,
                                    timeout_s=limite)
            except FluxoFora:
                if tentativa == tentativas:
                    raise
                dormir(espera_s * tentativa)                   # 2 s, depois 4 s
    return executar


def para_email_do_n8n(texto: str) -> str:
    """Escapa para HTML e dobra os &.

    O "enviar e aguardar" do n8n decodifica as entidades uma vez antes de montar o e-mail.

    Escapar uma vez só deixaria um <script> vindo do texto da IA chegar inteiro ao e-mail.
    """
    return html.escape(texto, quote=True).replace("&", "&amp;")
