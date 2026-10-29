"""Agendar em nome de alguém (capítulo 14).

Quando roda: regras de recorrência no fuso de São Paulo, com dias úteis e
feriados. Em nome de quem: quem agendou, conferido antes de cada vez.
Quem roda: um agendador só, garantido por uma trava no PostgreSQL.
"""
import os
import re
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime, time, timedelta
from time import monotonic
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

FUSO = ZoneInfo("America/Sao_Paulo")
TRAVA_AGENDADOR = 7_140_001                       # número qualquer, o mesmo em todos os processos
RETENTATIVAS = (timedelta(minutes=15), timedelta(hours=1))
FALHAS_PARA_SUSPENDER = 3
RODANDO_NO_MAXIMO = timedelta(minutes=10)         # "rodando" sem sinal de vida há isso: morreu
BATIMENTO_S = 60                                  # o despacho renova o sinal das que rodam
PARALELO = 8                                      # agentes do agendador rodando ao mesmo tempo
HORIZONTE = 400                                   # dias: regra sem data nesse prazo é regra quebrada
TIPOS = ("todo_dia", "dias_uteis", "semana", "dia_do_mes", "n_dia_util")


# ---------- calendário ----------

def pascoa(ano: int) -> date:
    """Algoritmo de Meeus/Jones/Butcher (calendário gregoriano)."""
    a, b, c = ano % 19, ano // 100, ano % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes, dia = divmod(h + l - 7 * m + 114, 31)
    return date(ano, mes, dia + 1)


def ler_extras(texto: str) -> list[tuple[int | None, int, int]]:
    """FERIADOS_EXTRAS: "07-09" vale todo ano; "2027-02-08" vale uma vez. Erro aqui para a subida."""
    extras = []
    for item in filter(None, (t.strip() for t in texto.split(","))):
        m = re.fullmatch(r"(?:(\d{4})-)?(\d{2})-(\d{2})", item)
        if not m:
            raise ValueError(f"FERIADOS_EXTRAS: {item!r} não é MM-DD nem AAAA-MM-DD")
        ano, mes, dia = (int(g) if g else None for g in m.groups())
        date(ano or 2024, mes, dia)                   # 2024 é bissexto: 02-29 passa
        extras.append((ano, mes, dia))
    return extras


EXTRAS = ler_extras(os.getenv("FERIADOS_EXTRAS", ""))     # na importação: falha na subida, não às 8h


def feriados(ano: int) -> set[date]:
    fixos = {(1, 1), (4, 21), (5, 1), (9, 7), (10, 12), (11, 2), (11, 15), (11, 20), (12, 25)}
    dias = {date(ano, m, d) for m, d in fixos}
    dias.add(pascoa(ano) - timedelta(days=2))     # Sexta-feira Santa
    for a, m, d in EXTRAS:                        # municipais, estaduais, ponto facultativo
        if a in (None, ano):
            try:
                dias.add(date(ano, m, d))
            except ValueError:
                pass                          # 02-29 recorrente, em ano que não é bissexto
    return dias


def dia_util(d: date) -> bool:
    return d.weekday() < 5 and d not in feriados(d.year)


def n_dia_util(ano: int, mes: int, n: int) -> date:
    d, contados = date(ano, mes, 1), 0
    while d.month == mes:
        if dia_util(d):
            contados += 1
            if contados == n:
                return d
        d += timedelta(days=1)
    raise ValueError(f"{mes:02d}/{ano} não tem {n} dias úteis")


# ---------- quando roda ----------

