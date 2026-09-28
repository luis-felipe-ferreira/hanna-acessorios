ALTER TABLE vendas
ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'pendente';

UPDATE vendas
SET status = 'pendente'
WHERE status IS NULL;

ALTER TABLE vendas
ALTER COLUMN status SET DEFAULT 'pendente';

CREATE UNIQUE INDEX IF NOT EXISTS idx_vendas_asaas_id_unique
ON vendas (asaas_id);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'produtos_quantidade_nao_negativa'
    ) THEN
        ALTER TABLE produtos
        ADD CONSTRAINT produtos_quantidade_nao_negativa
        CHECK (quantidade >= 0)
        NOT VALID;
    END IF;
END
$$;

ALTER TABLE produtos
VALIDATE CONSTRAINT produtos_quantidade_nao_negativa;
