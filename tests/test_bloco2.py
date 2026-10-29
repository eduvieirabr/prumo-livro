"""Testes dos capítulos 4 a 6.

Os testes do Broker usam um PostgreSQL de verdade e são pulados sem ele:
    PRUMO_TESTE_PG=postgresql://postgres@localhost:5432/prumo pytest
"""
import json
import os
import threading
import uuid
from decimal import Decimal as D

import pytest

from prumo.leitura import extrair
from prumo.leitura.pdf import extrair_paginas
from tests.test_bloco1 import RECIBO

# ---------- capítulo 4 ----------

OUTRO_LAYOUT = """DEMONSTRATIVO DE DÉBITOS PARCELADOS
Proc. 13804.720001/2025-00 | consolidado em 15/01/2025 | 60 prestações
Tributo | Período | Saldo
IRPJ | 03/2024 | 98.420,15
CSLL | 03/2024 | 54.103,90
PIS  | 06/2024 | 26.160,15
Valor consolidado: 178.684,20
"""

JSON_BOM = json.dumps({
    "processo": "13804.720001/2025-00", "data_consolidacao": "2025-01-15", "parcelas": 60,
    "total_consolidado": "178684.20",
    "debitos": [{"tributo": "IRPJ", "competencia": "03/2024", "valor": "98420.15"},
                {"tributo": "CSLL", "competencia": "03/2024", "valor": "54103.90"},
                {"tributo": "PIS", "competencia": "06/2024", "valor": "26160.15"}]})


def test_layout_conhecido_nao_usa_llm():
    def llm(_):
        raise AssertionError("não deveria chamar o LLM")
    leitura = extrair.ler(RECIBO, llm)
    assert leitura.origem == "recibo-rfb-v1" and leitura.aprovada


def test_layout_desconhecido_vai_ao_llm_e_passa_na_conferencia():
    leitura = extrair.ler(OUTRO_LAYOUT, lambda _: JSON_BOM)
    assert leitura.origem == "llm" and leitura.aprovada


def test_llm_que_inventa_valor_e_pega():
    # troca 54.103,90 por 54.130,90 e ajusta o total para a soma fechar
    inventado = JSON_BOM.replace("54103.90", "54130.90").replace("178684.20", "178711.20")
    leitura = extrair.ler(OUTRO_LAYOUT, lambda _: inventado)
    assert leitura.recibo.conferir() == []          # a soma fecha...
    assert not leitura.aprovada                     # ...mas os valores não estão no documento
    assert any("54.130,90" in p for p in leitura.problemas)


def test_llm_fora_do_formato():
    leitura = extrair.ler(OUTRO_LAYOUT, lambda _: "Claro! Aqui estão os débitos: ...")
    assert not leitura.aprovada and "fora do formato" in leitura.problemas[0]


def test_sem_llm_layout_desconhecido_fica_para_humano():
    assert not extrair.ler(OUTRO_LAYOUT).aprovada


def test_layout_parecido_que_mudou_vai_ao_llm():
    novo_ecac = OUTRO_LAYOUT.replace("DEMONSTRATIVO DE DÉBITOS PARCELADOS",
                                     "RECIBO DE CONSOLIDAÇÃO (versão 2)")
    leitura = extrair.ler(novo_ecac, lambda _: JSON_BOM)
    assert leitura.origem == "llm" and leitura.aprovada


def test_ia_fora_do_ar_nao_derruba_a_tela():
    def llm(_):
        raise ConnectionError("Broker indisponível")
    leitura = extrair.ler(OUTRO_LAYOUT, llm)
    assert not leitura.aprovada and "IA indisponível" in leitura.problemas[0]


def test_pedaco_de_outro_valor_nao_conta():
    assert extrair.aparece("54.103,90", OUTRO_LAYOUT)
    assert not extrair.aparece("4.103,90", OUTRO_LAYOUT)


