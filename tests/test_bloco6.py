"""Testes dos capítulos 16 a 18. Os que tocam o banco precisam de PRUMO_TESTE_PG."""
import json
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import psycopg
import pytest

from prumo import avaliacao, conferencia
from prumo.agentes import gatilho
from prumo.central import central as c
from prumo.leitura import extrair
from prumo.parcelamentos import baixa
from tests.test_bloco2 import DSN, JSON_BOM, OUTRO_LAYOUT, precisa_pg

CNPJ_PONTAL = "44555666000181"
ESQUEMA_PAGO = {"obrigatorios": {"titulo": "texto", "parcelamento": "texto", "parcela": "inteiro",
                                 "valor_pago": "decimal", "pago_em": "data"},
                "opcionais": {"banco": "texto"}}
ESQUEMA_BAIXA = {"obrigatorios": {"parcelamento": "texto", "parcela": "inteiro",
                                  "valor_pago": "decimal", "divergencia": "booleano"}}


@pytest.fixture
def db():
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS central CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS parc CASCADE")
        conn.execute(open("prumo/central/schema.sql").read())
        conn.execute(open("prumo/parcelamentos/schema.sql").read())
        for ident, sso in (("vertice", "vertice-erp"), ("prumo-parcelamentos", "prumo-parcelamentos"),
                           ("prumo-memoria", "prumo-memoria")):
            c.cadastrar_aplicacao(conn, ident, ident, sso)
        c.aprovar_aplicacao(conn, "vertice", "renato")
        c.aprovar_aplicacao(conn, "prumo-parcelamentos", "renato", nivel="restrito")
        c.criar_canal(conn, "vertice", baixa.CANAL_PAGOS, "restrito", ESQUEMA_PAGO)
        c.criar_canal(conn, "prumo-parcelamentos", baixa.CANAL_BAIXAS, "restrito", ESQUEMA_BAIXA)
        c.inscrever(conn, "prumo-parcelamentos", baixa.CANAL_PAGOS)
        yield conn


def pago(n=14, valor="2846.51", titulo="88231", evento=None):
    return {"id": evento or f"pg-{titulo}", "assunto": f"titulo:{titulo}", "empresa": CNPJ_PONTAL,
            "dados": {"titulo": titulo, "parcelamento": "PONTAL-2025-09", "parcela": n,
                      "valor_pago": valor, "pago_em": "2026-11-27"}}


# ---------- capítulo 16 ----------

@precisa_pg
def test_so_aplicacao_aprovada_e_dona_publica(db):
    with pytest.raises(c.SemPermissao):
        c.publicar(db, "prumo-memoria", baixa.CANAL_PAGOS, [pago()])          # não aprovada
    with pytest.raises(c.SemPermissao):
        c.publicar(db, "prumo-parcelamentos", baixa.CANAL_PAGOS, [pago()])    # não é dona
    assert c.publicar(db, "vertice", baixa.CANAL_PAGOS, [pago()]) == [1]
    assert c.publicar(db, "vertice", baixa.CANAL_PAGOS, [pago()]) == [1]      # mesmo id, mesmo número


@precisa_pg
def test_campo_renomeado_e_recusado_na_publicacao(db):
    renomeado = pago()
    renomeado["dados"]["numero_parcela"] = renomeado["dados"].pop("parcela")
    with pytest.raises(c.EventoInvalido) as erro:
        c.publicar(db, "vertice", baixa.CANAL_PAGOS, [pago(13), renomeado])
    assert "falta o campo 'parcela'" in str(erro.value) and "numero_parcela" in str(erro.value)
    assert db.execute("SELECT count(*) FROM central.evento").fetchone()[0] == 0     # lote inteiro ou nada


@precisa_pg
def test_dinheiro_cnpj_e_data_no_formato_do_contrato(db):
    for mudar in (lambda e: e["dados"].update(valor_pago=2846.51),
                  lambda e: e["dados"].update(valor_pago="2.846,51"),
                  lambda e: e["dados"].update(pago_em="27/11/2026"),
                  lambda e: e.update(empresa="44.555.666/0001-81")):
        ev = pago()
        mudar(ev)
        with pytest.raises(c.EventoInvalido):
            c.publicar(db, "vertice", baixa.CANAL_PAGOS, [ev])


