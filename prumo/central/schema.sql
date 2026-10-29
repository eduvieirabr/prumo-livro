-- Central de Eventos (capítulos 16 e 17). Num esquema próprio: o Broker já tem uma tabela "evento".
CREATE SCHEMA IF NOT EXISTS central;

CREATE TABLE IF NOT EXISTS central.aplicacao (
    id            text PRIMARY KEY CHECK (id ~ '^[a-z][a-z0-9-]{2,40}$'),   -- 'vertice', 'prumo-parcelamentos'
    nome          text NOT NULL,
    cliente_sso   text NOT NULL UNIQUE,         -- o azp do token de serviço no Prumo ID
    nivel         text NOT NULL DEFAULT 'interno'
                  CHECK (nivel IN ('interno', 'restrito', 'confidencial')),  -- até onde pode ler
    aprovada_em   timestamptz,                  -- só o administrador da central aprova
    aprovada_por  text
);

CREATE TABLE IF NOT EXISTS central.canal (
    nome          text PRIMARY KEY CHECK (nome ~ '^[a-z0-9-]+\.[a-z0-9_]+\.[a-z0-9_]+\.v[0-9]+$'),  -- vertice.titulo.pago.v1
    dono          text NOT NULL REFERENCES central.aplicacao(id),
    classificacao text NOT NULL CHECK (classificacao IN ('interno', 'restrito', 'confidencial')),
    esquema       jsonb NOT NULL,               -- {"obrigatorios": {"campo": "tipo"}, "opcionais": {...}}
    retencao      interval NOT NULL DEFAULT '30 days',
    ultimo_seq    bigint NOT NULL DEFAULT 0,    -- numeração densa, por canal, na ordem do commit
    criado_em     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS central.inscricao (
    aplicacao     text NOT NULL REFERENCES central.aplicacao(id),
    canal         text NOT NULL REFERENCES central.canal(nome),
    cursor        bigint NOT NULL DEFAULT 0,    -- último evento confirmado
    entregue_ate  bigint NOT NULL DEFAULT 0,    -- maior evento já entregue: confirmar não passa dele
    criada_em     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (aplicacao, canal)
);

CREATE TABLE IF NOT EXISTS central.evento (
    canal         text NOT NULL REFERENCES central.canal(nome),
    seq           bigint NOT NULL,
    id            text NOT NULL,                -- id do produtor: publicar de novo não duplica
    assunto       text NOT NULL,                -- de que coisa o evento fala: 'titulo:88231'
    empresa       text,                         -- CNPJ do cliente da Prumo, quando houver
    dados         jsonb NOT NULL,
    publicado_em  timestamptz NOT NULL DEFAULT clock_timestamp(),   -- a hora do INSERT, não do início da transação
    PRIMARY KEY (canal, seq),
    UNIQUE (canal, id)
);

-- quando o administrador aceita que uma inscrição perdeu eventos (capítulo 17)
CREATE TABLE IF NOT EXISTS central.perda_aceita (
    aplicacao  text NOT NULL,
    canal      text NOT NULL,
    de         bigint NOT NULL,                -- cursor antes
    ate        bigint NOT NULL,                -- cursor depois: o último evento que não existe mais
    por        text NOT NULL,
    em         timestamptz NOT NULL DEFAULT now()
);

-- a memória da idempotência dura mais que o evento: o conteúdo some na retenção,
-- o id, o número e o resumo do conteúdo ficam para sempre (umas dezenas de bytes por evento)
CREATE TABLE IF NOT EXISTS central.evento_id (
    canal   text NOT NULL REFERENCES central.canal(nome),
    id      text NOT NULL,
    seq     bigint NOT NULL,
    resumo  text NOT NULL,                       -- sha256 do assunto, empresa e dados, em JSON canônico
    PRIMARY KEY (canal, id)
);