def test_ocr_so_na_pagina_sem_texto(tmp_path):
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    imagem = tmp_path / "escaneada.png"
    img = Image.new("L", (1600, 400), 255)
    fonte = ImageFont.load_default(size=64)
    ImageDraw.Draw(img).text((40, 150), "TOTAL CONSOLIDADO: 178.684,20", fill=0, font=fonte)
    img.save(imagem)

    pdf = tmp_path / "recibo.pdf"
    c = canvas.Canvas(str(pdf), pagesize=A4)
    c.drawString(72, 760, "RECIBO DE CONSOLIDACAO DE PARCELAMENTO - pagina com texto")
    c.showPage()
    c.drawImage(str(imagem), 40, 500, width=520, height=130)
    c.showPage()
    c.save()

    chamadas = []

    def ocr(pagina):
        chamadas.append(pagina.page_number)
        from prumo.leitura.pdf import ocr_tesseract
        return ocr_tesseract(pagina, idioma="eng")

    paginas = extrair_paginas(str(pdf), ocr=ocr)
    assert [p.origem for p in paginas] == ["texto", "ocr"]
    assert chamadas == [2]
    assert "178.684,20" in paginas[1].texto


# ---------- capítulos 5 e 6 ----------

DSN = os.getenv("PRUMO_TESTE_PG")
precisa_pg = pytest.mark.skipif(not DSN, reason="sem PRUMO_TESTE_PG")


@pytest.fixture
def banco():
    import psycopg
    from prumo.broker.fila import criar_schema
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS aceite, evento, uso_pago, orcamento, pedido, integracao CASCADE")
        criar_schema(conn)
        yield conn


def _pedido(conn, prioridade=1, idade="0 seconds"):
    return conn.execute(
        """INSERT INTO pedido (integracao, tarefa, prioridade, chave, conteudo, criado_em)
           VALUES ('parcelamentos', 'extrair_debitos', %s, %s, '{}', now() - %s::interval)
           RETURNING id""", (prioridade, uuid.uuid4().hex, idade)).fetchone()[0]


def _integracao(conn, token="prm_teste", tarefas=("extrair_debitos", "assistente"),
                id="parcelamentos", avaliacao=False):
    from prumo.broker.api import hash_token
    conn.execute("""INSERT INTO integracao (id, token_hash, tarefas, interativa, avaliacao)
                    VALUES (%s, %s, %s, true, %s)""", (id, hash_token(token), list(tarefas), avaliacao))


@precisa_pg
def test_interativo_passa_a_frente_e_lote_velho_tambem(banco):
    from prumo.broker.fila import pegar_proximo
    _integracao(banco)
    velho = _pedido(banco, prioridade=1, idade="15 minutes")
    _pedido(banco, prioridade=1, idade="14 minutes")         # também velho, mas espera
    _pedido(banco, prioridade=1)
    interativo = _pedido(banco, prioridade=0)
    ordem = [pegar_proximo(banco, "gpu-1")["id"] for _ in range(2)]
    # o lote de 15 min ganhou uma vaga; o de 14 min não passa o interativo enquanto ele roda
    assert ordem == [velho, interativo]


@precisa_pg
def test_dois_consumidores_juntos_nao_promovem_dois_lotes_velhos(banco):
    import psycopg
    from prumo.broker.fila import pegar_proximo
    _integracao(banco)
    velho = _pedido(banco, prioridade=1, idade="15 minutes")
    _pedido(banco, prioridade=1, idade="14 minutes")
    interativo = _pedido(banco, prioridade=0)
    primeiro = psycopg.connect(DSN)
    primeiro.execute("SELECT 1")                             # transação aberta: decide e não faz commit
    assert pegar_proximo(primeiro, "gpu-1")["id"] == velho
    resultado = []
    t = threading.Thread(target=lambda: resultado.append(pegar_proximo(
        psycopg.connect(DSN, autocommit=True), "gpu-2")["id"]))
    t.start()
    t.join(0.5)
    assert t.is_alive()                                      # espera a decisão do primeiro
    primeiro.commit()
    t.join(5)
    assert resultado == [interativo]                         # o segundo lote velho não passou
    primeiro.close()


