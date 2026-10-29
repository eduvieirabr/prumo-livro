"""Central de Eventos: aplicações, canais, inscrições e o cursor (capítulos 16 e 17).

Quem publica é dono do canal. Quem lê precisa de inscrição e de nível para a
classificação do canal. Cada canal numera os eventos sem buracos, na ordem do
commit; cada inscrição guarda até onde confirmou. Nada aqui sabe de contabilidade.
"""
import hashlib
import json
import re
from datetime import date, datetime

from psycopg.types.json import Jsonb

from prumo.agentes.formulario import dv_cnpj

NIVEIS = ("interno", "restrito", "confidencial")
TIPOS = ("texto", "inteiro", "decimal", "data", "cnpj", "booleano")
MAX_LOTE = 500
MAX_LEITURA = 500


class SemPermissao(Exception):
    pass


class EventoInvalido(Exception):
    pass


class CanalInexistente(Exception):
    pass


class EventosPerdidos(Exception):
    """O cursor ficou para trás da retenção: há eventos que esta inscrição nunca vai ler."""

    def __init__(self, cursor: int, primeiro: int):
        super().__init__(f"cursor em {cursor}, primeiro evento disponível é {primeiro}")
        self.cursor, self.primeiro = cursor, primeiro


# ---------- cadastro: só o administrador aprova ----------

def cadastrar_aplicacao(conn, ident: str, nome: str, cliente_sso: str) -> None:
    conn.execute("INSERT INTO central.aplicacao (id, nome, cliente_sso) VALUES (%s, %s, %s)",
                 (ident, nome, cliente_sso))


def aprovar_aplicacao(conn, ident: str, admin: str, nivel: str = "interno") -> None:
    if nivel not in NIVEIS:
        raise EventoInvalido(f"nível {nivel!r} não existe")
    conn.execute("""UPDATE central.aplicacao SET aprovada_em = now(), aprovada_por = %s, nivel = %s
                     WHERE id = %s""", (admin, nivel, ident))


def _aprovada(conn, ident: str) -> str:
    linha = conn.execute("SELECT nivel, aprovada_em FROM central.aplicacao WHERE id = %s",
                         (ident,)).fetchone()
    if linha is None or linha[1] is None:
        raise SemPermissao(f"aplicação {ident!r} não cadastrada ou não aprovada")
    return linha[0]


def criar_canal(conn, dono: str, nome: str, classificacao: str, esquema: dict) -> None:
    _aprovada(conn, dono)
    if classificacao not in NIVEIS:
        raise EventoInvalido(f"classificação {classificacao!r} não existe")
    campos = {**esquema.get("obrigatorios", {}), **esquema.get("opcionais", {})}
    if not campos or any(t not in TIPOS for t in campos.values()):
        raise EventoInvalido(f"esquema precisa de campos com tipos de {TIPOS}")
    conn.execute("""INSERT INTO central.canal (nome, dono, classificacao, esquema)
                    VALUES (%s, %s, %s, %s)""", (nome, dono, classificacao, Jsonb(esquema)))


def inscrever(conn, aplicacao: str, canal: str, *, desde: str = "inicio") -> None:
    with conn.transaction():
        nivel = _aprovada(conn, aplicacao)
        linha = conn.execute("SELECT classificacao, ultimo_seq FROM central.canal WHERE nome = %s",
                             (canal,)).fetchone()
        if linha is None:
            raise CanalInexistente(f"canal {canal!r} não existe")
        if NIVEIS.index(linha[0]) > NIVEIS.index(nivel):
            raise SemPermissao(f"{aplicacao} não pode ler canal {linha[0]}")
        primeiro = _primeiro(conn, canal, linha[1])
        cursor = linha[1] if desde == "agora" else primeiro - 1
        conn.execute("""INSERT INTO central.inscricao (aplicacao, canal, cursor, entregue_ate)
                        VALUES (%s, %s, %s, %s)""", (aplicacao, canal, cursor, cursor))


def _primeiro(conn, canal: str, ultimo_seq: int) -> int:
    menor = conn.execute("SELECT min(seq) FROM central.evento WHERE canal = %s", (canal,)).fetchone()[0]
    return menor if menor is not None else ultimo_seq + 1


# ---------- o evento ----------