@precisa_pg
def test_canal_restrito_exige_nivel(db):
    c.aprovar_aplicacao(db, "prumo-memoria", "renato")                        # nível interno
    with pytest.raises(c.SemPermissao):
        c.inscrever(db, "prumo-memoria", baixa.CANAL_PAGOS)
    c.publicar(db, "vertice", baixa.CANAL_PAGOS, [pago()])
    db.execute("UPDATE central.aplicacao SET nivel = 'interno' WHERE id = 'prumo-parcelamentos'")
    with pytest.raises(c.SemPermissao):
        c.ler(db, "prumo-parcelamentos", baixa.CANAL_PAGOS)                   # o nível baixou depois


@precisa_pg
def test_cursor_so_anda_para_a_frente_e_ate_o_entregue(db):
    c.publicar(db, "vertice", baixa.CANAL_PAGOS, [pago(n, titulo=str(n)) for n in (12, 13, 14)])
    with pytest.raises(c.EventoInvalido):
        c.confirmar(db, "prumo-parcelamentos", baixa.CANAL_PAGOS, 2)          # nada entregue ainda
    lidos = c.ler(db, "prumo-parcelamentos", baixa.CANAL_PAGOS, maximo=2)
    assert [e["seq"] for e in lidos] == [1, 2]
    assert [e["seq"] for e in c.ler(db, "prumo-parcelamentos", baixa.CANAL_PAGOS, 2)] == [1, 2]
    assert c.confirmar(db, "prumo-parcelamentos", baixa.CANAL_PAGOS, 2) == 2
    with pytest.raises(c.EventoInvalido):
        c.confirmar(db, "prumo-parcelamentos", baixa.CANAL_PAGOS, 1)          # não volta
    assert [e["seq"] for e in c.ler(db, "prumo-parcelamentos", baixa.CANAL_PAGOS)] == [3]
    assert c.quem_le(db, baixa.CANAL_PAGOS) == [{"aplicacao": "prumo-parcelamentos",
                                                 "cursor": 2, "atraso": 1}]


@precisa_pg
def test_numeracao_densa_com_publicadores_simultaneos(db):
    def publica(k):
        with psycopg.connect(DSN, autocommit=True) as conn:
            for i in range(25):
                c.publicar(conn, "vertice", baixa.CANAL_PAGOS, [pago(titulo=f"{k}-{i}")])
    fios = [threading.Thread(target=publica, args=(k,)) for k in range(4)]
    [f.start() for f in fios]
    [f.join() for f in fios]
    seqs = [s for (s,) in db.execute("SELECT seq FROM central.evento ORDER BY seq")]
    assert seqs == list(range(1, 101))                                          # sem buracos


@precisa_pg
def test_retencao_que_passa_do_cursor_vira_eventos_perdidos(db):
    c.publicar(db, "vertice", baixa.CANAL_PAGOS, [pago(n, titulo=str(n)) for n in (12, 13)])
    db.execute("UPDATE central.evento SET publicado_em = now() - interval '40 days' WHERE seq = 1")
    assert c.limpar(db, datetime.now(timezone.utc)) == 1
    with pytest.raises(c.EventosPerdidos) as erro:
        c.ler(db, "prumo-parcelamentos", baixa.CANAL_PAGOS)
    assert (erro.value.cursor, erro.value.primeiro) == (0, 2)


