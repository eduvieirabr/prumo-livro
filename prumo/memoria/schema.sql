-- Memória Prumo (capítulo 10). PostgreSQL 16 + pgvector + unaccent.
-- Rodado pelo dono do banco. A aplicação conecta como mem_app, sem superpoderes.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS unaccent;

CREATE OR REPLACE FUNCTION mem_sem_acento(t text) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$ SELECT public.unaccent('public.unaccent', t) $$;

CREATE TABLE IF NOT EXISTS item (
    id                bigserial PRIMARY KEY,
    dono              text NOT NULL,                        -- sub do Prumo ID
    escopo            text NOT NULL CHECK (escopo IN ('pessoal', 'empresa')),
    restrito          boolean NOT NULL DEFAULT false,       -- empresa, mas só supervisor+
    compartilhado_com text[] NOT NULL DEFAULT '{}',          -- subs que podem ver
    titulo            text NOT NULL,
    criado_em         timestamptz NOT NULL DEFAULT now()
);
-- o cliente de quem o item fala; NULL = conhecimento interno do escritório, sem dado de cliente
ALTER TABLE item ADD COLUMN IF NOT EXISTS empresa text;

CREATE TABLE IF NOT EXISTS trecho (
    id      bigserial PRIMARY KEY,
    item    bigint NOT NULL REFERENCES item(id) ON DELETE CASCADE,
    ordem   int NOT NULL,
    texto   text NOT NULL,
    extra   text NOT NULL DEFAULT '',                       -- variantes de busca (CNPJ só dígitos)
    busca   tsvector GENERATED ALWAYS AS
            (to_tsvector('portuguese', mem_sem_acento(texto || ' ' || extra))) STORED,
    vetor   vector(64)                                      -- preenchido quando há embedding
);
CREATE INDEX IF NOT EXISTS trecho_busca ON trecho USING gin (busca);
CREATE INDEX IF NOT EXISTS trecho_vetor ON trecho USING hnsw (vetor vector_cosine_ops);

-- Quem está pedindo, informado pela aplicação a cada transação (set_config local).
CREATE OR REPLACE FUNCTION mem_sub() RETURNS text LANGUAGE sql STABLE
    AS $$ SELECT nullif(current_setting('mem.sub', true), '') $$;
CREATE OR REPLACE FUNCTION mem_nivel() RETURNS int LANGUAGE sql STABLE
    AS $$ SELECT coalesce(array_position(ARRAY['leitura', 'operador', 'supervisor', 'admin'],
                                          nullif(current_setting('mem.papel', true), '')), 0) $$;

ALTER TABLE item ENABLE ROW LEVEL SECURITY;
ALTER TABLE item FORCE ROW LEVEL SECURITY;
ALTER TABLE trecho ENABLE ROW LEVEL SECURITY;
ALTER TABLE trecho FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS ver ON item;
CREATE POLICY ver ON item FOR SELECT USING (
    mem_sub() IS NOT NULL                -- sem pessoa informada, nada aparece
    AND (dono = mem_sub()
         OR mem_sub() = ANY (compartilhado_com)
         OR (escopo = 'empresa' AND (NOT restrito OR mem_nivel() >= 3)))
);
DROP POLICY IF EXISTS criar ON item;
CREATE POLICY criar ON item FOR INSERT WITH CHECK (
    dono = mem_sub() AND (escopo = 'pessoal' OR mem_nivel() >= 2)
);
DROP POLICY IF EXISTS alterar ON item;
CREATE POLICY alterar ON item FOR UPDATE
    USING (mem_sub() IS NOT NULL
           AND (dono = mem_sub() OR (escopo = 'empresa' AND mem_nivel() >= 3)))
    WITH CHECK (escopo = 'pessoal' OR mem_nivel() >= 2);
DROP POLICY IF EXISTS apagar ON item;
CREATE POLICY apagar ON item FOR DELETE
    USING (mem_sub() IS NOT NULL
           AND (dono = mem_sub() OR (escopo = 'empresa' AND mem_nivel() >= 3)));

-- Trecho herda a visibilidade do item: a subconsulta também passa pela política de item.
DROP POLICY IF EXISTS ver ON trecho;
CREATE POLICY ver ON trecho FOR SELECT
    USING (EXISTS (SELECT 1 FROM item i WHERE i.id = trecho.item));
-- Escreve no trecho quem pode alterar o item: o dono, ou supervisor+ em item da empresa.
DROP POLICY IF EXISTS escrever ON trecho;
CREATE POLICY escrever ON trecho FOR ALL
    USING (EXISTS (SELECT 1 FROM item i WHERE i.id = trecho.item
                   AND (i.dono = mem_sub() OR (i.escopo = 'empresa' AND mem_nivel() >= 3))))
    WITH CHECK (EXISTS (SELECT 1 FROM item i WHERE i.id = trecho.item
                        AND (i.dono = mem_sub() OR (i.escopo = 'empresa' AND mem_nivel() >= 3))));

-- O que a política não expressa bem, o gatilho garante: dono não muda; só o dono
-- muda o escopo e a lista de quem vê; supervisor pode marcar como restrito, mas
-- só o dono tira a marca.
CREATE OR REPLACE FUNCTION mem_proteger_item() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.dono <> OLD.dono THEN
        RAISE EXCEPTION 'o dono de um item não muda';
    END IF;
    IF (NEW.escopo, NEW.compartilhado_com) IS DISTINCT FROM (OLD.escopo, OLD.compartilhado_com)
       AND OLD.dono IS DISTINCT FROM mem_sub() THEN
        RAISE EXCEPTION 'só o dono muda o escopo e com quem o item é compartilhado';
    END IF;
    IF OLD.restrito AND NOT NEW.restrito AND OLD.dono IS DISTINCT FROM mem_sub() THEN
        RAISE EXCEPTION 'só o dono tira a marca de restrito';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS proteger_item ON item;
CREATE TRIGGER proteger_item BEFORE UPDATE ON item
    FOR EACH ROW EXECUTE FUNCTION mem_proteger_item();

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mem_app') THEN
        CREATE ROLE mem_app LOGIN;
    END IF;
END $$;
GRANT SELECT, INSERT, UPDATE, DELETE ON item, trecho TO mem_app;
GRANT USAGE ON SEQUENCE item_id_seq, trecho_id_seq TO mem_app;