def validar_regra(regra: dict) -> None:
    """Na gravação e antes de cada uso. Regra inválida nunca chega ao laço de datas."""
    if not isinstance(regra, dict) or regra.get("tipo") not in TIPOS:
        raise ValueError(f"tipo de regra desconhecido: {regra!r}")
    hora = regra.get("hora", "08:00")
    if not isinstance(hora, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", hora):
        raise ValueError(f"hora inválida: {hora!r}")
    tipo = regra["tipo"]
    if tipo == "semana":
        dias = regra.get("dias")
        if not isinstance(dias, list) or not dias or not all(type(x) is int and 0 <= x <= 6 for x in dias):
            raise ValueError("dias da semana vão de 0 (segunda) a 6 (domingo)")
    if tipo == "dia_do_mes" and not (type(regra.get("dia")) is int and 1 <= regra["dia"] <= 31):
        raise ValueError("dia do mês vai de 1 a 31")
    if tipo == "n_dia_util" and not (type(regra.get("n")) is int and 1 <= regra["n"] <= 20):
        raise ValueError("o N-ésimo dia útil vai de 1 a 20")


def _datas(regra: dict, inicio: date):
    """Datas candidatas, em ordem, a partir de `inicio`, até HORIZONTE dias."""
    d = inicio
    while d <= inicio + timedelta(days=HORIZONTE):
        tipo = regra["tipo"]
        if tipo == "todo_dia":
            yield d
        elif tipo == "dias_uteis" and dia_util(d):
            yield d
        elif tipo == "semana" and d.weekday() in regra["dias"]:      # 0 = segunda
            yield d
        elif tipo == "dia_do_mes":
            ultimo = (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            if d.day == min(regra["dia"], ultimo.day):                 # dia 31 em fevereiro = dia 28/29
                yield d
        elif tipo == "n_dia_util":
            try:
                if d == n_dia_util(d.year, d.month, regra["n"]):
                    yield d
            except ValueError:
                pass
        d += timedelta(days=1)


def proxima(regra: dict, depois_de: datetime) -> datetime:
    """Próximo horário da regra, estritamente depois de `depois_de`. Devolve em UTC."""
    validar_regra(regra)
    hora = time.fromisoformat(regra.get("hora", "08:00"))
    local = depois_de.astimezone(FUSO)
    for d in _datas(regra, local.date()):
        quando = datetime.combine(d, hora, tzinfo=FUSO)
        if quando > local:
            return quando.astimezone(ZoneInfo("UTC"))
    raise ValueError(f"a regra não tem data nos próximos {HORIZONTE} dias")


# ---------- quem roda ----------

def _suspender(conn, ident: int, dono: str, motivo: str) -> None:
    conn.execute("UPDATE agendamento SET ativo = false, retentar_em = NULL, motivo = %s WHERE id = %s",
                 (motivo, ident))
    conn.execute("INSERT INTO aviso (dono, texto) VALUES (%s, %s)",
                 (dono, f"Agendamento {ident} suspenso: {motivo}"))


def tick(conn, agora: datetime, conferir_acesso, rodar) -> list[str]:
    """Uma volta do agendador: transforma o que venceu em ocorrências e roda as pendentes.

    conferir_acesso(dono, agente) -> bool         conta ativa no Prumo ID e acesso ao agente
    rodar(agendamento, retomar_de) -> (ok, execucao)
        retomar_de: None, ou a execução que falhou, para "continuar daqui" do passo que parou
    """
    eventos = criar_ocorrencias(conn, agora, conferir_acesso)
    if eventos == ["outro agendador está rodando"]:
        return eventos
    return eventos + despachar(conn, agora, conferir_acesso, rodar)


def criar_ocorrencias(conn, agora: datetime, conferir_acesso) -> list[str]:
    """Avança o calendário e grava a ocorrência pendente, na mesma transação."""
    eventos = []
    with conn.transaction():
        trava = conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (TRAVA_AGENDADOR,))
        if not trava.fetchone()[0]:
            return ["outro agendador está rodando"]
        vencidos = conn.execute(
            """SELECT id, dono, agente, regra,
                      CASE WHEN retentar_em <= %(agora)s THEN ultima_execucao END
                 FROM agendamento
                WHERE ativo AND (proxima <= %(agora)s OR retentar_em <= %(agora)s)
                ORDER BY id FOR UPDATE""", {"agora": agora}).fetchall()
        for ident, dono, agente, regra, retomar_de in vencidos:
            try:
                # parado por dias? UMA ocorrência, e segue
                seguinte = proxima(regra, agora)
            except ValueError as e:
                # regra quebrada suspende só este agendamento
                _suspender(conn, ident, dono, f"regra inválida: {e}")
                eventos.append(f"{ident}: suspenso, regra inválida")
                continue
            conn.execute("UPDATE agendamento SET proxima = %s, retentar_em = NULL"
                         " WHERE id = %s", (seguinte, ident))
            # aberta = pendente ou rodando, com ou sem sinal de vida: a parada será retomada
            em_aberto = conn.execute(
                """SELECT 1 FROM ocorrencia
                    WHERE agendamento = %s AND status IN ('pendente', 'rodando')""",
                (ident,)).fetchone()
            if em_aberto:
                conn.execute("INSERT INTO ocorrencia (agendamento, status)"
                             " VALUES (%s, 'pulada')", (ident,))
                eventos.append(f"{ident}: a anterior ainda está aberta; esta foi pulada")
            elif not conferir_acesso(dono, agente):
                _suspender(conn, ident, dono, "quem agendou perdeu o acesso")
                eventos.append(f"{ident}: suspenso, quem agendou perdeu o acesso")
            else:
                conn.execute("""INSERT INTO ocorrencia (agendamento, status, retomar_de)
                                VALUES (%s, 'pendente', %s)""", (ident, retomar_de))
    return eventos


def despachar(conn, agora: datetime, conferir_acesso, rodar) -> list[str]:
    """Pega ocorrências pendentes, e as 'rodando' de processo que morreu, PARALELO por vez."""
    eventos = []
    while lote := _despachar_lote(conn, agora, conferir_acesso, rodar):
        eventos += lote
    return eventos


def _despachar_lote(conn, agora: datetime, conferir_acesso, rodar) -> list[str]:
    posse = uuid.uuid4()
    with conn.transaction():
        pegas = conn.execute(
            """UPDATE ocorrencia o
                  SET status = 'rodando', posse = %(posse)s, rodando_desde = %(agora)s
                 FROM agendamento a
                WHERE a.id = o.agendamento AND o.id IN (
                      SELECT id FROM ocorrencia
                       WHERE status = 'pendente'
                          OR (status = 'rodando' AND rodando_desde < %(limite)s)
                       ORDER BY id LIMIT %(paralelo)s FOR UPDATE SKIP LOCKED)
            RETURNING o.id, a.id, a.dono, a.agente, coalesce(o.execucao, o.retomar_de)""",
            {"posse": posse, "agora": agora, "limite": agora - RODANDO_NO_MAXIMO,
             "paralelo": PARALELO}).fetchall()
    eventos = []
    # fora da transação: o banco não espera o agente
    with ThreadPoolExecutor(PARALELO) as grupo:
        futuros = {}
        for oc, ident, dono, agente, retomar_de in pegas:
            ag = {"id": ident, "dono": dono, "agente": agente, "ocorrencia": oc, "posse": posse}
            # de novo: a ocorrência pode ter esperado
            if conferir_acesso(dono, agente):
                futuros[grupo.submit(rodar, ag, retomar_de)] = ag
            else:
                r = _registrar(conn, ag, posse, agora, False, None, "sem acesso")
                eventos.append(f"{ident}: {r}")
        inicio = monotonic()
        while futuros:
            prontos, _ = wait(futuros, timeout=BATIMENTO_S, return_when=FIRST_COMPLETED)
            for futuro in prontos:
                ag = futuros.pop(futuro)
                try:
                    ok, execucao = futuro.result()
                except Exception:
                    ok, execucao = False, None
                r = _registrar(conn, ag, posse, agora, bool(ok), execucao)
                eventos.append(f"{ag['id']}: {r}")
            if futuros:                # sinal de vida das que seguem: ninguém as toma
                vivo = agora + timedelta(seconds=monotonic() - inicio)
                conn.execute("UPDATE ocorrencia SET rodando_desde = %s"
                             " WHERE posse = %s AND status = 'rodando'", (vivo, posse))
    return eventos


def vincular(conn, ag: dict, execucao: str) -> bool:
    """Quem roda o agente chama isto assim que cria a execução, antes do primeiro passo.

    Se o processo morrer no meio, a ocorrência retomada continua desta execução,
    do passo em que parou, em vez de começar outra do zero.
    """
    return conn.execute(
        """UPDATE ocorrencia SET execucao = %s
            WHERE id = %s AND posse = %s AND status = 'rodando'""",
        (execucao, ag["ocorrencia"], ag["posse"])).rowcount == 1


def _registrar(conn, ag: dict, posse, agora: datetime, ok: bool, execucao: str | None,
               sem_acesso: str | None = None) -> str:
    ident = ag["id"]
    with conn.transaction():
        if conn.execute("""SELECT 1 FROM ocorrencia WHERE id = %s AND posse = %s AND status = 'rodando'
                           FOR UPDATE""", (ag["ocorrencia"], posse)).fetchone() is None:
            return "resultado descartado: outro processo assumiu a ocorrência"
        retentativa, falhas = conn.execute(
            "SELECT retentativa, falhas_seguidas FROM agendamento WHERE id = %s FOR UPDATE",
            (ident,)).fetchone()
        conn.execute("UPDATE agendamento SET ultima_execucao = %s WHERE id = %s", (execucao, ident))
        if ok:
            conn.execute("UPDATE agendamento SET retentativa = 0, falhas_seguidas = 0 WHERE id = %s",
                         (ident,))
            status = "ok"
        elif sem_acesso:
            _suspender(conn, ident, ag["dono"], "quem agendou perdeu o acesso")
            status = "falhou; agendamento suspenso, sem acesso"
        elif retentativa < len(RETENTATIVAS):
            conn.execute("""UPDATE agendamento SET retentativa = retentativa + 1, retentar_em = %s
                             WHERE id = %s""", (agora + RETENTATIVAS[retentativa], ident))
            status = f"falhou; nova tentativa em {RETENTATIVAS[retentativa]}"
        else:
            falhas += 1
            conn.execute("UPDATE agendamento SET retentativa = 0, falhas_seguidas = %s WHERE id = %s",
                         (falhas, ident))
            status = "falhou"
            if falhas >= FALHAS_PARA_SUSPENDER:
                _suspender(conn, ident, ag["dono"], "três ocorrências seguidas com falha")
                status += "; agendamento suspenso"
        conn.execute("UPDATE ocorrencia SET status = %s, detalhe = %s WHERE id = %s",
                     ("ok" if ok else "falhou", Jsonb({"resumo": status, "execucao": execucao}),
                      ag["ocorrencia"]))
    return status
