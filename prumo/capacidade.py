"""Estimativa de memória de GPU para um modelo no Ollama (capítulo 3).

Conta de guardanapo, não promessa: pesos + cache de atenção (KV) para
num_ctx tokens em cada pedido paralelo + uma folga de trabalho.
"""
from dataclasses import dataclass

GIB = 1024 ** 3


@dataclass(frozen=True)
class Modelo:
    nome: str
    pesos_gb: float      # tamanho do download no Ollama, em GB (10^9 bytes)
    camadas: int
    cabecas_kv: int
    dim_cabeca: int


QWEN3_14B = Modelo("qwen3:14b", 9.3, camadas=40, cabecas_kv=8, dim_cabeca=128)
QWEN25_7B = Modelo("qwen2.5:7b", 4.7, camadas=28, cabecas_kv=4, dim_cabeca=128)


def kv_por_token(m: Modelo, bytes_por_valor: int = 2) -> int:
    """Chave + valor, em cada camada, para cada cabeça KV (f16 = 2 bytes)."""
    return 2 * m.camadas * m.cabecas_kv * m.dim_cabeca * bytes_por_valor


def vram_gib(m: Modelo, num_ctx: int, num_parallel: int = 1,
             bytes_por_valor: int = 2, folga_gib: float = 0.75) -> float:
    pesos = m.pesos_gb * 1e9 / GIB
    kv = kv_por_token(m, bytes_por_valor) * num_ctx * num_parallel / GIB
    return pesos + kv + folga_gib


if __name__ == "__main__":
    for m in (QWEN3_14B, QWEN25_7B):
        for ctx, par in ((8192, 1), (16384, 1), (16384, 2), (32768, 1)):
            print(f"{m.nome:12} ctx={ctx:>6} paralelo={par}  ~{vram_gib(m, ctx, par):5.1f} GiB")