@precisa_pg
def test_dois_consumidores_nunca_pegam_o_mesmo_pedido(banco):
    import psycopg
    from prumo.broker.fila import pegar_proximo
    _integracao(banco)
    for _ in range(200):
        _pedido(banco)
    pegos, trava = [], threading.Lock()

    def consumidor(nome):
        with psycopg.connect(DSN, autocommit=True) as conn:
            while (p := pegar_proximo(conn, nome)) is not None:
                with trava:
                    pegos.append(p["id"])

    threads = [threading.Thread(target=consumidor, args=(f"gpu-{i}",)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(pegos) == 200 and len(set(pegos)) == 200


@precisa_pg
def test_vaga_vencida_volta_para_a_fila_e_dono_antigo_nao_grava(banco):
    from prumo.broker.fila import concluir, pegar_proximo
    _integracao(banco)
    _pedido(banco)
    primeiro = pegar_proximo(banco, "gpu-1", prazo_s=0)      # vaga vence na hora
    segundo = pegar_proximo(banco, "gpu-2")
    assert segundo["id"] == primeiro["id"]
    assert not concluir(banco, primeiro, {"x": 1}, entrada=1, saida=1, ms=1)
    assert concluir(banco, segundo, {"x": 2}, entrada=1, saida=1, ms=1)
    linha = banco.execute("SELECT tentativas, descartes, resultado FROM pedido").fetchone()
    assert linha == (2, 1, {"x": 2})


@precisa_pg
def test_modelo_com_erro_volta_a_fila_e_desiste_na_terceira(banco):
    from prumo.broker.consumidor import processar_um

    def quebrado(tarefa, conteudo, **_):
        raise RuntimeError("Ollama respondeu 500")
    _integracao(banco)
    _pedido(banco)
    for esperado in ("pendente", "pendente", "erro"):
        assert processar_um(banco, "gpu-1", quebrado)
        assert banco.execute("SELECT status FROM pedido").fetchone()[0] == esperado
    assert not processar_um(banco, "gpu-1", quebrado)         # nada mais na fila


@precisa_pg
def test_api_token_tarefa_e_reaproveitamento(banco, monkeypatch):
    from fastapi.testclient import TestClient
    from prumo.broker import api
    from prumo.broker.consumidor import processar_um
    monkeypatch.setenv("BROKER_DSN", DSN)
    _integracao(banco, tarefas=["extrair_debitos"])
    cli = TestClient(api.app)
    h = {"Authorization": "Bearer prm_teste"}

    assert cli.post("/v1/pedidos", json={"tarefa": "x", "conteudo": {}},
                    headers={"Authorization": "Bearer errado"}).status_code == 401
    assert cli.post("/v1/pedidos", json={"tarefa": "assistente", "conteudo": {}},
                    headers=h).status_code == 403

    corpo = {"tarefa": "extrair_debitos", "conteudo": {"texto": OUTRO_LAYOUT}}
    primeiro = cli.post("/v1/pedidos", json=corpo, headers=h).json()
    assert primeiro["status"] == "pendente"
    processar_um(banco, "gpu-1", lambda tarefa, conteudo, **_: (json.loads(JSON_BOM), 812, 240))
    assert cli.get(f"/v1/pedidos/{primeiro['id']}", headers=h).json()["status"] == "concluido"

    segundo = cli.post("/v1/pedidos", json=corpo, headers=h).json()
    assert segundo["status"] == "reaproveitado"
    assert segundo["resultado"]["total_consolidado"] == "178684.20"
    visto = cli.get(f"/v1/pedidos/{segundo['id']}", headers=h).json()
    assert visto["resultado"]["total_consolidado"] == "178684.20"   # lido do pedido de origem
    consumo = banco.execute("SELECT sum(tokens_entrada) FROM pedido").fetchone()[0]
    assert consumo == 812                                  # o reaproveitado não gastou GPU

    from pathlib import Path
    sql = (Path(api.__file__).parent / "consumo.sql").read_text()
    linha = banco.execute(sql).fetchone()
    assert linha[:5] == ("parcelamentos", "extrair_debitos", 2, 1, 812)

    banco.execute("UPDATE integracao SET ativa = false")            # revogar
    assert cli.post("/v1/pedidos", json=corpo, headers=h).status_code == 401


@precisa_pg
def test_conteudo_some_depois_de_48h(banco):
    from prumo.broker.api import apagar_conteudo_antigo
    _integracao(banco)
    antigo = _pedido(banco, idade="60 hours")
    banco.execute("""UPDATE pedido SET status = 'concluido', resultado = '{}',
                     concluido_em = now() - interval '49 hours' WHERE id = %s""", (antigo,))
    _pedido(banco, idade="60 hours")          # ainda na fila: não pode perder o conteúdo
    assert apagar_conteudo_antigo(banco) == 1


# ---------- capítulo 4: documentos longos ----------

def _documento_longo(paginas=30, por_pagina=4):
    from decimal import Decimal
    from prumo.ia.explicar import formatar_reais
    textos, total, n = [], Decimal("0"), 0
    for p in range(paginas):
        linhas = ["DEMONSTRATIVO DE DÉBITOS PARCELADOS — continuação", "Tributo | Período | Saldo"]
        for _ in range(por_pagina):
            n += 1
            valor = Decimal(1000 + n * 37) + Decimal(n % 100) / 100
            total += valor
            linhas.append(f"{('IRPJ', 'CSLL', 'PIS', 'COFINS')[n % 4]} | {n % 12 + 1:02d}/2024 | "
                          f"{formatar_reais(valor).removeprefix('R$ ')}")
        textos.append("\n".join(linhas))
    textos[0] = "Proc. 13804.720001/2025-00 | consolidado em 15/01/2025 | 60 prestações\n" + textos[0]
    textos[-1] += f"\nValor consolidado: {formatar_reais(total).removeprefix('R$ ')}"
    return textos, total


def _modelo_fiel(trocar_no_bloco=None, falhar_uma_vez=None):
    import re as _re
    estado = {"falhou": False}

    def llm(prompt):
        if "cabeçalho" in prompt:
            total = _re.search(r"Valor consolidado: ([\d.]+,\d{2})", prompt).group(1)
            return json.dumps({"processo": "13804.720001/2025-00", "data_consolidacao": "2025-01-15",
                               "parcelas": 60, "total_consolidado": total.replace(".", "").replace(",", ".")})
        paginas = [int(p) for p in _re.findall(r"=== página (\d+) ===", prompt)]
        if falhar_uma_vez in paginas and not estado["falhou"]:
            estado["falhou"] = True
            return "desculpe, não consegui"
        debitos, pagina = [], None
        for linha in prompt.splitlines():
            m = _re.match(r"=== página (\d+) ===", linha)
            if m:
                pagina = int(m.group(1))
            m = _re.match(r"(\w+) \| (\d{2}/\d{4}) \| ([\d.]+,\d{2})$", linha)
            if m and pagina:
                valor = m.group(3).replace(".", "").replace(",", ".")
                if trocar_no_bloco in paginas and not debitos:
                    valor = valor[:-2] + valor[-1] + valor[-2]          # dois dígitos trocados
                debitos.append({"pagina": pagina, "tributo": m.group(1), "competencia": m.group(2),
                                "valor": valor})
        return json.dumps({"debitos": debitos})
    return llm


def test_documento_longo_lido_em_blocos_e_conferido():
    from prumo.leitura import blocos
    paginas, total = _documento_longo()
    divisao = blocos.dividir(paginas, orcamento=1_500)
    assert len(divisao) > 4 and divisao[0][0] == 0 and divisao[-1][1] == 30
    assert all(a[1] == b[0] for a, b in zip(divisao, divisao[1:]))        # sem buraco, sem sobreposição
    leitura = blocos.ler_longo(paginas, _modelo_fiel(), orcamento=1_500)
    assert leitura.aprovada and len(leitura.recibo.debitos) == 120
    assert leitura.recibo.total_consolidado == total


def test_bloco_com_valor_trocado_falha_e_so_ele_e_refeito():
    from prumo.leitura import blocos
    paginas, _ = _documento_longo()
    ruim = blocos.ler_longo(paginas, _modelo_fiel(trocar_no_bloco=13), orcamento=1_500)
    assert not ruim.aprovada and len(ruim.falhos) == 1 and "não está nela" in ruim.problemas[0]
    chamadas = []
    fiel = _modelo_fiel()
    refeita = blocos.ler_longo(paginas, lambda p: chamadas.append(p) or fiel(p),
                               orcamento=1_500, anterior=ruim)
    assert refeita.aprovada and len(refeita.recibo.debitos) == 120
    assert len(chamadas) == 2                                            # o bloco falho e o cabeçalho


def test_resposta_quebrada_tem_segunda_tentativa():
    from prumo.leitura import blocos
    paginas, _ = _documento_longo()
    assert blocos.ler_longo(paginas, _modelo_fiel(falhar_uma_vez=1), orcamento=1_500).aprovada


@precisa_pg
def test_avaliacao_pede_inferencia_nova_e_so_ela_pode(banco, monkeypatch):
    from fastapi.testclient import TestClient
    from prumo.broker import api
    from prumo.broker.consumidor import processar_um
    monkeypatch.setenv("BROKER_DSN", DSN)
    _integracao(banco, tarefas=["extrair_debitos"])
    _integracao(banco, token="prm_aval", tarefas=["extrair_debitos"], id="avaliacao", avaliacao=True)
    cli = TestClient(api.app)
    corpo = {"tarefa": "extrair_debitos", "conteudo": {"texto": OUTRO_LAYOUT}}

    nega = cli.post("/v1/pedidos", json={**corpo, "reaproveitar": False},
                    headers={"Authorization": "Bearer prm_teste"})
    assert nega.status_code == 403                       # produção não desliga o reaproveitamento

    h = {"Authorization": "Bearer prm_aval"}
    primeiro = cli.post("/v1/pedidos", json={**corpo, "reaproveitar": False}, headers=h).json()
    processar_um(banco, "gpu-1", lambda tarefa, conteudo, **_: (json.loads(JSON_BOM), 812, 240))
    segundo = cli.post("/v1/pedidos", json={**corpo, "reaproveitar": False}, headers=h).json()
    assert primeiro["status"] == segundo["status"] == "pendente"   # cada rodada vai ao modelo
    assert cli.post("/v1/pedidos", json=corpo, headers=h).json()["status"] == "reaproveitado"


def test_documento_longo_confere_o_cabecalho_e_muda_o_pedido_ao_refazer():
    from prumo.leitura import blocos
    paginas, _ = _documento_longo()
    fiel, pedidos = _modelo_fiel(trocar_no_bloco=13), []

    def llm(prompt):
        pedidos.append(prompt)
        resposta = fiel(prompt)
        if "cabeçalho" in prompt:                   # data de consolidação um dia depois
            return resposta.replace("2025-01-15", "2025-01-16")
        return resposta

    ruim = blocos.ler_longo(paginas, llm, orcamento=1_500)
    refeitos = [p for p in pedidos if "anterior recusada" in p]
    assert refeitos and "não está nela" in refeitos[0]   # a segunda tentativa leva o motivo
    assert not ruim.aprovada
    fiel = _modelo_fiel()                          # agora sem dígito trocado: só o cabeçalho erra
    certo = blocos.ler_longo(paginas, llm, orcamento=1_500)
    assert certo.recibo is not None and not certo.aprovada
    assert any("16/01/2025" in p for p in certo.problemas)


def test_broker_le_documento_longo_num_pedido_so(monkeypatch):
    from prumo import servicos
    paginas, _ = _documento_longo()
    fiel, chamadas = _modelo_fiel(), []

    def gerar(url, corpo, *, parcial, limite_s):
        chamadas.append(limite_s)
        return fiel(corpo["messages"][0]["content"]), 100, 20

    monkeypatch.setattr(servicos, "gerar_ollama", gerar)
    chamar = servicos.chamador("http://ollama", "qwen3:14b", {"num_ctx": 16384},
                               "qwen3:14b@sha256:a1b2c3")
    resultado, entrada, saida = chamar("ler_documento", {"paginas": paginas},
                                       parcial=lambda _: None, limite_s=600)
    assert resultado["aprovada"] and resultado["recibo"]["parcelas"] == 60
    assert resultado["modelo"] == "qwen3:14b@sha256:a1b2c3"     # quem de fato respondeu
    assert entrada == 100 * len(chamadas) and len(chamadas) >= 2
    assert all(b <= a for a, b in zip(chamadas, chamadas[1:]))   # um prazo só, que diminui
    simples, _, _ = chamar("extrair_debitos", {"prompt": "oi"}, parcial=lambda _: None,
                           limite_s=60)
    assert simples["modelo"] == "qwen3:14b@sha256:a1b2c3"


def test_bloco_e_limitado_tambem_pela_resposta():
    from prumo.leitura import blocos
    pagina = "\n".join(f"IRPJ | {m:02d}/2024 | 1.234,{m:02d}" for m in range(1, 41))  # 40 débitos
    assert blocos.saida_estimada(pagina) == 40 * blocos.TOKENS_POR_DEBITO
    divisao = blocos.dividir([pagina] * 6)
    assert all(fim - inicio <= 2 for inicio, fim in divisao)      # 3 páginas não cabem na resposta
    assert divisao[0] == (0, 2) and len(divisao) == 3
