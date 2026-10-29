from datetime import date
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from prumo.capacidade import QWEN25_7B, QWEN3_14B, kv_por_token, vram_gib
from prumo.ia import explicar as ex
from prumo.leitura.recibo import ler_recibo
from prumo.motor.rfb import Parcelamento, valor_parcela
from prumo.politica import Destino, EnvioNegado, autorizar
from scripts.carga import percentil
from scripts import fake_ollama

SELIC = {  # % ao mês, valores ilustrativos
    (2025, 2): D("0.99"), (2025, 3): D("0.96"), (2025, 4): D("1.06"),
    (2025, 5): D("1.14"), (2025, 6): D("1.14"), (2025, 7): D("1.10"),
    (2025, 8): D("1.28"), (2025, 9): D("1.16"), (2025, 10): D("1.22"),
    (2025, 11): D("1.28"), (2025, 12): D("1.05"), (2026, 1): D("1.22"),
}

RECIBO = """RECIBO DE CONSOLIDAÇÃO DE PARCELAMENTO
Processo: 13804.720001/2025-00
Contribuinte: Metalúrgica Ferraz Ltda.
Data da consolidação: 15/01/2025
Quantidade de parcelas: 60
DÉBITOS
  IRPJ   03/2024   98.420,15
  CSLL   03/2024   54.103,90
  PIS    06/2024   26.160,15
TOTAL CONSOLIDADO: 178.684,20
"""


def parcelamento():
    r = ler_recibo(RECIBO)
    return Parcelamento(r.total_consolidado, D("0"), r.parcelas, r.data_consolidacao)


def test_recibo_fecha():
    r = ler_recibo(RECIBO)
    assert r.conferir() == []
    assert r.total_consolidado == D("178684.20")


def test_recibo_com_debito_faltando_nao_fecha():
    r = ler_recibo(RECIBO.replace("  PIS    06/2024   26.160,15\n", ""))
    assert "diferente do total" in r.conferir()[0]


def test_layout_desconhecido():
    with pytest.raises(ValueError, match="layout desconhecido"):
        ler_recibo("Outro layout qualquer")


def test_parcela_14_bate_com_o_darf():
    p = parcelamento()
    assert p.parcela_base == D("2978.07")
    assert valor_parcela(p, date(2026, 2, 27), SELIC) == D("3412.87")


def test_primeira_parcela_so_tem_o_um_por_cento():
    assert valor_parcela(parcelamento(), date(2025, 1, 31), SELIC) == D("3007.85")


def test_selic_faltando_para():
    with pytest.raises(KeyError, match="não cadastrada"):
        valor_parcela(parcelamento(), date(2026, 4, 30), SELIC)


CALCULO = {"Parcela 14": D("3412.87"), "Saldo no longo prazo": D("138547.12")}


def test_formatar_reais():
    assert ex.formatar_reais(D("138547.12")) == "R$ 138.547,12"


def _explicar(modo, monkeypatch):
    monkeypatch.setenv("FAKE_OLLAMA_MODO", modo)
    cliente = TestClient(fake_ollama.app)
    monkeypatch.setattr(ex.httpx, "post", lambda url, json, timeout: cliente.post("/api/chat", json=json))
    return ex.explicar("Quanto é a parcela 14?", CALCULO, url="", modelo="qwen3:14b")


def test_explicacao_fiel_passa(monkeypatch):
    assert "R$ 3.412,87" in _explicar("fiel", monkeypatch)


def test_explicacao_com_digito_trocado_e_recusada(monkeypatch):
    with pytest.raises(ex.ExplicacaoRecusada, match="3.412,78"):
        _explicar("distraido", monkeypatch)


def test_politica():
    autorizar("ler_documento", Destino.LOCAL)
    with pytest.raises(EnvioNegado, match="nunca sai"):
        autorizar("ler_documento", Destino.EXTERNO, aceite_registrado=True)
    with pytest.raises(EnvioNegado, match="aceite"):
        autorizar("assistente", Destino.EXTERNO)
    autorizar("assistente", Destino.EXTERNO, aceite_registrado=True)
    with pytest.raises(EnvioNegado, match="sem regra"):
        autorizar("resumo_novo", Destino.EXTERNO, aceite_registrado=True)


def test_capacidade():
    assert kv_por_token(QWEN3_14B) == 163_840
    assert 11.5 < vram_gib(QWEN3_14B, 16384) < 12.5
    assert vram_gib(QWEN3_14B, 16384, 2) > 14
    assert vram_gib(QWEN25_7B, 16384) < 6.5


def test_percentil():
    assert percentil([float(i) for i in range(1, 101)], 95) == 95.0
