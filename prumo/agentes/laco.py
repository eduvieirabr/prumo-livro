"""Um agente é um laço (capítulo 11).

Uma lista de passos, um contexto que cresce e um estado gravado no banco
depois de cada passo. Sem framework: cabe numa leitura.

Quem roda tem a posse da execução, como o consumidor tem a vaga no capítulo 6:
um token, um batimento enquanto o passo demora, e gravação só com o token vigente.
"""
import json
import re
import threading
import time
import uuid
from contextlib import contextmanager
from collections.abc import Callable
from dataclasses import dataclass, field

from psycopg.types.json import Jsonb


@dataclass(frozen=True)
class Passo:
    nome: str                 # a saída do passo fica no contexto com este nome
    tipo: str                 # prompt, skill, consulta_memoria, fluxo_n8n
    config: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Agente:
    id: str
    versao: int
    passos: tuple[Passo, ...]

    def __post_init__(self):
        if not 1 <= len(self.passos) <= MAX_PASSOS:
            raise ValueError(f"um agente tem de 1 a {MAX_PASSOS} passos")
        if len({p.nome for p in self.passos}) != len(self.passos):
            raise ValueError("dois passos com o mesmo nome")


class Aguardando(Exception):
    """O passo precisa de algo que só uma pessoa pode dar."""

    def __init__(self, falta: str):
        super().__init__(falta)
        self.falta = falta


MAX_PASSOS = 10
PARADA_HA = "15 minutes"     # 'rodando' sem notícia há mais que isso = processo morreu
BATIMENTO_S = 60             # enquanto um passo demora, a posse é renovada a cada minuto

# executor(passo, contexto) -> saída do passo (dict)
Executor = Callable[[Passo, dict], dict]


def iniciar(conn, agente: Agente, dono: str, entradas: dict) -> str:
    return str(conn.execute(
        """INSERT INTO execucao (agente, versao, dono, contexto)
           VALUES (%s, %s, %s, %s) RETURNING id""",
        (agente.id, agente.versao, dono, Jsonb({"entradas": entradas}))).fetchone()[0])


class PosseTomada(Exception):
    """Outro processo assumiu a execução. Quem perdeu a posse não grava mais nada."""


def _gravar(conn, execucao: str, posse, **campos) -> None:
    sets = ", ".join(f"{k} = %({k})s" for k in campos)
    valores = {k: Jsonb(v) if k == "contexto" else v for k, v in campos.items()}
    gravou = conn.execute(
        f"UPDATE execucao SET {sets}, atualizado_em = now()"
        " WHERE id = %(id)s AND posse = %(posse)s",
        {**valores, "id": execucao, "posse": posse}).rowcount
    if not gravou:
        raise PosseTomada(execucao)


@contextmanager
def _batimento(conn, execucao: str, posse):
    """Renova a posse enquanto o passo roda. Renovação que falha é posse perdida."""
    parar, perdeu = threading.Event(), threading.Event()

    def bater():
        while not parar.wait(BATIMENTO_S):
            try:
                renovou = conn.execute("UPDATE execucao SET atualizado_em = now()"
                                       " WHERE id = %s AND posse = %s",
                                       (execucao, posse)).rowcount
            except Exception:
                renovou = 0
            if not renovou:
                perdeu.set()
                return
    t = threading.Thread(target=bater, daemon=True)
    t.start()
    try:
        yield
    finally:
        parar.set()
        t.join()
    if perdeu.is_set():          # o passo terminou, mas a execução já é de outro
        raise PosseTomada(execucao)


