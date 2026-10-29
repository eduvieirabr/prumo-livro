"""LLM paga como exceção (capítulo 8).

O Broker decide se um pedido pode sair; reserva o custo máximo no
orçamento do mês ANTES de enviar; liquida pelo custo real depois. Sem o
custo real (o provedor não informou o uso), a reserva fica até a conciliação.
As chaves dos provedores ficam no gateway, nunca no banco do Broker.
"""
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_UP, Decimal

import httpx

from prumo.politica import Destino, EnvioNegado, aceite_vigente, autorizar, montar_externo

MILHAO = Decimal(1_000_000)
QUATRO_CASAS = Decimal("0.0001")


@dataclass(frozen=True)
class Preco:
    modelo: str
    entrada_por_milhao: Decimal   # R$ por milhão de tokens de entrada
    saida_por_milhao: Decimal     # R$ por milhão de tokens de saída


def custo(preco: Preco, entrada: int, saida: int) -> Decimal:
    bruto = (entrada * preco.entrada_por_milhao + saida * preco.saida_por_milhao) / MILHAO
    return bruto.quantize(QUATRO_CASAS, ROUND_UP)


MOLDE_TOKENS = 200   # instruções do sistema e marcações do formato de conversa


def tokens_estimados(texto: str) -> int:
    """Por cima: 2 tokens a cada 3 caracteres, mais o molde da conversa.

    Texto corrido em português gasta bem menos; valores, CNPJs e códigos gastam
    perto disso. Errar para cima só prende um pouco mais de orçamento.
    """
    return len(texto) * 2 // 3 + MOLDE_TOKENS


def custo_maximo(preco: Preco, texto: str, max_saida: int) -> Decimal:
    return custo(preco, tokens_estimados(texto), max_saida)


def decidir_destino(tarefa: str, modo: str, *, local_no_ar: bool, aceite: bool) -> Destino:
    """Ocupado não é fora do ar: fila cheia nunca manda pedido para a LLM paga."""
    if modo == "local" or (modo == "local_com_fallback" and local_no_ar):
        return Destino.LOCAL
    try:
        autorizar(tarefa, Destino.EXTERNO, aceite_registrado=aceite)
    except EnvioNegado:
        return Destino.LOCAL
    return Destino.EXTERNO


RESERVAR = """
WITH r AS (
    UPDATE orcamento SET reservado = reservado + %(valor)s
     WHERE integracao = %(integracao)s AND mes = %(mes)s
       AND gasto + reservado + %(valor)s <= teto
    RETURNING 1
)
INSERT INTO uso_pago (pedido, integracao, mes, modelo, reserva)
SELECT %(pedido)s, %(integracao)s, %(mes)s, %(modelo)s, %(valor)s FROM r
RETURNING pedido
"""

LIQUIDAR = """
WITH u AS (
    UPDATE uso_pago SET custo = %(custo)s, situacao = 'liquidado'
     WHERE pedido = %(pedido)s AND situacao IN ('reservado', 'conciliar')
    RETURNING integracao, mes, reserva
)
UPDATE orcamento o SET reservado = o.reservado - u.reserva, gasto = o.gasto + %(custo)s
  FROM u WHERE o.integracao = u.integracao AND o.mes = u.mes
"""


def mes_de(d: date) -> date:
    return d.replace(day=1)


def reservar(conn, pedido_id, integracao: str, modelo: str, valor: Decimal, hoje: date) -> bool:
    """True se coube no teto do mês. A reserva é atômica: duas não passam do teto juntas."""
    return conn.execute(RESERVAR, {"pedido": pedido_id, "integracao": integracao,
                                   "mes": mes_de(hoje), "modelo": modelo,
                                   "valor": valor}).fetchone() is not None


def liquidar(conn, pedido_id, custo_real: Decimal | None) -> None:
    """Troca a reserva pelo custo real; sem custo real, marca para conciliar."""
    if custo_real is None:
        conn.execute("UPDATE uso_pago SET situacao = 'conciliar' WHERE pedido = %s",
                     (pedido_id,))
        return
    conn.execute(LIQUIDAR, {"pedido": pedido_id, "custo": custo_real})


def chamar_gateway(url: str, chave_virtual: str, modelo: str, mensagens: list[dict],
                   max_saida: int, timeout: float = 120) -> tuple[str, dict | None]:
    """Formato OpenAI, falando com o gateway. A chave é virtual: a do provedor fica lá."""
    r = httpx.post(f"{url}/v1/chat/completions", timeout=timeout,
                   headers={"Authorization": f"Bearer {chave_virtual}"},
                   json={"model": modelo, "messages": mensagens, "max_tokens": max_saida})
    r.raise_for_status()
    corpo = r.json()
    return corpo["choices"][0]["message"]["content"], corpo.get("usage")


# ---------- o caminho inteiro, numa função ----------

class TetoEstourado(Exception):
    """O custo máximo do pedido não cabe no orçamento do mês: não sai, e a integração sabe."""


def atender_fora(conn, pedido: dict, integ: dict, *, local_no_ar: bool, preco: Preco,
                 gateway, hoje: date, max_saida: int = 800) -> dict | None:
    """Decide e, se for o caso, atende o pedido na LLM paga. None: fica na fila local.

    `gateway(mensagens, max_saida) -> (texto, uso)`; na plataforma, `chamar_gateway`.
    O aceite é do cliente do pedido (`conteudo.empresa`), para a finalidade da tarefa.
    """
    empresa = pedido["conteudo"].get("empresa")
    aceite = bool(empresa) and aceite_vigente(conn, empresa, pedido["tarefa"], hoje)
    destino = decidir_destino(pedido["tarefa"], integ["modo"], local_no_ar=local_no_ar,
                              aceite=aceite)
    if destino is Destino.LOCAL:
        return None
    mensagens = montar_externo(pedido["tarefa"], pedido["conteudo"])
    texto = "\n".join(m["content"] for m in mensagens)
    if not reservar(conn, pedido["id"], integ["id"], preco.modelo,
                    custo_maximo(preco, texto, max_saida), hoje):
        raise TetoEstourado(f"orçamento de {integ['id']} esgotado neste mês")
    try:
        resposta, uso = gateway(mensagens, max_saida)
    except Exception:
        liquidar(conn, pedido["id"], None)          # pode ter sido cobrado: conciliar
        raise
    real = custo(preco, uso["prompt_tokens"], uso["completion_tokens"]) if uso else None
    liquidar(conn, pedido["id"], real)
    return {"texto": resposta}