@precisa_pg
def test_api_http_com_token_de_servico(db, monkeypatch):
    from fastapi.testclient import TestClient
    from prumo.central import api
    from prumo.identidade import Verificador
    from tests.test_bloco3 import EMISSOR, emitir
    from cryptography.hazmat.primitives.asymmetric import rsa
    import jwt
    privada = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(privada.public_key()))
    jwks = {"keys": [{**jwk, "kid": "k1", "use": "sig", "alg": "RS256"}]}
    monkeypatch.setenv("CENTRAL_DSN", DSN)
    monkeypatch.setattr(api, "VERIFICADOR", Verificador(EMISSOR, "prumo-central", lambda: jwks))
    cli = TestClient(api.app)

    def cab(azp):
        return {"Authorization": "Bearer " + emitir(privada, aud="prumo-central", azp=azp, client_id=azp)}
    r = cli.post(f"/v1/canais/{baixa.CANAL_PAGOS}/eventos", json={"eventos": [pago()]},
                 headers=cab("vertice-erp"))
    assert r.status_code == 201 and r.json() == {"seqs": [1]}
    r = cli.get(f"/v1/inscricoes/{baixa.CANAL_PAGOS}/eventos", headers=cab("prumo-parcelamentos"))
    assert r.status_code == 200 and r.json()["eventos"][0]["dados"]["valor_pago"] == "2846.51"
    assert cli.post(f"/v1/inscricoes/{baixa.CANAL_PAGOS}/cursor", json={"ate": 1},
                    headers=cab("prumo-parcelamentos")).json() == {"cursor": 1}
    assert cli.post(f"/v1/canais/{baixa.CANAL_PAGOS}/eventos", json={"eventos": [pago()]},
                    headers=cab("prumo-parcelamentos")).status_code == 403
    pessoa = emitir(privada, aud="prumo-central", azp="vertice-erp", sub="caio")
    assert cli.get(f"/v1/inscricoes/{baixa.CANAL_PAGOS}/eventos",
                   headers={"Authorization": f"Bearer {pessoa}"}).status_code == 401
    assert cli.get(f"/v1/inscricoes/{baixa.CANAL_PAGOS}/eventos").status_code == 401   # sem cabeçalho
    sem_azp = emitir(privada, aud="prumo-central")
    assert cli.get(f"/v1/inscricoes/{baixa.CANAL_PAGOS}/eventos",
                   headers={"Authorization": f"Bearer {sem_azp}"}).status_code == 401
    assert cli.post("/v1/canais/vertice.cliente.novo.v1/eventos", json={"eventos": [pago()]},
                    headers=cab("vertice-erp")).status_code == 404


# ---------- capítulo 17 ----------

class ClienteLocal:
    """O cliente da central, falando direto com o banco, com os erros que o HTTP daria."""

    CODIGOS = {c.SemPermissao: 403, c.EventoInvalido: 422, c.CanalInexistente: 404, c.EventosPerdidos: 409}

    def __init__(self, conn, app, cair_antes_de_confirmar=False):
        self.conn, self.app, self.cair = conn, app, cair_antes_de_confirmar

    def _http(self, funcao, *args):
        import httpx
        try:
            return funcao(self.conn, self.app, *args)
        except tuple(self.CODIGOS) as erro:
            pedido = httpx.Request("POST", "http://central")
            resposta = httpx.Response(self.CODIGOS[type(erro)], text=str(erro), request=pedido)
            raise httpx.HTTPStatusError(str(erro), request=pedido, response=resposta) from erro

    def ler(self, canal, maximo=100):
        return self._http(c.ler, canal, maximo)

    def confirmar(self, canal, ate):
        if self.cair:
            raise RuntimeError("processo caiu antes de confirmar")
        return self._http(c.confirmar, canal, ate)

    def publicar(self, canal, eventos):
        return self._http(c.publicar, canal, eventos)


def _parcelas(conn):
    for n in (13, 14, 15):
        conn.execute("""INSERT INTO parc.parcela (parcelamento, numero, vencimento, valor_calculado)
                        VALUES ('PONTAL-2025-09', %s, '2026-11-27', 2846.51)""", (n,))


@precisa_pg
def test_baixa_e_reentrega_depois_de_cair_antes_do_cursor(db):
    _parcelas(db)
    c.publicar(db, "vertice", baixa.CANAL_PAGOS, [pago(13, titulo="13"), pago(14, titulo="14")])
    with pytest.raises(RuntimeError):
        baixa.consumir(db, ClienteLocal(db, "prumo-parcelamentos", cair_antes_de_confirmar=True))
    relatorio = baixa.consumir(db, ClienteLocal(db, "prumo-parcelamentos"))   # reentrega
    assert relatorio == ["1: repetido; nada a fazer", "2: repetido; nada a fazer"]
    pagas = db.execute("SELECT count(*) FROM parc.parcela WHERE valor_pago IS NOT NULL").fetchone()[0]
    assert pagas == 2 and db.execute("SELECT count(*) FROM parc.saida").fetchone()[0] == 2
    assert c.quem_le(db, baixa.CANAL_PAGOS)[0]["cursor"] == 2


