"""Prumo ID: verificar tokens do provedor de identidade (capítulo 9).

O token é assinado pelo Keycloak. A aplicação confere a assinatura com as
chaves públicas do provedor (JWKS), guardadas em memória: nenhuma chamada
ao provedor por pedido. Chave nova (rotação) = recarrega, no máximo uma vez por minuto.
"""
import time
from collections.abc import Callable

import jwt


class TokenInvalido(Exception):
    pass


class SemAcesso(Exception):
    pass


class Verificador:
    def __init__(self, emissor: str, audiencia: str, obter_jwks: Callable[[], dict],
                 folga_s: int = 30, recarga_minima_s: float = 60,
                 idade_maxima_s: float = 600):
        self.emissor, self.audiencia, self.folga_s = emissor, audiencia, folga_s
        self._obter_jwks, self._chaves, self._revogadas = obter_jwks, {}, set()
        self._recarga_minima_s, self._ultima_carga = recarga_minima_s, float("-inf")
        self._idade_maxima_s, self._carregado_em = idade_maxima_s, float("-inf")

    def _carregar(self) -> None:
        self._ultima_carga = time.monotonic()
        try:
            jwks = self._obter_jwks()
        except Exception as erro:            # provedor fora: token não conferido = inválido
            raise TokenInvalido(f"chaves indisponíveis: {erro}") from erro
        chaves = {}
        for k in jwks.get("keys", []):
            kid = k.get("kid")
            if k.get("use", "sig") != "sig" or not kid or kid in self._revogadas:
                continue
            try:
                chaves[kid] = jwt.PyJWK(k).key
            except jwt.PyJWTError:            # tipo de chave que não usamos: ignora só ela
                continue
        self._chaves = chaves
        self._carregado_em = self._ultima_carga

    def esquecer(self, kid: str) -> None:
        """Emergência: a chave privada vazou.

        Sai do cache agora e não volta, mesmo que o JWKS ainda a liste."""
        self._revogadas.add(kid)
        self._chaves.pop(kid, None)

    def _chave(self, kid: str | None):
        if not kid:
            raise TokenInvalido("token sem kid")
        velho = time.monotonic() - self._carregado_em >= self._idade_maxima_s
        if (velho or kid not in self._chaves) and \
                time.monotonic() - self._ultima_carga >= self._recarga_minima_s:
            try:
                # chave nova chega; chave retirada sai em até 10 min
                self._carregar()
            except TokenInvalido:
                if kid not in self._chaves:  # provedor fora: só vale o que já estava no cache
                    raise
        if kid not in self._chaves:
            raise TokenInvalido(f"chave {kid!r} desconhecida")
        return self._chaves[kid]

    def verificar(self, token: str) -> dict:
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            return jwt.decode(token, self._chave(kid), algorithms=["RS256"],
                              audience=self.audiencia, issuer=self.emissor,
                              leeway=self.folga_s,
                              options={"require": ["exp", "iss", "aud"]})
        except jwt.PyJWTError as erro:
            raise TokenInvalido(str(erro)) from erro


# ---------- papéis em dois níveis ----------

NIVEIS = ("leitura", "operador", "supervisor", "admin")


def papel(claims: dict, app: str) -> str:
    """Nível da pessoa no app. O papel de realm diz se ela entra; o do client, o que faz."""
    if f"acesso-{app}" not in claims.get("realm_access", {}).get("roles", []):
        raise SemAcesso(f"sem acesso a {app}")
    do_app = claims.get("resource_access", {}).get(f"prumo-{app}", {}).get("roles", [])
    niveis = [n for n in NIVEIS if n in do_app]
    if not niveis:
        raise SemAcesso(f"sem papel em {app}")
    return niveis[-1]


def pode(claims: dict, app: str, minimo: str) -> bool:
    """Papéis cumulativos: quem é supervisor também pode o que o operador pode."""
    try:
        return NIVEIS.index(papel(claims, app)) >= NIVEIS.index(minimo)
    except SemAcesso:
        return False


# ---------- logout de todos os sistemas (back-channel) ----------

EVENTO_LOGOUT = "http://schemas.openid.net/event/backchannel-logout"


class Sessoes:
    """Sessões do app, por `sid` do provedor. Em produção, numa tabela."""

    def __init__(self):
        self._ativas: dict[str, str] = {}

    def abrir(self, sid: str, usuario: str) -> None:
        self._ativas[sid] = usuario

    def ativa(self, sid: str) -> bool:
        return sid in self._ativas

    def encerrar_por_aviso(self, verificador: Verificador, logout_token: str) -> str:
        """O provedor avisa: a sessão `sid` acabou. Devolve o sid encerrado."""
        claims = verificador.verificar(logout_token)
        if EVENTO_LOGOUT not in claims.get("events", {}):
            raise TokenInvalido("não é um aviso de logout")
        if "nonce" in claims:
            raise TokenInvalido("aviso de logout não pode ter nonce")
        sid = claims.get("sid")
        if not sid:
            raise TokenInvalido("aviso sem sid")
        self._ativas.pop(sid, None)
        return sid