def _confere(valor, tipo: str) -> bool:
    if tipo == "texto":
        return isinstance(valor, str) and 0 < len(valor) <= 2_000 and "\x00" not in valor
    if tipo == "inteiro":
        return type(valor) is int
    if tipo == "booleano":
        return type(valor) is bool
    if tipo == "decimal":                              # dinheiro em JSON é texto: "3412.87"
        return (isinstance(valor, str) and valor != "-0.00"
                and re.fullmatch(r"-?(0|[1-9][0-9]{0,11})\.[0-9]{2}", valor) is not None)
    if tipo == "data":
        try:
            return isinstance(valor, str) and date.fromisoformat(valor).isoformat() == valor
        except ValueError:
            return False
    return (isinstance(valor, str) and re.fullmatch(r"[0-9A-Z]{12}[0-9]{2}", valor) is not None
            and len(set(valor)) > 1 and dv_cnpj(valor[:12]) == valor[12:])   # sem pontuação


def conferir_evento(evento: dict, esquema: dict) -> list[str]:
    """Erros do evento contra o esquema do canal. Lista vazia = pode publicar."""
    if not isinstance(evento, dict):
        return ["o evento não é um objeto"]
    erros = []
    if not _confere(evento.get("id"), "texto") or len(evento["id"]) > 100:
        erros.append("id obrigatório, texto de até 100 caracteres")
    if not _confere(evento.get("assunto"), "texto"):
        erros.append("assunto obrigatório")
    if evento.get("empresa") is not None and not _confere(evento["empresa"], "cnpj"):
        erros.append("empresa precisa ser um CNPJ válido")
    dados = evento.get("dados")
    if not isinstance(dados, dict):
        return erros + ["dados precisa ser um objeto"]
    obrig, opc = esquema.get("obrigatorios", {}), esquema.get("opcionais", {})
    for campo, tipo in obrig.items():
        if campo not in dados:
            erros.append(f"falta o campo {campo!r}")
        elif not _confere(dados[campo], tipo):
            erros.append(f"{campo!r} precisa ser {tipo}")
    for campo, valor in dados.items():
        if campo in opc and valor is not None and not _confere(valor, opc[campo]):
            erros.append(f"{campo!r} precisa ser {opc[campo]}")
        elif campo not in obrig and campo not in opc:
            erros.append(f"campo {campo!r} não está no esquema do canal")
    return erros


def _resumo(ev: dict) -> str:
    canonico = json.dumps([ev["assunto"], ev.get("empresa"), ev["dados"]],
                          sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonico.encode()).hexdigest()