@precisa_pg
def test_valor_do_darf_manda_e_divergencia_vai_para_a_marta(db):
    _parcelas(db)
    c.publicar(db, "vertice", baixa.CANAL_PAGOS, [pago(14, valor="2846.15")])
    assert baixa.consumir(db, ClienteLocal(db, "prumo-parcelamentos")) == \
        ["1: baixa com divergência de -R$ 0,36"]
    pago_, div = db.execute("SELECT valor_pago, divergencia FROM parc.parcela WHERE numero = 14").fetchone()
    assert pago_ == D("2846.15") and div == D("-0.36")


@precisa_pg
def test_mesmo_titulo_publicado_duas_vezes_com_ids_diferentes(db):
    _parcelas(db)
    c.publicar(db, "vertice", baixa.CANAL_PAGOS, [pago(14, evento="a"), pago(14, evento="b")])
    assert baixa.consumir(db, ClienteLocal(db, "prumo-parcelamentos")) == \
        ["1: baixa", "2: já estava paga por este título"]


@precisa_pg
def test_evento_que_falha_tres_vezes_vai_para_a_quarentena_e_nao_trava_a_fila(db):
    _parcelas(db)
    c.publicar(db, "vertice", baixa.CANAL_PAGOS, [pago(99, titulo="99"), pago(14, titulo="14")])
    cli = ClienteLocal(db, "prumo-parcelamentos")
    assert baixa.consumir(db, cli) == [] and baixa.consumir(db, cli) == []
    terceira = baixa.consumir(db, cli)
    assert terceira[0].startswith("1: quarentena: parcela 99") and terceira[1] == "2: baixa"
    assert db.execute("SELECT tentativas FROM parc.falha_evento").fetchone()[0] == 3


@precisa_pg
def test_outbox_publica_a_baixa_sem_duplicar(db):
    _parcelas(db)
    c.publicar(db, "vertice", baixa.CANAL_PAGOS, [pago(14)])
    baixa.consumir(db, ClienteLocal(db, "prumo-parcelamentos"))
    cli = ClienteLocal(db, "prumo-parcelamentos")
    assert baixa.enviar_saida(db, cli) == 1 and baixa.enviar_saida(db, cli) == 0
    db.execute("UPDATE parc.saida SET enviado_em = NULL")                     # caiu antes de marcar
    assert baixa.enviar_saida(db, cli) == 1                                   # republica: mesmo número
    assert db.execute("SELECT count(*) FROM central.evento WHERE canal = %s",
                      (baixa.CANAL_BAIXAS,)).fetchone()[0] == 1


@precisa_pg
def test_evento_dispara_agente_uma_vez_e_respeita_o_nivel():
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS disparo, gatilho CASCADE")
        conn.execute(open("prumo/agentes/schema.sql").read())
        for dono in ("marta", "livia"):
            conn.execute("INSERT INTO gatilho (dono, agente, canal) VALUES (%s, 'avisar-cliente', %s)",
                         (dono, baixa.CANAL_BAIXAS))
        papeis = {"marta": "supervisor", "livia": "operador"}
        rodadas = []
        ev = {"id": "baixa:PONTAL-2025-09:14", "dados": {"parcela": 14}}
        r1 = gatilho.disparar(conn, baixa.CANAL_BAIXAS, "restrito", ev, lambda d, a: True,
                              papeis.get, lambda g, ent: rodadas.append(g["dono"]) or True)
        r2 = gatilho.disparar(conn, baixa.CANAL_BAIXAS, "restrito", ev, lambda d, a: True,
                              papeis.get, lambda g, ent: rodadas.append(g["dono"]) or True)
        assert rodadas == ["marta"] and "suspenso, canal restrito" in r1[1]
        assert r2 == ["1: evento já disparou este gatilho"]


# ---------- capítulo 18 ----------

def test_modelo_sabotador_na_leitura():
    sab = avaliacao.sabotagens_da_leitura(json.loads(JSON_BOM))
    def recusa_antes(r):
        return not extrair.ler(OUTRO_LAYOUT, lambda _: r).aprovada

    def recusa_depois(r):
        return not conferencia.ler_conferido(OUTRO_LAYOUT, lambda _: r).aprovada

    antes = avaliacao.avaliar(sab, recusa_antes)
    depois = avaliacao.avaliar(sab, recusa_depois)
    assert sum(antes.values()) == 6 and sum(depois.values()) == len(sab) == 10
    mes = "competência do mês seguinte"
    assert not antes[mes] and depois[mes]
    assert not recusa_depois(JSON_BOM)                      # o certo passa


