"""Política de envio externo (capítulo 2).

A regra "nada sai daqui" vira dado, não boa intenção: cada tarefa de IA
declara se pode ir a um modelo de fora e em que condição.
"""
import re
from dataclasses import dataclass
from datetime import date
from enum import Enum


class Destino(Enum):
    LOCAL = "local"
    EXTERNO = "externo"


@dataclass(frozen=True)
class Regra:
    externo_permitido: bool
    exige_aceite: bool = True
    motivo: str = ""


POLITICA: dict[str, Regra] = {
    "ler_documento": Regra(False, motivo="documento de cliente inteiro, com dados fiscais"),
    "extrair_debitos": Regra(False, motivo="débitos, CNPJ e valores do cliente"),
    "planejar_calculo": Regra(False, motivo="estrutura e amostras da planilha do cliente"),
    "assistente": Regra(True, exige_aceite=True,
                        motivo="pergunta e números já calculados; só com aceite"),
}

# capítulo 15: a regra ampliada de forma explícita, e não por interpretação
POLITICA["ia_conectada"] = Regra(True, exige_aceite=True,
                                 motivo="resposta de agente pedida pela própria pessoa, por MCP; só com aceite")


class EnvioNegado(Exception):
    pass


def autorizar(tarefa: str, destino: Destino, *, aceite_registrado: bool = False) -> None:
    """Levanta EnvioNegado quando o pedido não pode ir ao destino."""
    if destino is Destino.LOCAL:
        return
    regra = POLITICA.get(tarefa)
    if regra is None:
        raise EnvioNegado(f"Tarefa '{tarefa}' sem regra: na dúvida, fica na casa")
    if not regra.externo_permitido:
        raise EnvioNegado(f"'{tarefa}' nunca sai: {regra.motivo}")
    if regra.exige_aceite and not aceite_registrado:
        raise EnvioNegado(f"'{tarefa}' só sai com aceite registrado")


# ---------- o aceite: do cliente, por finalidade, com validade ----------

def aceite_vigente(conn, empresa: str, finalidade: str, hoje: date) -> bool:
    """Ter acesso ao dado não é ter autorização para mandá-lo para fora. O aceite é do
    cliente, registrado por quem responde por ele; vale para uma finalidade e vence."""
    return conn.execute(
        """SELECT 1 FROM aceite WHERE empresa = %s AND finalidade = %s
              AND revogado_em IS NULL AND valido_ate >= %s LIMIT 1""",
        (empresa, finalidade, hoje)).fetchone() is not None


# ---------- o que sai é montado pelo Broker, campo por campo ----------

CONTRATO_EXTERNO = {"assistente": {"pergunta": 1_000, "calculo": 40}}
_DECIMAL = re.compile(r"-?[0-9]{1,12}\.[0-9]{2}")
_NOME = re.compile(r"[a-z][a-z0-9_]{0,59}")
INSTRUCOES_EXTERNAS = ("Você explica cálculos de parcelamento para contadores. "
                       "Use só os valores do bloco <calculo>. Não faça contas.")


def montar_externo(tarefa: str, conteudo: dict) -> list[dict]:
    """A aplicação manda campos; o pedido para fora é escrito aqui. Um documento colado
    na pergunta, um campo a mais ou um valor que não é número: não sai."""
    contrato = CONTRATO_EXTERNO.get(tarefa)
    if contrato is None:
        raise EnvioNegado(f"'{tarefa}' não tem contrato de envio externo")
    if not isinstance(conteudo, dict) or set(conteudo) - {"empresa", *contrato}:
        raise EnvioNegado(f"campos fora do contrato de '{tarefa}'")
    pergunta, calculo = conteudo.get("pergunta"), conteudo.get("calculo", {})
    if not isinstance(pergunta, str) or not 0 < len(pergunta) <= contrato["pergunta"]:
        raise EnvioNegado(f"a pergunta precisa ter de 1 a {contrato['pergunta']} caracteres")
    if not isinstance(calculo, dict) or len(calculo) > contrato["calculo"] or not all(
            isinstance(k, str) and _NOME.fullmatch(k)
            and isinstance(v, str) and _DECIMAL.fullmatch(v)
            for k, v in calculo.items()):
        raise EnvioNegado("o cálculo só leva nomes curtos e valores em reais")
    bloco = "\n".join(f"{k}: {v}" for k, v in calculo.items())
    return [{"role": "system", "content": INSTRUCOES_EXTERNAS},
            {"role": "user", "content": f"<calculo>\n{bloco}\n</calculo>\n\n{pergunta}"}]
