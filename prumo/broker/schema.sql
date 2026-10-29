-- Broker de IA do livro (capítulos 5 a 9). PostgreSQL 16.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS integracao (
    id          text PRIMARY KEY,                -- 'parcelamentos', 'agentes'...
    token_hash  text UNIQUE,                     -- sha256 do token fixo; o token nunca é guardado
    tarefas     text[] NOT NULL,                 -- tarefas que a integração pode pedir
    interativa  boolean NOT NULL DEFAULT false,  -- pode pedir prioridade de quem está na tela
    avaliacao   boolean NOT NULL DEFAULT false,  -- pode pedir inferência nova, sem reaproveitar (cap. 18)
    ativa       boolean NOT NULL DEFAULT true    -- revogar = false (o histórico fica)
);

CREATE TABLE IF NOT EXISTS pedido (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    integracao   text NOT NULL REFERENCES integracao(id),
    tarefa       text NOT NULL,
    prioridade   smallint NOT NULL DEFAULT 1,    -- 0 interativo, 1 lote
    chave        text NOT NULL,                  -- hash de tarefa + prompt + modelo + conteúdo
    status       text NOT NULL DEFAULT 'pendente'
                 CHECK (status IN ('pendente', 'executando', 'concluido', 'erro', 'reaproveitado')),
    conteudo     jsonb,                          -- apagado depois de 48 h
    resultado    jsonb,                          -- apagado depois de 48 h
    servidor     text,
    vaga_token   uuid,                           -- quem tem a vaga agora
    vaga_ate     timestamptz,                    -- até quando a vaga é dele
    tentativas   int NOT NULL DEFAULT 0,
    tokens_entrada int,
    tokens_saida   int,
    duracao_ms     int,
    descartes      int NOT NULL DEFAULT 0,       -- resultados jogados fora (vaga perdida)
    ms_descartados int NOT NULL DEFAULT 0,       -- tempo de vaga gasto à toa
    origem       uuid REFERENCES pedido(id),     -- de qual pedido o resultado foi reaproveitado
    criado_em    timestamptz NOT NULL DEFAULT now(),
    concluido_em timestamptz
);

CREATE INDEX IF NOT EXISTS pedido_fila ON pedido (criado_em)
    WHERE status IN ('pendente', 'executando');
CREATE INDEX IF NOT EXISTS pedido_chave ON pedido (integracao, chave, concluido_em)
    WHERE status = 'concluido';

-- capítulo 7: eventos de cada pedido. É a outbox que o SSE lê.
CREATE TABLE IF NOT EXISTS evento (
    seq        bigserial PRIMARY KEY,
    pedido     uuid NOT NULL REFERENCES pedido(id) ON DELETE CASCADE,
    tipo       text NOT NULL,                    -- parcial, nova_tentativa, concluido, erro
    dados      jsonb NOT NULL DEFAULT '{}',
    criado_em  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS evento_pedido ON evento (pedido, seq);

-- capítulo 8: LLM paga como exceção
ALTER TABLE integracao ADD COLUMN IF NOT EXISTS modo_ia text NOT NULL DEFAULT 'local'
    CHECK (modo_ia IN ('local', 'local_com_fallback', 'llm_paga'));

CREATE TABLE IF NOT EXISTS orcamento (
    integracao text NOT NULL REFERENCES integracao(id),
    mes        date NOT NULL,                    -- primeiro dia do mês
    teto       numeric(12,2) NOT NULL,           -- em reais
    gasto      numeric(14,4) NOT NULL DEFAULT 0,
    reservado  numeric(14,4) NOT NULL DEFAULT 0,
    PRIMARY KEY (integracao, mes)                -- sem CHECK: gasto real sempre é registrado
);

CREATE TABLE IF NOT EXISTS uso_pago (
    pedido     uuid PRIMARY KEY REFERENCES pedido(id),
    integracao text NOT NULL,
    mes        date NOT NULL,
    modelo     text NOT NULL,
    reserva    numeric(14,4) NOT NULL,
    custo      numeric(14,4),
    situacao   text NOT NULL DEFAULT 'reservado'
               CHECK (situacao IN ('reservado', 'liquidado', 'conciliar')),
    criado_em  timestamptz NOT NULL DEFAULT now()
);

-- capítulo 9: a integração também se identifica pelo cliente do Prumo ID
ALTER TABLE integracao ADD COLUMN IF NOT EXISTS cliente_sso text UNIQUE;

-- capítulos 2, 8 e 15: o aceite de envio externo é por cliente e por finalidade, com dono e validade
CREATE TABLE IF NOT EXISTS aceite (
    id            bigserial PRIMARY KEY,
    empresa       text NOT NULL,                 -- CNPJ do cliente da Prumo
    finalidade    text NOT NULL,                 -- a tarefa da política: 'assistente', 'ia_conectada'
    responsavel   text NOT NULL,                 -- quem responde pelo cliente na Prumo e registrou
    evidencia     text NOT NULL,                 -- o documento do cliente: aditivo, termo, e-mail
    registrado_em timestamptz NOT NULL DEFAULT now(),
    valido_ate    date NOT NULL,
    revogado_em   timestamptz
);
CREATE INDEX IF NOT EXISTS aceite_vigente ON aceite (empresa, finalidade) WHERE revogado_em IS NULL;

-- o aceite é do cliente (tabela aceite), não da integração
ALTER TABLE integracao DROP COLUMN IF EXISTS aceite_externo_por;
ALTER TABLE integracao DROP COLUMN IF EXISTS aceite_externo_em;