def test_modelo_sabotador_na_explicacao():
    calc = {"parcela_14": D("3412.87"), "parcela_base": D("2978.07")}
    certa = "A parcela 14 ficou em R$ 3.412,87, sobre a base de 2.978,07, com Selic de 14,62%."
    assert conferencia.conferir_explicacao(certa, calc) == set()
    r = avaliacao.avaliar(avaliacao.sabotagens_da_explicacao("3.412,87"),
                          lambda t: conferencia.conferir_explicacao(t, calc))
    assert r == {"dígitos trocados, com R$": True, "valor inventado, com R$": True,
                 "dígitos trocados, sem R$": True, "valor colado, sem pontos": True,
                 "sem pontos e sem R$": True, "valor por extenso": False}       # limite conhecido
    assert conferencia.conferir_explicacao("A divergência foi de -R$ 0,36.", {"d": D("-0.36")}) == set()
    assert conferencia.conferir_explicacao("Selic de 14,62 por cento.", calc) == set()


@precisa_pg
def test_mesmo_id_com_outro_conteudo_e_recusado(db):
    c.publicar(db, "vertice", baixa.CANAL_PAGOS, [pago(14)])
    with pytest.raises(c.EventoInvalido, match="outro conteúdo"):
        c.publicar(db, "vertice", baixa.CANAL_PAGOS, [pago(14, valor="2846.15")])


@precisa_pg
def test_banco_fora_do_ar_nao_conta_tentativa(db, monkeypatch):
    _parcelas(db)
    c.publicar(db, "vertice", baixa.CANAL_PAGOS, [pago(14)])

    def cai(conn, ev):
        raise psycopg.OperationalError("conexão perdida")
    monkeypatch.setattr(baixa, "dar_baixa", cai)
    for _ in range(4):
        with pytest.raises(psycopg.OperationalError):
            baixa.consumir(db, ClienteLocal(db, "prumo-parcelamentos"))
    assert db.execute("SELECT count(*) FROM parc.falha_evento").fetchone()[0] == 0


def test_token_de_servico_guardado_ate_perto_de_vencer():
    from prumo.servicos import TokenDeServico
    pedidos, relogio = [], [1000.0]

    class Http:
        @staticmethod
        def post(url, data, timeout):
            pedidos.append(data["client_id"])
            import httpx
            return httpx.Response(200, json={"access_token": f"t{len(pedidos)}", "expires_in": 300},
                                  request=httpx.Request("POST", url))
    token = TokenDeServico("http://id/token", "prumo-parcelamentos", "s", http=Http, agora=lambda: relogio[0])
    assert token() == "t1" and token() == "t1"
    relogio[0] += 271                                       # faltam 29 s: renova antes de vencer
    assert token() == "t2" and pedidos == ["prumo-parcelamentos"] * 2



@precisa_pg
def test_evento_recusado_no_outbox_nao_trava_os_outros(db):
    _parcelas(db)
    db.execute("INSERT INTO parc.saida (canal, evento) VALUES (%s, %s)",
               (baixa.CANAL_BAIXAS, json.dumps({"id": "x", "assunto": "y", "dados": {"parcela": 1}})))
    c.publicar(db, "vertice", baixa.CANAL_PAGOS, [pago(13, titulo="13")])
    baixa.consumir(db, ClienteLocal(db, "prumo-parcelamentos"))
    assert baixa.enviar_saida(db, ClienteLocal(db, "prumo-parcelamentos")) == 1
    assert "falta o campo" in db.execute("SELECT recusado FROM parc.saida WHERE id = 1").fetchone()[0]


@precisa_pg
def test_correcao_da_marta_vira_evento_novo(db):
    _parcelas(db)
    c.publicar(db, "vertice", baixa.CANAL_PAGOS, [pago(14, valor="2846.15")])
    baixa.consumir(db, ClienteLocal(db, "prumo-parcelamentos"))
    baixa.corrigir_pagamento(db, "PONTAL-2025-09", 14, D("2846.51"), "marta", "dígitos trocados no título")
    assert db.execute("SELECT valor_pago, divergencia FROM parc.parcela WHERE numero = 14").fetchone() == \
        (D("2846.51"), None)
    assert baixa.enviar_saida(db, ClienteLocal(db, "prumo-parcelamentos")) == 2       # baixa e correção


