-- Prumo Agentes (capítulos 11 e 12). PostgreSQL 16.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Versão de skill: uma vez gravada, não muda e não some.
CREATE TABLE IF NOT EXISTS skill_versao (
    skill       text NOT NULL,
    versao      int NOT NULL,
    hash        text NOT NULL,                 -- sha256 do conteúdo
    instrucoes  text NOT NULL,
    arquivos    jsonb NOT NULL DEFAULT '{}',   -- referências e scripts, como texto; nada executa
    formulario  jsonb NOT NULL DEFAULT '[]',
    tokens      int NOT NULL,
    criado_por  text NOT NULL,
    criado_em   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (skill, versao)
);

CREATE OR REPLACE FUNCTION skill_versao_imutavel() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'versão de skill não se altera: crie uma versão nova';
END $$;
DROP TRIGGER IF EXISTS imutavel ON skill_versao;
CREATE TRIGGER imutavel BEFORE UPDATE OR DELETE ON skill_versao
    FOR EACH ROW EXECUTE FUNCTION skill_versao_imutavel();
DROP TRIGGER IF EXISTS imutavel_truncate ON skill_versao;
CREATE TRIGGER imutavel_truncate BEFORE TRUNCATE ON skill_versao
    -- TRUNCATE não dispara gatilho de linha
    FOR EACH STATEMENT EXECUTE FUNCTION skill_versao_imutavel();

-- Execução de agente: o estado mora aqui, não na memória do processo.
CREATE TABLE IF NOT EXISTS execucao (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    agente       text NOT NULL,
    versao       int NOT NULL,                 -- versão do agente fixada na execução
    dono         text NOT NULL,                -- quem rodou: a execução é dele
    passo        int NOT NULL DEFAULT 0,       -- próximo passo a rodar
    contexto     jsonb NOT NULL DEFAULT '{}',  -- entradas + saída de cada passo, por nome
    status       text NOT NULL DEFAULT 'pronta'
                 CHECK (status IN ('pronta', 'rodando', 'pausada', 'aguardando', 'concluida', 'erro')),
    aguardando   text,                         -- o que falta para continuar
    erro         text,
    origem       uuid REFERENCES execucao(id), -- "continuar daqui" a partir de outra execução
    posse        uuid,                         -- quem está rodando agora; só ele grava
    atualizado_em timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS passo_execucao (
    execucao  uuid NOT NULL REFERENCES execucao(id) ON DELETE CASCADE,
    indice    int NOT NULL,
    nome      text NOT NULL,
    tipo      text NOT NULL,
    saida     jsonb,
    erro      text,
    ms        int NOT NULL,
    em        timestamptz NOT NULL DEFAULT now()
);

-- capítulo 14: agendamentos, em nome de quem agendou
CREATE TABLE IF NOT EXISTS agendamento (
    id              bigserial PRIMARY KEY,
    dono            text NOT NULL,             -- quem agendou: a execução roda em nome dele
    agente          text NOT NULL,
    regra           jsonb NOT NULL,            -- {"tipo": "n_dia_util", "n": 5, "hora": "08:00"}
    proxima         timestamptz NOT NULL,
    ativo           boolean NOT NULL DEFAULT true,
    motivo          text,                      -- por que foi suspenso
    retentativa     int NOT NULL DEFAULT 0,
    retentar_em     timestamptz,
    falhas_seguidas int NOT NULL DEFAULT 0,
    ultima_execucao text                       -- a nova tentativa continua desta execução
);

-- cada vez que o agendamento vence vira uma ocorrência, gravada junto com o avanço do calendário:
-- se o processo cair antes de rodar, a ocorrência continua lá, pendente, e outro a pega
CREATE TABLE IF NOT EXISTS ocorrencia (
    id            bigserial PRIMARY KEY,
    agendamento   bigint NOT NULL REFERENCES agendamento(id) ON DELETE CASCADE,
    status        text NOT NULL CHECK (status IN ('pendente', 'rodando', 'ok', 'falhou', 'pulada')),
    retomar_de    text,                      -- execução que falhou: continuar dela
    posse         uuid,                      -- quem está rodando; só ele registra o resultado
    rodando_desde timestamptz,
    detalhe       jsonb NOT NULL DEFAULT '{}',
    em            timestamptz NOT NULL DEFAULT now()
);
-- a execução que esta ocorrência iniciou, gravada logo no começo: a retomada continua dela
ALTER TABLE ocorrencia ADD COLUMN IF NOT EXISTS execucao text;
-- no máximo uma ocorrência aberta por agendamento, mesmo a de um processo que morreu
CREATE UNIQUE INDEX IF NOT EXISTS uma_aberta_por_agendamento ON ocorrencia (agendamento)
    WHERE status IN ('pendente', 'rodando');

-- avisos na tela de quem agendou (suspensões e o motivo)
CREATE TABLE IF NOT EXISTS aviso (
    id    bigserial PRIMARY KEY,
    dono  text NOT NULL,
    texto text NOT NULL,
    lido  boolean NOT NULL DEFAULT false,
    em    timestamptz NOT NULL DEFAULT now()
);

-- capítulo 17: agente disparado por evento da central
CREATE TABLE IF NOT EXISTS gatilho (
    id      bigserial PRIMARY KEY,
    dono    text NOT NULL,                     -- roda em nome dele, como o agendamento
    agente  text NOT NULL,
    canal   text NOT NULL,                     -- canal da central em que o Prumo Agentes está inscrito
    ativo   boolean NOT NULL DEFAULT true,
    motivo  text
);

CREATE TABLE IF NOT EXISTS disparo (
    gatilho    bigint NOT NULL REFERENCES gatilho(id) ON DELETE CASCADE,
    id_evento  text NOT NULL,
    status     text NOT NULL DEFAULT 'rodando',
    em         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (gatilho, id_evento)            -- reentrega do evento não dispara de novo
);
