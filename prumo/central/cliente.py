"""Cliente HTTP da Central de Eventos, para as aplicações em Python (capítulo 17).

Não é obrigatório: o contrato é HTTP e JSON. O Vértice, que não é Python, fala o mesmo contrato.
"""
import httpx


class CentralFora(Exception):
    pass


class Cliente:
    def __init__(self, url: str, obter_token, *, http=None, timeout_s: float = 10):
        self.url, self.obter_token = url.rstrip("/"), obter_token
        self.http, self.timeout_s = http or httpx, timeout_s

    def _chamar(self, metodo: str, caminho: str, **kw) -> dict:
        try:
            r = self.http.request(metodo, f"{self.url}{caminho}", timeout=self.timeout_s,
                                  headers={"Authorization": f"Bearer {self.obter_token()}"}, **kw)
        except httpx.TransportError as e:
            raise CentralFora(str(e)) from e
        if r.status_code >= 500:
            raise CentralFora(f"central respondeu {r.status_code}")
        r.raise_for_status()
        return r.json()

    def publicar(self, canal: str, eventos: list[dict]) -> list[int]:
        return self._chamar("POST", f"/v1/canais/{canal}/eventos", json={"eventos": eventos})["seqs"]

    def ler(self, canal: str, maximo: int = 100) -> list[dict]:
        return self._chamar("GET", f"/v1/inscricoes/{canal}/eventos", params={"maximo": maximo})["eventos"]

    def confirmar(self, canal: str, ate: int) -> int:
        return self._chamar("POST", f"/v1/inscricoes/{canal}/cursor", json={"ate": ate})["cursor"]