def publicar(conn, aplicacao: str, canal: str, eventos: list[dict]) -> list[int]:
    """Lote inteiro ou nada. Devolve o número de cada evento; republicar devolve o mesmo."""
    if not isinstance(eventos, list) or not 1 <= len(eventos) <= MAX_LOTE:
        raise EventoInvalido(f"um lote tem de 1 a {MAX_LOTE} eventos")
    with conn.transaction():
        _aprovada(conn, aplicacao)
        # FOR UPDATE: um publicador por canal por vez
        linha = conn.execute(
            "SELECT dono, esquema FROM central.canal WHERE nome = %s FOR UPDATE",
            (canal,)).fetchone()
        if linha is None:
            raise CanalInexistente(f"canal {canal!r} não existe")
        if linha[0] != aplicacao:
            raise SemPermissao("só o dono do canal publica nele")
        erros = [f"evento {i + 1}: {e}" for i, ev in enumerate(eventos)
                 for e in conferir_evento(ev, linha[1])]
        if erros:
            raise EventoInvalido("; ".join(erros[:20]))
        numeros = []
        for ev in eventos:
            resumo = _resumo(ev)
            ja = conn.execute(
                "SELECT seq, resumo FROM central.evento_id WHERE canal = %s AND id = %s",
                (canal, ev["id"])).fetchone()
            if ja is not None and ja[1] != resumo:
                raise EventoInvalido(f"id {ev['id']!r} já publicado com outro conteúdo")
            if ja is None:
                ja = conn.execute(
                    """UPDATE central.canal SET ultimo_seq = ultimo_seq + 1
                        WHERE nome = %s RETURNING ultimo_seq""", (canal,)).fetchone()
                conn.execute(
                    """INSERT INTO central.evento (canal, seq, id, assunto, empresa, dados)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (canal, ja[0], ev["id"], ev["assunto"], ev.get("empresa"),
                     Jsonb(ev["dados"])))
                conn.execute(
                    """INSERT INTO central.evento_id (canal, id, seq, resumo)
                       VALUES (%s, %s, %s, %s)""", (canal, ev["id"], ja[0], resumo))
            numeros.append(ja[0])
    return numeros


# ---------- ler e confirmar ----------

def ler(conn, aplicacao: str, canal: str, maximo: int = 100) -> list[dict]:
    """Os próximos eventos depois do cursor. Ler de novo sem confirmar devolve os mesmos."""
    maximo = max(1, min(maximo, MAX_LEITURA))
    with conn.transaction():
        nivel = _aprovada(conn, aplicacao)
        linha = conn.execute(
            """SELECT i.cursor, c.classificacao, c.ultimo_seq FROM central.inscricao i
                 JOIN central.canal c ON c.nome = i.canal
                WHERE i.aplicacao = %s AND i.canal = %s FOR UPDATE OF i""",
            (aplicacao, canal)).fetchone()
        if linha is None:
            raise SemPermissao(f"{aplicacao} não está inscrita em {canal}")
        cursor, classificacao, ultimo = linha
        if NIVEIS.index(classificacao) > NIVEIS.index(nivel):  # o nível pode ter baixado depois
            raise SemPermissao(f"{aplicacao} não pode mais ler canal {classificacao}")
        primeiro = _primeiro(conn, canal, ultimo)
        if cursor + 1 < primeiro:
            raise EventosPerdidos(cursor, primeiro)
        linhas = conn.execute(
            """SELECT seq, id, assunto, empresa, dados, publicado_em FROM central.evento
                WHERE canal = %s AND seq > %s ORDER BY seq LIMIT %s""",
            (canal, cursor, maximo)).fetchall()
        if linhas:
            conn.execute("""UPDATE central.inscricao SET entregue_ate = greatest(entregue_ate, %s)
                             WHERE aplicacao = %s AND canal = %s""", (linhas[-1][0], aplicacao, canal))
    return [{"seq": s, "id": i, "assunto": a, "empresa": e, "dados": d, "publicado_em": p.isoformat(),
             "classificacao": classificacao} for s, i, a, e, d, p in linhas]   # viaja com o evento


def confirmar(conn, aplicacao: str, canal: str, ate: int) -> int:
    """Move o cursor para `ate`. Só para a frente, e só até o que já foi entregue."""
    linha = conn.execute(
        """UPDATE central.inscricao SET cursor = %s
            WHERE aplicacao = %s AND canal = %s AND %s BETWEEN cursor AND entregue_ate
        RETURNING cursor""", (ate, aplicacao, canal, ate)).fetchone()
    if linha is None:
        raise EventoInvalido("só se confirma o que já foi entregue, e o cursor não volta")
    return linha[0]


# ---------- quem depende de quem ----------

def quem_le(conn, canal: str) -> list[dict]:
    """As inscrições do canal e quanto cada uma está atrasada."""
    return [{"aplicacao": a, "cursor": cur, "atraso": ult - cur} for a, cur, ult in conn.execute(
        """SELECT i.aplicacao, i.cursor, c.ultimo_seq FROM central.inscricao i
             JOIN central.canal c ON c.nome = i.canal WHERE i.canal = %s ORDER BY i.aplicacao""",
        (canal,)).fetchall()]


def limpar(conn, agora: datetime) -> int:
    """Apaga o que passou da retenção de cada canal, confirmado ou não, sempre do começo:
    nunca fica um buraco no meio, porque apaga até o maior número vencido."""
    return conn.execute(
        """DELETE FROM central.evento e
            WHERE e.seq <= (SELECT max(v.seq) FROM central.evento v JOIN central.canal c ON c.nome = v.canal
                             WHERE v.canal = e.canal AND v.publicado_em < %s - c.retencao)""",
        (agora,)).rowcount


def aceitar_perda(conn, aplicacao: str, canal: str, admin: str) -> int:
    """Depois da reconciliação: o administrador aceita a perda e a inscrição volta a andar."""
    with conn.transaction():
        linha = conn.execute(
            """SELECT i.cursor, c.ultimo_seq FROM central.inscricao i JOIN central.canal c ON c.nome = i.canal
                WHERE i.aplicacao = %s AND i.canal = %s FOR UPDATE OF i""", (aplicacao, canal)).fetchone()
        if linha is None:
            raise SemPermissao(f"{aplicacao} não está inscrita em {canal}")
        novo = _primeiro(conn, canal, linha[1]) - 1
        if novo <= linha[0]:
            raise EventoInvalido("não há perda a aceitar")
        conn.execute("""UPDATE central.inscricao SET cursor = %s, entregue_ate = %s
                         WHERE aplicacao = %s AND canal = %s""", (novo, novo, aplicacao, canal))
        conn.execute("""INSERT INTO central.perda_aceita (aplicacao, canal, de, ate, por)
                        VALUES (%s, %s, %s, %s, %s)""", (aplicacao, canal, linha[0], novo, admin))
    return novo
