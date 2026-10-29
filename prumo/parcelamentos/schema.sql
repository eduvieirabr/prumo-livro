-- Prumo Parcelamentos, o lado que consome a central (capítulo 17).
CREATE SCHEMA IF NOT EXISTS parc;

CREATE TABLE IF NOT EXISTS parc.parcela (
    parcelamento    text NOT NULL,
    numero          int NOT NULL,
    vencimento      date NOT NULL,
    valor_calculado numeric(14,2) NOT NULL,     -- o motor do capítulo 1
    valor_pago      numeric(14,2),              -- o que o cliente registrou como pago; a Marta confere o DARF
    pago_em         date,
    titulo          text,                       -- o título do Vértice que pagou
    divergencia     numeric(14,2),              -- pago − calculado, quando diferente: vai para a Marta
    PRIMARY KEY (parcelamento, numero)
);

-- o que já foi aplicado: a reentrega encontra a linha e não faz nada
CREATE TABLE IF NOT EXISTS parc.evento_aplicado (
    canal      text NOT NULL,
    id_evento  text NOT NULL,
    seq        bigint NOT NULL,
    resultado  text NOT NULL,
    em         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (canal, id_evento)
);

CREATE TABLE IF NOT EXISTS parc.falha_evento (
    canal      text NOT NULL,
    id_evento  text NOT NULL,
    seq        bigint NOT NULL,
    tentativas int NOT NULL DEFAULT 1,
    erro       text NOT NULL,
    PRIMARY KEY (canal, id_evento)
);

-- outbox: o que o Parcelamentos tem a dizer, gravado na mesma transação da baixa
CREATE TABLE IF NOT EXISTS parc.saida (
    id          bigserial PRIMARY KEY,
    canal       text NOT NULL,
    evento      jsonb NOT NULL,
    enviado_em  timestamptz,
    recusado    text                            -- a central recusou: fica para alguém olhar, a fila anda
);

-- correção feita por uma pessoa, com o comprovante na mão
CREATE TABLE IF NOT EXISTS parc.correcao (
    id            bigserial PRIMARY KEY,
    parcelamento  text NOT NULL,
    numero        int NOT NULL,
    de            numeric(14,2),
    para          numeric(14,2) NOT NULL,
    por           text NOT NULL,
    motivo        text NOT NULL,
    em            timestamptz NOT NULL DEFAULT now()
);