@precisa_pg
def test_erro_de_dado_do_banco_conta_tentativa(db, monkeypatch):
    _parcelas(db)
    c.publicar(db, "vertice", baixa.CANAL_PAGOS, [pago(14)])

    def estoura(conn, ev):
        raise psycopg.errors.NumericValueOutOfRange("valor fora da faixa")
    monkeypatch.setattr(baixa, "dar_baixa", estoura)
    for _ in range(3):
        baixa.consumir(db, ClienteLocal(db, "prumo-parcelamentos"))
    assert db.execute("SELECT tentativas FROM parc.falha_evento").fetchone()[0] == 3


@precisa_pg
def test_perda_aceita_destrava_a_inscricao(db):
    c.publicar(db, "vertice", baixa.CANAL_PAGOS, [pago(n, titulo=str(n)) for n in (12, 13)])
    db.execute("UPDATE central.evento SET publicado_em = now() - interval '40 days' WHERE seq = 1")
    c.limpar(db, datetime.now(timezone.utc))
    with pytest.raises(c.EventosPerdidos):
        c.ler(db, "prumo-parcelamentos", baixa.CANAL_PAGOS)
    assert c.aceitar_perda(db, "prumo-parcelamentos", baixa.CANAL_PAGOS, "renato") == 1
    assert [e["seq"] for e in c.ler(db, "prumo-parcelamentos", baixa.CANAL_PAGOS)] == [2]
    assert db.execute("SELECT de, ate, por FROM central.perda_aceita").fetchone() == (0, 1, "renato")


@precisa_pg
def test_limpar_nunca_abre_buraco_no_meio(db):
    c.publicar(db, "vertice", baixa.CANAL_PAGOS, [pago(n, titulo=str(n)) for n in (12, 13, 14)])
    db.execute("UPDATE central.evento SET publicado_em = now() - interval '40 days' WHERE seq = 2")
    assert c.limpar(db, datetime.now(timezone.utc)) == 2                     # o 1 vai junto com o 2
    assert [s for (s,) in db.execute("SELECT seq FROM central.evento")] == [3]


@precisa_pg
def test_republicar_com_outra_empresa_e_recusado_e_classificacao_viaja(db):
    c.publicar(db, "vertice", baixa.CANAL_PAGOS, [pago()])
    sem = pago()
    sem["empresa"] = None
    with pytest.raises(c.EventoInvalido, match="outro conteúdo"):
        c.publicar(db, "vertice", baixa.CANAL_PAGOS, [sem])
    assert c.ler(db, "prumo-parcelamentos", baixa.CANAL_PAGOS)[0]["classificacao"] == "restrito"


def test_tipos_estritos_do_contrato():
    assert not c._confere("00000000000000", "cnpj") and not c._confere("0003412.87", "decimal")
    assert not c._confere("-0.00", "decimal") and c._confere("-0.36", "decimal")
    assert not c._confere("a\x00b", "texto") and not c._confere(True, "inteiro")
    erros = c.conferir_evento({"id": "x\x00", "assunto": "a", "dados": {}}, {"obrigatorios": {}})
    assert erros == ["id obrigatório, texto de até 100 caracteres"]


@precisa_pg
def test_idempotencia_vale_depois_da_retencao(db):
    c.publicar(db, "vertice", baixa.CANAL_PAGOS, [pago(14)])
    db.execute("UPDATE central.evento SET publicado_em = now() - interval '40 days'")
    assert c.limpar(db, datetime.now(timezone.utc)) == 1
    assert c.publicar(db, "vertice", baixa.CANAL_PAGOS, [pago(14)]) == [1]          # o mesmo número
    assert db.execute("SELECT count(*) FROM central.evento").fetchone()[0] == 0      # e nada regravado
    with pytest.raises(c.EventoInvalido, match="outro conteúdo"):
        c.publicar(db, "vertice", baixa.CANAL_PAGOS, [pago(14, valor="2846.15")])


