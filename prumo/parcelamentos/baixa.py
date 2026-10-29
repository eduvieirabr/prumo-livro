"""A parcela 14 foi paga: o Parcelamentos consome a central (capítulo 17).

Lê do cursor, aplica cada evento numa transação que também registra que ele foi
aplicado, e só então confirma o cursor. Cair no meio = reentrega, e a reentrega
encontra o registro e não faz nada. Evento que falha três vezes vai para a quarentena.
"""
import hashlib
import json
from decimal import Decimal, InvalidOperation

import httpx
import psycopg

from psycopg.types.json import Jsonb

from prumo.ia.explicar import formatar_reais

CANAL_PAGOS = "vertice.titulo.pago.v1"
CANAL_BAIXAS = "prumo.parcela.baixada.v1"
TRANSITORIOS = {401, 403, 429}      # não são culpa do evento: não marcam recusa
TENTATIVAS = 3


class BaixaRecusada(Exception):
    """O evento é válido, mas não dá para aplicar: alguém precisa olhar."""


# erros do evento, que conta tentativa; banco fora do ar não conta, sobe e espera
DO_EVENTO = (BaixaRecusada, KeyError, TypeError, ValueError, InvalidOperation, psycopg.DataError)


def id_da_baixa(canal_origem: str, id_origem: str) -> str:
    """Determinística e de tamanho fixo: o id de origem pode ter os 100 caracteres do
    contrato, e o derivado não pode passar deles. A proveniência legível fica em
    parc.evento_aplicado, que liga o evento de origem ao resultado."""
    chave = json.dumps([canal_origem, id_origem], ensure_ascii=False)
    return "baixa:" + hashlib.sha256(chave.encode()).hexdigest()[:40]


def dar_baixa(conn, evento: dict) -> str:
    d = evento["dados"]
    linha = conn.execute(
        """SELECT valor_calculado, valor_pago, titulo FROM parc.parcela
            WHERE parcelamento = %s AND numero = %s FOR UPDATE""",
        (d["parcelamento"], d["parcela"])).fetchone()
    if linha is None:
        raise BaixaRecusada(f"parcela {d['parcela']} de {d['parcelamento']} não existe")
    calculado, pago, titulo = linha
    valor = Decimal(d["valor_pago"])
    if pago is not None:
        if titulo == d["titulo"] and pago == valor:
            return "já estava paga por este título"          # o Vértice publicou duas vezes
        raise BaixaRecusada(
            f"parcela já paga pelo título {titulo}, de {formatar_reais(pago)}")
    divergencia = valor - calculado if valor != calculado else None
    conn.execute(
        """UPDATE parc.parcela
              SET valor_pago = %s, pago_em = %s, titulo = %s, divergencia = %s
            WHERE parcelamento = %s AND numero = %s""",
        (valor, d["pago_em"], d["titulo"], divergencia, d["parcelamento"], d["parcela"]))
    saida = {
        # uma por fato de origem, sempre curta
        "id": id_da_baixa(CANAL_PAGOS, evento["id"]),
        "assunto": f"parcela:{d['parcelamento']}:{d['parcela']}",
        "empresa": evento.get("empresa"),
        "dados": {"parcelamento": d["parcelamento"], "parcela": d["parcela"],
                  "valor_pago": d["valor_pago"], "divergencia": divergencia is not None}}
    conn.execute("INSERT INTO parc.saida (canal, evento) VALUES (%s, %s)",
                 (CANAL_BAIXAS, Jsonb(saida)))
    if divergencia is None:
        return "baixa"
    return f"baixa com divergência de {formatar_reais(divergencia)}"


def _falhou(conn, canal: str, evento: dict, erro: Exception) -> int:
    return conn.execute(
        """INSERT INTO parc.falha_evento (canal, id_evento, seq, erro) VALUES (%s, %s, %s, %s)
           ON CONFLICT (canal, id_evento) DO UPDATE
              SET tentativas = parc.falha_evento.tentativas + 1, erro = EXCLUDED.erro
        RETURNING tentativas""", (canal, evento["id"], evento["seq"], str(erro))).fetchone()[0]


