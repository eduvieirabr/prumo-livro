"""O que os scripts da central (caps. 16 e 17) compartilham: esquema, canais e eventos."""
import httpx

from prumo.central import central as c
from prumo.parcelamentos import baixa

ESQUEMA_PAGO = {"obrigatorios": {"titulo": "texto", "parcelamento": "texto",
                                 "parcela": "inteiro", "valor_pago": "decimal",
                                 "pago_em": "data"},
                "opcionais": {"banco": "texto"}}
ESQUEMA_BAIXA = {"obrigatorios": {"parcelamento": "texto", "parcela": "inteiro",
                                  "valor_pago": "decimal", "divergencia": "booleano"}}


def preparar(conn) -> None:
    """Central e Parcelamentos do zero.

    O Vértice publica pagos; o Parcelamentos lê os pagos e publica as baixas.
    """
    conn.execute("DROP SCHEMA IF EXISTS central CASCADE")
    conn.execute("DROP SCHEMA IF EXISTS parc CASCADE")
    conn.execute(open("prumo/central/schema.sql").read())
    conn.execute(open("prumo/parcelamentos/schema.sql").read())
    for ident, sso in (("vertice", "vertice-erp"),
                       ("prumo-parcelamentos", "prumo-parcelamentos")):
        c.cadastrar_aplicacao(conn, ident, ident, sso)
    c.aprovar_aplicacao(conn, "vertice", "renato")
    c.aprovar_aplicacao(conn, "prumo-parcelamentos", "renato", nivel="restrito")
    c.criar_canal(conn, "vertice", baixa.CANAL_PAGOS, "restrito", ESQUEMA_PAGO)
    c.criar_canal(conn, "prumo-parcelamentos", baixa.CANAL_BAIXAS, "restrito", ESQUEMA_BAIXA)
    c.inscrever(conn, "prumo-parcelamentos", baixa.CANAL_PAGOS)


def pago(parcela: int = 14, titulo: str = "88231", valor: str = "2846.51") -> dict:
    return {"id": f"pg-{titulo}", "assunto": f"titulo:{titulo}", "empresa": "44555666000181",
            "dados": {"titulo": titulo, "parcelamento": "PONTAL-2025-09", "parcela": parcela,
                      "valor_pago": valor, "pago_em": "2026-11-27"}}


class ClienteLocal:
    """O cliente da central chamando as funções no banco, com os erros que o HTTP daria."""

    CODIGOS = {c.SemPermissao: 403, c.EventoInvalido: 422, c.CanalInexistente: 404,
               c.EventosPerdidos: 409}

    def __init__(self, conn, app: str):
        self.conn, self.app = conn, app

    def _http(self, funcao, *args):
        try:
            return funcao(self.conn, self.app, *args)
        except tuple(self.CODIGOS) as erro:
            pedido = httpx.Request("POST", "http://central")
            resposta = httpx.Response(self.CODIGOS[type(erro)], text=str(erro), request=pedido)
            raise httpx.HTTPStatusError(str(erro), request=pedido, response=resposta) from erro

    def ler(self, canal, maximo=100):
        return self._http(c.ler, canal, maximo)

    def confirmar(self, canal, ate):
        return self._http(c.confirmar, canal, ate)

    def publicar(self, canal, eventos):
        return self._http(c.publicar, canal, eventos)