def rodar(conn, execucao: str, agente: Agente, executores: dict[str, Executor],
          *, visiveis, um_passo: bool = False) -> str:
    """Roda do passo gravado; ao retomar, confere o contexto com a permissão de hoje."""
    posse = uuid.uuid4()
    tomada = conn.execute(
        """UPDATE execucao SET status = 'rodando', posse = %s, aguardando = NULL,
                  erro = NULL, atualizado_em = now()
            WHERE id = %s AND agente = %s AND versao = %s
              AND (status <> 'rodando' OR atualizado_em < now() - %s::interval)
        RETURNING passo, contexto""",
        (posse, execucao, agente.id, agente.versao, PARADA_HA)).fetchone()
    if tomada is None:
        raise ValueError("execução já rodando, ou fixada em outro agente ou versão")
    passo_atual, contexto = tomada
    try:
        volta = _primeiro_invisivel(agente, passo_atual, contexto, visiveis)
        if volta is not None:                     # retomada com permissão perdida: refaz dali
            passo_atual, contexto = volta, _ate(agente, volta, contexto)
            _gravar(conn, execucao, posse, passo=passo_atual, contexto=contexto)
        while passo_atual < len(agente.passos):
            passo, inicio = agente.passos[passo_atual], time.monotonic()
            try:
                with _batimento(conn, execucao, posse):
                    saida = executores[passo.tipo](passo, contexto)
            except Aguardando as a:
                _gravar(conn, execucao, posse, status="aguardando", aguardando=a.falta)
                return "aguardando"
            except Exception as e:
                with conn.transaction():
                    _gravar(conn, execucao, posse, status="erro", erro=str(e))
                    _registrar(conn, execucao, passo_atual, passo, None,
                               f"{type(e).__name__}: {e}", inicio)
                return "erro"
            contexto = {**contexto, passo.nome: saida}
            passo_atual += 1
            # passo e estado gravados juntos, só com a posse
            with conn.transaction():
                _gravar(conn, execucao, posse, passo=passo_atual, contexto=contexto)
                _registrar(conn, execucao, passo_atual - 1, passo, saida, None, inicio)
            if um_passo and passo_atual < len(agente.passos):
                _gravar(conn, execucao, posse, status="pausada")
                return "pausada"
        _gravar(conn, execucao, posse, status="concluida")
        return "concluida"
    except PosseTomada:
        return "posse perdida"                    # o resultado deste processo vai para o lixo


def _registrar(conn, execucao, indice, passo, saida, erro, inicio) -> None:
    conn.execute(
        """INSERT INTO passo_execucao (execucao, indice, nome, tipo, saida, erro, ms)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (execucao, indice, passo.nome, passo.tipo, Jsonb(saida) if saida is not None else None,
         erro, int((time.monotonic() - inicio) * 1000)))


def continuar_daqui(conn, execucao: str, agente: Agente, indice: int, quem: str,
                    *, visiveis) -> str:
    """Nova execução que reaproveita as saídas dos passos antes de `indice`.

    Pode ser com a mesma versão do agente ou com uma versão nova: os passos
    anteriores precisam ter os mesmos nomes, ou o reaproveitamento é recusado.
    """
    dono, contexto, id_agente = conn.execute(
        "SELECT dono, contexto, agente FROM execucao WHERE id = %s", (execucao,)).fetchone()
    if quem != dono:
        raise PermissionError("só quem rodou a execução pode continuá-la")
    if id_agente != agente.id:
        raise ValueError("a execução de origem é de outro agente")
    mantidos = [p.nome for p in agente.passos[:indice]]
    faltando = [n for n in mantidos if n not in contexto]
    if faltando:
        raise ValueError(f"a execução de origem não tem as saídas de {faltando}")
    for nome in mantidos:                         # reaproveitar só o que a pessoa ainda vê
        usados = itens_usados(contexto[nome])
        if usados and usados - set(visiveis(usados)):
            raise PermissionError(f"a saída de {nome!r} usa itens que você não vê mais; "
                                  "continue de um passo anterior")
    novo = {"entradas": contexto["entradas"], **{n: contexto[n] for n in mantidos}}
    return str(conn.execute(
        """INSERT INTO execucao (agente, versao, dono, passo, contexto, origem)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        (agente.id, agente.versao, dono, indice, Jsonb(novo), execucao)).fetchone()[0])


def itens_usados(saida) -> set[int]:
    """Os itens da memória que sustentam a saída de um passo (as fontes da consulta)."""
    if not isinstance(saida, dict):
        return set()
    return {f["item"] for f in saida.get("fontes", []) if isinstance(f, dict) and "item" in f}