def _aplicado(conn, canal: str, evento: dict, resultado: str) -> None:
    conn.execute("""INSERT INTO parc.evento_aplicado (canal, id_evento, seq, resultado)
                    VALUES (%s, %s, %s, %s)""",
                 (canal, evento["id"], evento["seq"], resultado))


def consumir(conn, cliente, canal: str = CANAL_PAGOS, maximo: int = 100) -> list[str]:
    relatorio, ate = [], None
    for ev in cliente.ler(canal, maximo):
        try:
            with conn.transaction():
                if conn.execute("SELECT 1 FROM parc.evento_aplicado"
                                " WHERE canal = %s AND id_evento = %s",
                                (canal, ev["id"])).fetchone():
                    resultado = "repetido; nada a fazer"
                else:
                    resultado = dar_baixa(conn, ev)
                    _aplicado(conn, canal, ev, resultado)
        except DO_EVENTO as erro:
            if _falhou(conn, canal, ev, erro) < TENTATIVAS:
                # o cursor fica antes dele: volta na próxima leitura
                break
            resultado = f"quarentena: {erro}"
            _aplicado(conn, canal, ev, resultado)
        relatorio.append(f"{ev['seq']}: {resultado}")
        ate = ev["seq"]
    if ate is not None:
        # depois do commit: cair antes daqui = reentrega inofensiva
        cliente.confirmar(canal, ate)
    return relatorio


def enviar_saida(conn, cliente, maximo: int = 100) -> int:
    """Leva o outbox para a central, um evento por vez. Publicar de novo o mesmo id não duplica
    (capítulo 16); um evento recusado pela central fica marcado, e os outros seguem."""
    enviados = 0
    for ident, canal, evento in conn.execute(
            """SELECT id, canal, evento FROM parc.saida WHERE enviado_em IS NULL AND recusado IS NULL
                ORDER BY id LIMIT %s""", (maximo,)).fetchall():
        with conn.transaction():
            if conn.execute("""SELECT 1 FROM parc.saida WHERE id = %s AND enviado_em IS NULL
                               FOR UPDATE SKIP LOCKED""", (ident,)).fetchone() is None:
                continue                                  # outro processo pegou
            try:
                cliente.publicar(canal, [evento])
            except httpx.HTTPStatusError as erro:
                codigo = erro.response.status_code   # 5xx, 401, 403 e 429: tentar depois
                if codigo >= 500 or codigo in TRANSITORIOS:
                    raise
                conn.execute("UPDATE parc.saida SET recusado = %s WHERE id = %s",
                             (erro.response.text[:500], ident))
                continue
            conn.execute("UPDATE parc.saida SET enviado_em = now() WHERE id = %s", (ident,))
            enviados += 1
    return enviados


def corrigir_pagamento(conn, parcelamento: str, numero: int, valor: Decimal, quem: str,
                       motivo: str) -> None:
    """A Marta corrige o valor pago, com o comprovante na tela. Fica registrado e vira evento."""
    with conn.transaction():
        linha = conn.execute(
            """SELECT valor_pago, valor_calculado FROM parc.parcela
                WHERE parcelamento = %s AND numero = %s FOR UPDATE""", (parcelamento, numero)).fetchone()
        if linha is None:
            raise BaixaRecusada(f"parcela {numero} de {parcelamento} não existe")
        divergencia = valor - linha[1] if valor != linha[1] else None
        conn.execute("""UPDATE parc.parcela SET valor_pago = %s, divergencia = %s
                         WHERE parcelamento = %s AND numero = %s""", (valor, divergencia, parcelamento, numero))
        ident = conn.execute("""INSERT INTO parc.correcao (parcelamento, numero, de, para, por, motivo)
                                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                             (parcelamento, numero, linha[0], valor, quem, motivo)).fetchone()[0]
        conn.execute("INSERT INTO parc.saida (canal, evento) VALUES (%s, %s)", (CANAL_BAIXAS, Jsonb({
            "id": f"correcao:{parcelamento}:{numero}:{ident}", "assunto": f"parcela:{parcelamento}:{numero}",
            "dados": {"parcelamento": parcelamento, "parcela": numero, "valor_pago": f"{valor:.2f}",
                      "divergencia": divergencia is not None}})))