def test_avaliacao_real_identidade_classificacao_e_limite():
    from prumo import avaliacao
    from prumo.conferencia import ler_conferido
    from prumo.leitura.extrair import recibo_de_json
    from tests.test_bloco2 import JSON_BOM, OUTRO_LAYOUT
    tags = {"models": [{"name": "qwen3:14b", "digest": "a1b2c3d4e5f6a7b8c9d0"}]}
    assert avaliacao.identidade_do_modelo(tags, "qwen3:14b") == "qwen3:14b@a1b2c3d4e5f6"
    assert avaliacao.identidade_do_modelo(tags, "qwen3:14b", completo=True) == \
        "qwen3:14b@a1b2c3d4e5f6a7b8c9d0"
    with pytest.raises(LookupError):
        avaliacao.identidade_do_modelo(tags, "qwen3:32b")

    gabarito = recibo_de_json(JSON_BOM)
    certa = ler_conferido(OUTRO_LAYOUT, lambda _: JSON_BOM)
    assert avaliacao.classificar(certa, gabarito) == "certa"
    torto = json.loads(JSON_BOM)
    torto["parcelas"] = 61
    recusada = ler_conferido(OUTRO_LAYOUT, lambda _: json.dumps(torto))
    assert avaliacao.classificar(recusada, gabarito) == "recusada"
    outro = recibo_de_json(json.dumps({**json.loads(JSON_BOM), "processo": "13804.720001/2025-01"}))
    assert avaliacao.classificar(certa, outro) == "errada"     # aprovada, mas não é o gabarito

    assert round(avaliacao.limite_superior(100), 4) == 0.0295   # zero em 100: até ~3%
    assert round(avaliacao.limite_superior(300), 4) == 0.0099


def test_avaliacao_mede_o_modelo_e_nao_o_parser():
    from prumo.conferencia import conferir_resposta, ler_conferido
    torto = json.loads(JSON_BOM)
    torto["parcelas"] = 61
    assert conferir_resposta(OUTRO_LAYOUT, JSON_BOM).aprovada
    recusada = conferir_resposta(OUTRO_LAYOUT, json.dumps(torto))
    assert not recusada.aprovada and "parcelas 61" in recusada.problemas[0]
    assert not conferir_resposta(OUTRO_LAYOUT, '{"processo": ').aprovada
    assert ler_conferido(OUTRO_LAYOUT, lambda _: JSON_BOM).aprovada   # mesma régua


def test_sabotador_contagens_de_antes_da_explicacao_e_do_plano():
    from prumo.agentes import planilha as pl
    from prumo.ia.explicar import valores_estranhos
    calc = {"parcela_14": D("3412.87"), "parcela_base": D("2978.07")}
    sab = avaliacao.sabotagens_da_explicacao("3.412,87")
    antes = avaliacao.avaliar(sab, lambda t: valores_estranhos(t, calc))
    depois = avaliacao.avaliar(sab, lambda t: conferencia.conferir_explicacao(t, calc))
    assert (len(sab), sum(antes.values()), sum(depois.values())) == (6, 3, 5)

    colunas = [("Data", "data"), ("Histórico", "texto"), ("Valor", "numero"),
               ("Situação", "texto")]
    m = {"extrato": {"linhas": 6, "colunas": [{"nome": n, "tipo": t, "exemplos": []}
                                              for n, t in colunas]}}
    certo = {"calculos": [{"nome": "nao_identificados", "aba": "extrato", "operacao": "soma",
                           "coluna": "Valor", "filtros": [{"coluna": "Situação", "op": "=",
                                                           "valor": "não identificado"}]}]}
    assert pl.conferir_plano(certo, m) == []
    plano = avaliacao.avaliar(avaliacao.sabotagens_do_plano(certo),
                              lambda r: pl.conferir_plano(r, m))
    assert (len(plano), sum(plano.values())) == (7, 5)        # antes e depois: 5 de 7
    assert not plano["filtro com o valor errado"] and not plano["contagem no lugar de soma"]


@precisa_pg
def test_id_de_origem_com_100_caracteres_ainda_gera_baixa_publicavel(db):
    _parcelas(db)
    longo = "pg-" + "x" * 97                                     # o máximo do contrato
    c.publicar(db, "vertice", baixa.CANAL_PAGOS, [pago(14, evento=longo)])
    baixa.consumir(db, ClienteLocal(db, "prumo-parcelamentos"))
    assert baixa.enviar_saida(db, ClienteLocal(db, "prumo-parcelamentos")) == 1
    (evento,) = db.execute("SELECT evento FROM parc.saida").fetchone()
    assert len(evento["id"]) <= 100
    assert evento["id"] == baixa.id_da_baixa(baixa.CANAL_PAGOS, longo)   # o mesmo, sempre
    assert db.execute("SELECT recusado FROM parc.saida").fetchone()[0] is None
