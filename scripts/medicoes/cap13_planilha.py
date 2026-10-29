"""Capítulo 13: o tamanho do que vai para o modelo (o mapa) e o tempo do cálculo.

Extratos gerados no formato do da Marta, com 90 e com 20 mil linhas. Tokens estimados a um
a cada três caracteres, a regra do capítulo 12. Mediana de sete cálculos de um plano com uma
soma filtrada e uma soma agrupada por mês. Não usa banco.
"""
import csv
import json
import random
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from prumo.agentes import planilha as pl
from scripts.medicoes._comum import cronometrar, mediana, ms

PLANO = {"calculos": [
    {"nome": "nao_identificados", "aba": "extrato", "operacao": "soma", "coluna": "Valor",
     "filtros": [{"coluna": "Situação", "op": "=", "valor": "não identificado"}]},
    {"nome": "por_mes", "aba": "extrato", "operacao": "soma", "coluna": "Valor",
     "agrupar_por": {"coluna": "Data", "por": "mes"}}]}
HISTORICOS = ["TED RECEBIDA", "PIX RECEBIDO", "TARIFA PACOTE", "PAGAMENTO FORNECEDOR",
              "DEPÓSITO", "BOLETO PAGO"]


def gerar(linhas: int, caminho: Path, sorteio: random.Random) -> None:
    inicio = datetime(2026, 10, 1)
    with caminho.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Data", "Histórico", "Valor", "Situação"])
        for i in range(linhas):
            valor = Decimal(sorteio.randint(100, 900_000)) / 100
            texto = f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            if sorteio.random() < 0.3:
                texto = f"({texto})"                       # débito entre parênteses
            situacao = sorteio.choice(["identificado"] * 3 + ["não identificado"])
            w.writerow([(inicio + timedelta(days=i * 31 // linhas)).strftime("%d/%m/%Y"),
                        f"{sorteio.choice(HISTORICOS)} {i}", texto, situacao])


def main() -> None:
    sorteio = random.Random(14)
    with tempfile.TemporaryDirectory() as pasta:
        for linhas in (90, 20_000):
            caminho = Path(pasta) / "extrato.csv"
            gerar(linhas, caminho, sorteio)
            abas = pl.ler(str(caminho))
            arquivo = len(caminho.read_text(encoding="utf-8")) // 3
            mapa = len(json.dumps(pl.mapa(abas), ensure_ascii=False)) // 3
            tempo = mediana(cronometrar(lambda: pl.calcular(PLANO, abas), 7))
            print(f"{linhas:>6} linhas: arquivo ~{arquivo} tokens, mapa ~{mapa} tokens, "
                  f"cálculo {ms(tempo)}")


if __name__ == "__main__":
    main()