def _primeiro_invisivel(agente: Agente, passo: int, contexto: dict, visiveis) -> int | None:
    for i, p in enumerate(agente.passos[:passo]):
        usados = itens_usados(contexto.get(p.nome))
        if usados and usados - set(visiveis(usados)):
            return i
    return None


def _ate(agente: Agente, indice: int, contexto: dict) -> dict:
    """O contexto como estava antes do passo `indice`."""
    return {"entradas": contexto["entradas"],
            **{q.nome: contexto[q.nome] for q in agente.passos[:indice]}}


def revalidar(conn, execucao: str, agente: Agente, visiveis) -> int | None:
    """Antes de continuar uma execução parada: quem a roda AGORA ainda vê tudo o que a
    memória entregou a ela? visiveis(ids) -> set, chamada como a pessoa.

    Se não vê, o contexto volta para antes do primeiro passo afetado, que roda de novo,
    agora com a permissão de hoje. Devolve o índice desse passo, ou None. Execução
    concluída é histórico e não muda: para refazer, `continuar_daqui`.
    """
    with conn.transaction():
        passo, contexto, status = conn.execute(
            "SELECT passo, contexto, status FROM execucao WHERE id = %s FOR UPDATE",
            (execucao,)).fetchone()
        if status in ("rodando", "concluida"):
            raise ValueError(f"revalidar só execução parada, e esta está {status}")
        i = _primeiro_invisivel(agente, passo, contexto, visiveis)
        if i is not None:
            conn.execute("""UPDATE execucao SET passo = %s, contexto = %s,
                                   status = 'pausada', atualizado_em = now()
                             WHERE id = %s""",
                         (i, Jsonb(_ate(agente, i, contexto)), execucao))
        return i


def historico(conn, execucao: str, visiveis) -> list[dict]:
    """O passo a passo de uma execução, como a pessoa pode vê-lo hoje.

    A saída de um passo que usou itens que ela não vê mais não é mostrada.
    """
    linhas = conn.execute(
        """SELECT indice, nome, tipo, saida, erro, ms FROM passo_execucao
            WHERE execucao = %s ORDER BY em, indice""", (execucao,)).fetchall()
    passos = []
    for indice, nome, tipo, saida, erro, ms in linhas:
        usados = itens_usados(saida)
        if usados and usados - set(visiveis(usados)):
            saida = {"oculta": "usa itens da memória que você não vê"}
        passos.append({"indice": indice, "nome": nome, "tipo": tipo, "saida": saida,
                       "erro": erro, "ms": ms})
    return passos


def execucao_raiz(conn, execucao: str) -> str:
    """A primeira execução de uma cadeia de "continuar daqui"."""
    return str(conn.execute(
        """WITH RECURSIVE cadeia (id, origem) AS (
               SELECT id, origem FROM execucao WHERE id = %s
               UNION ALL
               SELECT e.id, e.origem FROM execucao e JOIN cadeia c ON e.id = c.origem)
           SELECT id FROM cadeia WHERE origem IS NULL""", (execucao,)).fetchone()[0])


# ---------- preencher o prompt com o que veio antes ----------

_MARCA_DADOS = re.compile(r"<(\s*/?\s*dados)", re.I)


def _dado(nome: str, valor) -> str:
    texto = valor if isinstance(valor, str) else json.dumps(valor, ensure_ascii=False)
    texto = _MARCA_DADOS.sub(r"&lt;\1", texto)     # </dados>, </DADOS>, < / dados>...
    return f'<dados origem="{nome}">\n{texto}\n</dados>'


def preencher(modelo: str, contexto: dict) -> str:
    """Troca {{nome}} pela saída do passo `nome`, sempre como dado delimitado.

    O texto de um passo anterior pode conter instruções escritas por qualquer
    pessoa (um anexo, uma resposta de fluxo). Ele entra marcado como dado, e as
    marcas que ele mesmo traz são neutralizadas.
    """
    def troca(m):
        caminho = m.group(1).split(".")
        valor = contexto
        for parte in caminho:
            if not isinstance(valor, dict) or parte not in valor:
                raise Aguardando(f"falta o valor de {m.group(1)}")
            valor = valor[parte]
        return _dado(m.group(1), valor)
    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", troca, modelo)
