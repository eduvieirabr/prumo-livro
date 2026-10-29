"""Um evento da central dispara um agente (capítulo 17).

As mesmas regras do agendamento do capítulo 14: roda em nome de quem criou o
gatilho, com o acesso conferido antes; canal restrito ou confidencial só para
supervisor ou acima; e o mesmo evento nunca dispara duas vezes o mesmo gatilho.
"""
from prumo.identidade import NIVEIS as PAPEIS

CLASSIFICACOES = ("interno", "restrito", "confidencial")


def _suspender(conn, ident: int, motivo: str) -> None:
    conn.execute("UPDATE gatilho SET ativo = false, motivo = %s WHERE id = %s", (motivo, ident))


def disparar(conn, canal: str, classificacao: str, evento: dict,
             conferir_acesso, papel_de, rodar) -> list[str]:
    """conferir_acesso(dono, agente) -> bool; papel_de(dono) -> papel; rodar(gatilho, entradas) -> bool"""
    resultado = []
    for ident, dono, agente in conn.execute(
            "SELECT id, dono, agente FROM gatilho WHERE canal = %s AND ativo ORDER BY id",
            (canal,)).fetchall():
        if CLASSIFICACOES.index(classificacao) > 0 and \
                PAPEIS.index(papel_de(dono)) < PAPEIS.index("supervisor"):
            _suspender(conn, ident, f"canal {classificacao} só dispara agente de supervisor")
            resultado.append(f"{ident}: suspenso, canal {classificacao}")
            continue
        if not conferir_acesso(dono, agente):
            _suspender(conn, ident, "quem criou o gatilho perdeu o acesso")
            resultado.append(f"{ident}: suspenso, perdeu o acesso")
            continue
        novo = conn.execute(
            """INSERT INTO disparo (gatilho, id_evento) VALUES (%s, %s)
               ON CONFLICT DO NOTHING RETURNING gatilho""", (ident, evento["id"])).fetchone()
        if novo is None:
            resultado.append(f"{ident}: evento já disparou este gatilho")
            continue
        # o evento entra como dado, nunca como instrução
        try:
            ag = {"id": ident, "dono": dono, "agente": agente}
            ok = bool(rodar(ag, {"evento": evento}))
        except Exception:
            ok = False
        conn.execute("UPDATE disparo SET status = %s WHERE gatilho = %s AND id_evento = %s",
                     ("ok" if ok else "falhou", ident, evento["id"]))
        resultado.append(f"{ident}: {'ok' if ok else 'falhou'}")
    return resultado
