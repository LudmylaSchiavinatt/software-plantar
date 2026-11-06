-- Migração aditiva e preservadora: histórico de repasses efetivamente realizados.
-- Não apaga nem recria tabelas existentes. Faça backup antes de aplicar.
-- Pode ser executada mais de uma vez (idempotente).
BEGIN;

-- Necessários para as FKs compostas (id, cooperativa_id); não alteram dados.
-- (id já é chave primária, então não há risco de duplicidade ao criar estes índices.)
CREATE UNIQUE INDEX IF NOT EXISTS unq_pagamento_id_cooperativa
    ON pagamentos_fornecedores(id, cooperativa_id);

CREATE UNIQUE INDEX IF NOT EXISTS unq_fornecedor_id_cooperativa
    ON fornecedores(id, cooperativa_id);

CREATE TABLE IF NOT EXISTS repasses_cooperados (
    id BIGSERIAL PRIMARY KEY,
    cooperativa_id INTEGER NOT NULL REFERENCES cooperativas(id) ON DELETE CASCADE,
    pagamento_id INTEGER NOT NULL,
    fornecedor_id INTEGER NOT NULL,
    valor NUMERIC(12,2) NOT NULL CHECK (valor > 0),
    data_pagamento DATE NOT NULL,
    forma_pagamento VARCHAR(40) NOT NULL,
    referencia VARCHAR(200),
    observacoes VARCHAR(500),
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- RESTRICT de propósito: protege o histórico de pagamentos. O app (DELETE de entrega)
    -- apaga os repasses explicitamente, após confirmação do usuário, antes de apagar a entrega.
    CONSTRAINT fk_repasse_pagamento_tenant
        FOREIGN KEY (pagamento_id, cooperativa_id)
        REFERENCES pagamentos_fornecedores(id, cooperativa_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_repasse_fornecedor_tenant
        FOREIGN KEY (fornecedor_id, cooperativa_id)
        REFERENCES fornecedores(id, cooperativa_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_repasses_pagamento_tenant
    ON repasses_cooperados(cooperativa_id, pagamento_id, data_pagamento);
CREATE INDEX IF NOT EXISTS idx_repasses_fornecedor_tenant
    ON repasses_cooperados(cooperativa_id, fornecedor_id, data_pagamento);

COMMIT;
