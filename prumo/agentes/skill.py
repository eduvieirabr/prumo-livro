"""Skill é dado, não código (capítulo 12).

Uma skill é um pacote de instruções (SKILL.md + references/). Aqui ela é
lida, analisada, recortada para caber no orçamento e versionada. Nenhum
arquivo da skill é executado, nunca.
"""
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field

ORCAMENTO_SKILL = 4096        # tokens da skill dentro de uma janela de 16.384
MAX_PACOTE = 5 * 1024 * 1024  # 5 MB descompactados, no total
MAX_ARQUIVOS = 200
EXECUTAVEIS = (".py", ".sh", ".js", ".ts", ".ps1", ".bat", ".rb")
SEGREDO = re.compile(r"(sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
                     r"|(?i:api[_-]?key|token|senha)\s*[:=]\s*\S{8,})")


@dataclass
class Skill:
    nome: str
    descricao: str
    instrucoes: str
    arquivos: dict[str, bytes] = field(default_factory=dict)

    @property
    def hash(self) -> str:
        h = hashlib.sha256(self.instrucoes.encode())
        for nome in sorted(self.arquivos):
            h.update(nome.encode() + self.arquivos[nome])
        return h.hexdigest()


def _frontmatter(md: str) -> tuple[dict, str]:
    md = md.replace("\r\n", "\n")
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", md, re.S)
    if not m:
        return {}, md
    campos = dict(re.findall(r"^(\w+):\s*(.+)$", m.group(1), re.M))
    return {k: v.strip().strip('"') for k, v in campos.items()}, m.group(2)


def ler_pacote(nome_arquivo: str, conteudo: bytes) -> Skill:
    """Aceita .zip/.skill (pacote) ou .md/.txt (só instruções)."""
    if nome_arquivo.endswith((".zip", ".skill")):
        with zipfile.ZipFile(io.BytesIO(conteudo)) as z:
            infos = [i for i in z.infolist() if not i.is_dir()]
            if len(infos) > MAX_ARQUIVOS or sum(i.file_size for i in infos) > MAX_PACOTE:
                raise ValueError("pacote grande demais")       # e nada é extraído para o disco
            arquivos = {i.filename: z.read(i) for i in infos}
        principal = next((n for n in arquivos if n.split("/")[-1] == "SKILL.md"), None)
        if principal is None:
            raise ValueError("pacote sem SKILL.md")
        texto = arquivos.pop(principal).decode("utf-8")
    else:
        arquivos, texto = {}, conteudo.decode("utf-8")
    meta, corpo = _frontmatter(texto)
    return Skill(meta.get("name", nome_arquivo.rsplit(".", 1)[0]), meta.get("description", ""),
                 corpo.strip(), arquivos)


def tokens(texto: str) -> int:
    """Estimativa para prosa em português; o cadastro troca pelo número medido no Broker."""
    return len(texto) // 3 + 1


def analisar(s: Skill) -> dict:
    """A prévia que o dono vê antes de salvar a versão."""
    achados = []
    scripts = [n for n in s.arquivos if n.endswith(EXECUTAVEIS)]
    if scripts:
        achados.append(f"scripts que não vão rodar: {', '.join(sorted(scripts))}")
    if re.search(r"\b(execute|run|rode|rodar|python3?\s+\S+\.py|bash)\b", s.instrucoes, re.I):
        achados.append("as instruções mandam executar algo")
    if re.search(r"https?://", s.instrucoes):
        achados.append("as instruções citam endereços da internet, que a IA local não acessa")
    textos = [s.instrucoes] + [c.decode("utf-8", "ignore") for c in s.arquivos.values()]
    if any(SEGREDO.search(t) for t in textos):
        achados.append("parece haver uma chave ou senha no pacote")
    total = tokens(s.instrucoes)
    if total > ORCAMENTO_SKILL:
        achados.append(f"instruções com ~{total} tokens; acima de {ORCAMENTO_SKILL}, o excesso é cortado")
    if scripts or any("executar" in a for a in achados):
        selo = "precisa adaptar"
    elif total > ORCAMENTO_SKILL or len(achados) > 0:
        selo = "roda em parte"
    else:
        selo = "roda inteira"
    return {"selo": selo, "achados": achados, "tokens": total}


def recortar(instrucoes: str, limite: int = ORCAMENTO_SKILL) -> str:
    """Corta no último parágrafo que cabe, e diz que cortou."""
    if tokens(instrucoes) <= limite:
        return instrucoes
    pedaco = instrucoes[: limite * 3]
    corte = pedaco.rfind("\n\n")
    return pedaco[: corte if corte > 0 else len(pedaco)] + "\n\n[instruções cortadas no limite]"


def salvar_versao(conn, s: Skill, formulario: list[dict], autor: str) -> int:
    """Grava uma versão nova. Conteúdo igual ao da última versão não gera versão."""
    ultima = conn.execute(
        "SELECT versao, hash FROM skill_versao WHERE skill = %s ORDER BY versao DESC LIMIT 1",
        (s.nome,)).fetchone()
    hash_novo = hashlib.sha256((s.hash + json.dumps(formulario, sort_keys=True)).encode()).hexdigest()
    if ultima and ultima[1] == hash_novo:
        return ultima[0]
    versao = (ultima[0] + 1) if ultima else 1
    arquivos = {n: c.decode("utf-8", "replace") for n, c in s.arquivos.items()}
    conn.execute(
        """INSERT INTO skill_versao (skill, versao, hash, instrucoes, arquivos, formulario,
                                     tokens, criado_por)
           VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)""",
        (s.nome, versao, hash_novo, s.instrucoes, json.dumps(arquivos), json.dumps(formulario),
         tokens(s.instrucoes), autor))
    return versao


# ---------- usar a skill ----------

CONFERIR = """Você confere se um pedido traz os dados que a skill precisa.
Responda SOMENTE com JSON: {"completo": true|false, "faltando": ["..."]}.
Valores de exemplo das instruções da skill NÃO contam como dados do pedido."""


def conferir_entradas(instrucoes: str, pedido: str, llm) -> list[str]:
    """Para skill sem formulário: o que falta no pedido. Na dúvida, falta."""
    bruto = llm(f"{CONFERIR}\n\n<skill>\n{instrucoes}\n</skill>\n\n"
                f"<pedido>\n{pedido}\n</pedido>")
    try:
        r = json.loads(bruto)
        if r["completo"] is True:
            return []
        return [str(f) for f in r.get("faltando", [])] or ["dados do caso"]
    except (ValueError, KeyError, TypeError):
        return ["não foi possível conferir; informe os dados do caso"]


def montar(instrucoes: str, pergunta: str, dados: dict | None = None,
           anexos: list[tuple[str, str, str | None]] = ()) -> list[dict]:
    """Mensagens para o Broker: skill no sistema; dados e anexos marcados como dados."""
    def neutro(t: str) -> str:
        return re.sub(r"<(\s*/?\s*(dados_informados|anexo))", r"&lt;\1", t, flags=re.I)

    def atributo(t: str) -> str:
        return t.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    partes = []
    if dados:
        partes.append("<dados_informados>\n" + neutro(json.dumps(dados, ensure_ascii=False, indent=1))
                      + "\n</dados_informados>")
    for nome, texto, campo in anexos:
        rotulo = f' campo="{atributo(campo)}"' if campo else ""
        partes.append(f'<anexo nome="{atributo(nome)}"{rotulo}>\n{neutro(texto)}\n</anexo>')
    partes.append(pergunta)
    return [{"role": "system", "content": recortar(instrucoes)},
            {"role": "user", "content": "\n\n".join(partes)}]
