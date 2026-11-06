-- ==========================================
-- 1. ESTRUTURA BASE E MULTI-TENANT
-- ==========================================

-- Tabela de Cooperativas (Inquilinos / Tenants)
-- ==========================================
-- 0. PRE-MIGRACAO (bancos criados em versoes anteriores)
-- As chaves compostas (id, cooperativa_id) precisam existir ANTES de qualquer
-- tabela nova que aponte para elas. Em banco novo, as tabelas ainda nao existem
-- e este bloco nao faz nada.
-- ==========================================
DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['fornecedores','clientes','produtos','notas_fiscais','usuarios']
    LOOP
        IF to_regclass(t) IS NOT NULL THEN
            EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS cooperativa_id INTEGER', t);
        END IF;
    END LOOP;

    IF to_regclass('fornecedores') IS NOT NULL THEN
        CREATE UNIQUE INDEX IF NOT EXISTS unq_fornecedor_id_cooperativa ON fornecedores(id, cooperativa_id);
    END IF;
    IF to_regclass('clientes') IS NOT NULL THEN
        CREATE UNIQUE INDEX IF NOT EXISTS unq_cliente_id_cooperativa ON clientes(id, cooperativa_id);
    END IF;
    IF to_regclass('produtos') IS NOT NULL THEN
        CREATE UNIQUE INDEX IF NOT EXISTS unq_produto_id_cooperativa ON produtos(id, cooperativa_id);
    END IF;
    IF to_regclass('notas_fiscais') IS NOT NULL THEN
        CREATE UNIQUE INDEX IF NOT EXISTS unq_nota_id_cooperativa ON notas_fiscais(id, cooperativa_id);
    END IF;
    IF to_regclass('usuarios') IS NOT NULL THEN
        CREATE UNIQUE INDEX IF NOT EXISTS unq_usuario_id_cooperativa ON usuarios(id, cooperativa_id);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS cooperativas (
    id SERIAL PRIMARY KEY,
    nome VARCHAR(150) NOT NULL,
    cnpj VARCHAR(20) UNIQUE NOT NULL,
    ativo BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Tabela de Fornecedores (Produtores / Cooperados)
CREATE TABLE IF NOT EXISTS fornecedores (
    id SERIAL PRIMARY KEY,
    cooperativa_id INTEGER NOT NULL,
    nome VARCHAR(100) NOT NULL,
    cpf_cnpj VARCHAR(20),
    cidade VARCHAR(50),
    ativo BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (cooperativa_id) REFERENCES cooperativas(id) ON DELETE CASCADE,
    CONSTRAINT unq_fornecedor_id_cooperativa UNIQUE (id, cooperativa_id)
);

-- Tabela de Usuários (login/cadastro — cooperado ou cooperativa)
CREATE TABLE IF NOT EXISTS usuarios (
    id SERIAL PRIMARY KEY,
    cooperativa_id INTEGER NOT NULL,
    email VARCHAR(120) NOT NULL,
    senha_hash VARCHAR(255) NOT NULL,
    tipo TEXT NOT NULL CHECK(tipo IN ('cooperado', 'admin')),
    fornecedor_id INTEGER,
    ativo BOOLEAN DEFAULT TRUE,
    aprovado BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (cooperativa_id) REFERENCES cooperativas(id) ON DELETE CASCADE,
    FOREIGN KEY (fornecedor_id, cooperativa_id) REFERENCES fornecedores(id, cooperativa_id),
    CONSTRAINT unq_usuario_email_cooperativa UNIQUE (cooperativa_id, email),
    CHECK (
        (tipo = 'cooperado' AND fornecedor_id IS NOT NULL)
        OR (tipo = 'admin')
    )
);

-- Tabela de Clientes (Prefeituras/Instituições)
CREATE TABLE IF NOT EXISTS clientes (
    id SERIAL PRIMARY KEY,
    cooperativa_id INTEGER NOT NULL,
    nome VARCHAR(100) NOT NULL,
    cpf_cnpj VARCHAR(20),
    tipo TEXT DEFAULT 'prefeitura',
    cidade VARCHAR(50),
    valor_total_contrato DECIMAL(10,2) DEFAULT 0,
    prazo_pagamento_dias INTEGER DEFAULT 30,
    ativo BOOLEAN DEFAULT TRUE,
    FOREIGN KEY (cooperativa_id) REFERENCES cooperativas(id) ON DELETE CASCADE,
    CONSTRAINT unq_cliente_id_cooperativa UNIQUE (id, cooperativa_id)
);

-- Tabela de Produtos
CREATE TABLE IF NOT EXISTS produtos (
    id SERIAL PRIMARY KEY,
    cooperativa_id INTEGER NOT NULL,
    nome VARCHAR(100) NOT NULL,
    variedade VARCHAR(50),
    unidade_medida TEXT DEFAULT 'kg',
    preco_referencia DECIMAL(10,2),
    FOREIGN KEY (cooperativa_id) REFERENCES cooperativas(id) ON DELETE CASCADE,
    CONSTRAINT unq_produto_cooperativa UNIQUE (cooperativa_id, nome),
    CONSTRAINT unq_produto_id_cooperativa UNIQUE (id, cooperativa_id)
);

-- Tabela de Notas Fiscais (A Receber - Vendas para as prefeituras)
CREATE TABLE IF NOT EXISTS notas_fiscais (
    id SERIAL PRIMARY KEY,
    cooperativa_id INTEGER NOT NULL,
    chave_acesso VARCHAR(44),
    numero_nota VARCHAR(20) NOT NULL,
    data_emissao DATE NOT NULL,
    cliente_id INTEGER NOT NULL,
    peso_liquido DECIMAL(10,2) DEFAULT 0,
    quantidade_cx INTEGER DEFAULT 0,
    valor_total DECIMAL(10,2) NOT NULL,
    valor_frete DECIMAL(10,2) DEFAULT 0,
    previsao_pagamento DATE,
    data_pagamento DATE,
    status TEXT DEFAULT 'emitida',
    observacoes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (cooperativa_id) REFERENCES cooperativas(id) ON DELETE CASCADE,
    FOREIGN KEY (cliente_id, cooperativa_id) REFERENCES clientes(id, cooperativa_id),
    CONSTRAINT unq_nota_id_cooperativa UNIQUE (id, cooperativa_id),
    CONSTRAINT unq_nota_numero_cooperativa UNIQUE (cooperativa_id, numero_nota)
);

-- Itens da Nota Fiscal
CREATE TABLE IF NOT EXISTS nota_itens (
    id SERIAL PRIMARY KEY,
    cooperativa_id INTEGER NOT NULL,
    nota_id INTEGER NOT NULL,
    produto_id INTEGER NOT NULL,
    quantidade DECIMAL(10,2) NOT NULL,
    valor_unitario DECIMAL(10,2) NOT NULL,
    valor_total DECIMAL(10,2) NOT NULL,
    FOREIGN KEY (cooperativa_id) REFERENCES cooperativas(id) ON DELETE CASCADE,
    FOREIGN KEY (nota_id, cooperativa_id) REFERENCES notas_fiscais(id, cooperativa_id) ON DELETE CASCADE,
    FOREIGN KEY (produto_id, cooperativa_id) REFERENCES produtos(id, cooperativa_id)
);

-- Tabela de Pagamentos a Fornecedores (A Pagar / Repasses de Compras)
CREATE TABLE IF NOT EXISTS pagamentos_fornecedores (
    id SERIAL PRIMARY KEY,
    cooperativa_id INTEGER NOT NULL,
    fornecedor_id INTEGER NOT NULL,
    nota_id INTEGER,
    cliente_id INTEGER,
    data_emissao DATE NOT NULL,
    prefeitura VARCHAR(100),
    produto_descricao TEXT,
    numero_nota_fornecedor VARCHAR(20),
    peso_liquido DECIMAL(10,2) DEFAULT 0,
    valor_compra DECIMAL(10,2) NOT NULL,

    -- Descontos
    funrural DECIMAL(10,2) DEFAULT 0,
    taxa_cooperativa DECIMAL(10,2) DEFAULT 0,
    desconto_aipro DECIMAL(10,2) DEFAULT 0,
    pg_caixa_entrega DECIMAL(10,2) DEFAULT 0,
    pg_frete_coop DECIMAL(10,2) DEFAULT 0,
    pg_caixa_papelao DECIMAL(10,2) DEFAULT 0,
    frete_cooaipro DECIMAL(10,2) DEFAULT 0,
    pg_extra_paa DECIMAL(10,2) DEFAULT 0,
    pg_frete_kits_mogi DECIMAL(10,2) DEFAULT 0,
    pg_ref_uso_hr DECIMAL(10,2) DEFAULT 0,
    pg_ref_uso_ford DECIMAL(10,2) DEFAULT 0,
    ref_frete_entrega_poa DECIMAL(10,2) DEFAULT 0,
    pg_afranio DECIMAL(10,2) DEFAULT 0,
    pg_frete_mogi DECIMAL(10,2) DEFAULT 0,
    vlr_pago_combustivel DECIMAL(10,2) DEFAULT 0,
    frete_entrega_sao_jose DECIMAL(10,2) DEFAULT 0,
    pg_uso_camara_fria DECIMAL(10,2) DEFAULT 0,
    pg_doacao_produtor DECIMAL(10,2) DEFAULT 0,
    pg_extra DECIMAL(10,2) DEFAULT 0,
    adiantamento DECIMAL(10,2) DEFAULT 0,
    descontos_outros DECIMAL(10,2) DEFAULT 0,

    -- Valores finais
    valor_liquido DECIMAL(10,2) NOT NULL,
    numero_cheque VARCHAR(30),
    data_pagamento DATE,
    status TEXT DEFAULT 'pendente',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (cooperativa_id) REFERENCES cooperativas(id) ON DELETE CASCADE,
    FOREIGN KEY (fornecedor_id, cooperativa_id) REFERENCES fornecedores(id, cooperativa_id),
    FOREIGN KEY (nota_id, cooperativa_id) REFERENCES notas_fiscais(id, cooperativa_id) ON DELETE SET NULL
);

-- Tabela de Estoque/Compras Pendentes
CREATE TABLE IF NOT EXISTS estoque_pendente (
    id SERIAL PRIMARY KEY,
    cooperativa_id INTEGER NOT NULL,
    fornecedor_id INTEGER NOT NULL,
    chave_acesso VARCHAR(44),
    numero_nota_fornecedor VARCHAR(20),
    data_emissao DATE,
    produto_descricao VARCHAR(100) NOT NULL,
    quantidade DECIMAL(10,2) NOT NULL,
    valor_unitario DECIMAL(10,2) NOT NULL,
    valor_total DECIMAL(10,2) NOT NULL,
    funrural DECIMAL(10,2) DEFAULT 0,
    valor_liquido DECIMAL(10,2) NOT NULL,
    status VARCHAR(30) DEFAULT 'aguardando_repasse',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (cooperativa_id) REFERENCES cooperativas(id) ON DELETE CASCADE,
    FOREIGN KEY (fornecedor_id, cooperativa_id) REFERENCES fornecedores(id, cooperativa_id)
);


-- ==========================================
-- 2. ÍNDICES DE PERFORMANCE (OBRIGATÓRIOS)
-- ==========================================
CREATE INDEX IF NOT EXISTS idx_usuarios_coop ON usuarios(cooperativa_id);
CREATE INDEX IF NOT EXISTS idx_usuarios_fornecedor ON usuarios(fornecedor_id);
CREATE INDEX IF NOT EXISTS idx_fornecedores_coop ON fornecedores(cooperativa_id);
CREATE INDEX IF NOT EXISTS idx_clientes_coop ON clientes(cooperativa_id);
CREATE INDEX IF NOT EXISTS idx_produtos_coop ON produtos(cooperativa_id);
CREATE INDEX IF NOT EXISTS idx_notas_fiscais_coop ON notas_fiscais(cooperativa_id);
CREATE INDEX IF NOT EXISTS idx_pagamentos_coop ON pagamentos_fornecedores(cooperativa_id);
CREATE INDEX IF NOT EXISTS idx_estoque_pendente_coop ON estoque_pendente(cooperativa_id);


-- ==========================================
-- 3. VIEWS ATUALIZADAS COM ISOLAMENTO
-- ==========================================

-- View: custos agregados por nota fiscal
DROP VIEW IF EXISTS vw_custos_por_nota CASCADE;
CREATE VIEW vw_custos_por_nota AS
SELECT
    pf.cooperativa_id,
    pf.nota_id,
    COUNT(pf.id) as total_custos,
    COALESCE(SUM(pf.valor_compra), 0) as total_valor_compra,
    COALESCE(SUM(pf.funrural + pf.taxa_cooperativa + pf.desconto_aipro +
        pf.pg_caixa_entrega + pf.pg_frete_coop + pf.pg_caixa_papelao +
        pf.frete_cooaipro + pf.pg_extra_paa + pf.pg_frete_kits_mogi +
        pf.pg_ref_uso_hr + pf.pg_ref_uso_ford + pf.ref_frete_entrega_poa +
        pf.pg_afranio + pf.pg_frete_mogi + pf.vlr_pago_combustivel +
        pf.frete_entrega_sao_jose + pf.pg_uso_camara_fria +
        pf.pg_doacao_produtor + pf.pg_extra + pf.adiantamento +
        pf.descontos_outros), 0) as total_descontos,
    COALESCE(SUM(pf.valor_liquido), 0) as total_liquido_fornecedores
FROM pagamentos_fornecedores pf
WHERE pf.nota_id IS NOT NULL
GROUP BY pf.cooperativa_id, pf.nota_id;

-- View: resultados por prefeitura
DROP VIEW IF EXISTS vw_resultados_por_prefeitura CASCADE;
CREATE VIEW vw_resultados_por_prefeitura AS
SELECT
    c.cooperativa_id,
    c.id,
    c.nome as prefeitura,
    c.cidade,
    (SELECT COUNT(*) FROM notas_fiscais WHERE cliente_id = c.id AND cooperativa_id = c.cooperativa_id) as total_notas,
    COALESCE(c.valor_total_contrato, 0) as valor_total_contratos,
    (SELECT COALESCE(SUM(valor_total), 0)
     FROM notas_fiscais
     WHERE cliente_id = c.id AND cooperativa_id = c.cooperativa_id AND status != 'cancelada') as valor_recebido,
    (COALESCE(c.valor_total_contrato, 0) -
     (SELECT COALESCE(SUM(valor_total), 0)
      FROM notas_fiscais
      WHERE cliente_id = c.id AND cooperativa_id = c.cooperativa_id AND status != 'cancelada')) as valor_a_receber,
    (SELECT COALESCE(SUM(cn.total_liquido_fornecedores), 0)
     FROM notas_fiscais nf_sub
     JOIN vw_custos_por_nota cn ON cn.nota_id = nf_sub.id AND cn.cooperativa_id = nf_sub.cooperativa_id
     WHERE nf_sub.cliente_id = c.id AND nf_sub.cooperativa_id = c.cooperativa_id) as total_custos,
    ((SELECT COALESCE(SUM(valor_total), 0)
      FROM notas_fiscais WHERE cliente_id = c.id AND cooperativa_id = c.cooperativa_id AND status != 'cancelada') -
     (SELECT COALESCE(SUM(cn.total_liquido_fornecedores), 0)
      FROM notas_fiscais nf_sub
      JOIN vw_custos_por_nota cn ON cn.nota_id = nf_sub.id AND cn.cooperativa_id = nf_sub.cooperativa_id
      WHERE nf_sub.cliente_id = c.id AND nf_sub.cooperativa_id = c.cooperativa_id)) as resultado_liquido
FROM clientes c;

-- View: relatório por cooperado
DROP VIEW IF EXISTS vw_relatorio_cooperado CASCADE;
CREATE VIEW vw_relatorio_cooperado AS
SELECT
    pf.cooperativa_id,
    f.id as fornecedor_id,
    f.nome as cooperado,
    COUNT(DISTINCT CASE WHEN pf.nota_id IS NOT NULL THEN pf.nota_id END) as total_notas,
    SUM(pf.valor_compra) as total_bruto,
    SUM(
        pf.funrural + pf.taxa_cooperativa + pf.desconto_aipro +
        pf.pg_caixa_entrega + pf.pg_frete_coop + pf.pg_caixa_papelao +
        pf.frete_cooaipro + pf.pg_extra_paa + pf.pg_frete_kits_mogi +
        pf.pg_ref_uso_hr + pf.pg_ref_uso_ford + pf.ref_frete_entrega_poa +
        pf.pg_afranio + pf.pg_frete_mogi + pf.vlr_pago_combustivel +
        pf.frete_entrega_sao_jose + pf.pg_uso_camara_fria +
        pf.pg_doacao_produtor + pf.pg_extra + pf.adiantamento +
        pf.descontos_outros
    ) as total_descontos,
    SUM(pf.valor_liquido) as total_liquido,
    SUM(CASE
        WHEN pf.status = 'pendente' THEN pf.valor_liquido
        ELSE 0
    END) as total_a_pagar
FROM pagamentos_fornecedores pf
JOIN fornecedores f ON f.id = pf.fornecedor_id AND f.cooperativa_id = pf.cooperativa_id
GROUP BY pf.cooperativa_id, f.id, f.nome;

-- View: resumo de pagamentos
DROP VIEW IF EXISTS vw_pagamentos_resumo CASCADE;
CREATE VIEW vw_pagamentos_resumo AS
SELECT
    f.cooperativa_id,
    f.id,
    f.nome as fornecedor,
    COALESCE(lim.limite_calculado, 0) as cota,
    COUNT(pf.id) as total_pagamentos,
    COALESCE(SUM(pf.valor_compra), 0) as total_bruto,
    (COALESCE(lim.limite_calculado, 0) - COALESCE(SUM(pf.valor_compra), 0)) as saldo,
    COALESCE(SUM(pf.funrural + pf.taxa_cooperativa + pf.desconto_aipro +
         pf.pg_caixa_entrega + pf.pg_frete_coop + pf.pg_caixa_papelao +
         pf.frete_cooaipro + pf.pg_extra_paa + pf.pg_frete_kits_mogi +
         pf.pg_ref_uso_hr + pf.pg_ref_uso_ford + pf.ref_frete_entrega_poa +
         pf.pg_afranio + pf.pg_frete_mogi + pf.vlr_pago_combustivel +
         pf.frete_entrega_sao_jose + pf.pg_uso_camara_fria +
         pf.pg_doacao_produtor + pf.pg_extra + pf.adiantamento +
         pf.descontos_outros), 0) as total_descontos,
    COALESCE(SUM(pf.valor_liquido), 0) as total_liquido,
    COALESCE(SUM(CASE WHEN pf.status = 'pendente' THEN pf.valor_liquido ELSE 0 END), 0) as total_a_pagar
FROM fornecedores f
LEFT JOIN pagamentos_fornecedores pf ON f.id = pf.fornecedor_id AND f.cooperativa_id = pf.cooperativa_id
LEFT JOIN (
    SELECT pf_dist.cooperativa_id, pf_dist.fornecedor_id, SUM(nf.valor_total) as limite_calculado
    FROM (SELECT DISTINCT cooperativa_id, fornecedor_id, nota_id FROM pagamentos_fornecedores WHERE nota_id IS NOT NULL) pf_dist
    JOIN notas_fiscais nf ON pf_dist.nota_id = nf.id AND pf_dist.cooperativa_id = nf.cooperativa_id
    GROUP BY pf_dist.cooperativa_id, pf_dist.fornecedor_id
) lim ON f.id = lim.fornecedor_id AND f.cooperativa_id = lim.cooperativa_id
GROUP BY f.cooperativa_id, f.id, f.nome, lim.limite_calculado;

-- ==========================================
-- 4. ESTRUTURAS COMPLEMENTARES / MIGRAÇÃO
-- ==========================================

-- O app utiliza esta tabela para as cotas de cada cooperado por contrato.
CREATE TABLE IF NOT EXISTS contrato_cooperado (
    id SERIAL PRIMARY KEY,
    cooperativa_id INTEGER NOT NULL,
    cliente_id INTEGER NOT NULL,
    fornecedor_id INTEGER NOT NULL,
    cota DECIMAL(10,2) NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (cooperativa_id) REFERENCES cooperativas(id) ON DELETE CASCADE,
    FOREIGN KEY (cliente_id, cooperativa_id) REFERENCES clientes(id, cooperativa_id) ON DELETE CASCADE,
    FOREIGN KEY (fornecedor_id, cooperativa_id) REFERENCES fornecedores(id, cooperativa_id) ON DELETE CASCADE,
    CONSTRAINT unq_contrato_id_cooperativa UNIQUE (id, cooperativa_id),
    CONSTRAINT unq_contrato_cooperado UNIQUE (cooperativa_id, cliente_id, fornecedor_id)
);

-- --------------------------
-- Migração de banco existente
-- --------------------------

-- nota_itens e pagamentos_fornecedores precisam carregar explicitamente o tenant.
ALTER TABLE nota_itens
    ADD COLUMN IF NOT EXISTS cooperativa_id INTEGER;

UPDATE nota_itens ni
SET cooperativa_id = nf.cooperativa_id
FROM notas_fiscais nf
WHERE nf.id = ni.nota_id
  AND ni.cooperativa_id IS NULL;

ALTER TABLE pagamentos_fornecedores
    ADD COLUMN IF NOT EXISTS cliente_id INTEGER;

UPDATE pagamentos_fornecedores pf
SET cliente_id = nf.cliente_id
FROM notas_fiscais nf
WHERE pf.nota_id = nf.id
  AND pf.cliente_id IS NULL;

-- Caso a tabela já exista de uma versão anterior, adiciona o tenant.
ALTER TABLE contrato_cooperado
    ADD COLUMN IF NOT EXISTS cooperativa_id INTEGER;

UPDATE contrato_cooperado cc
SET cooperativa_id = c.cooperativa_id
FROM clientes c
WHERE c.id = cc.cliente_id
  AND cc.cooperativa_id IS NULL;

-- Uniques por tenant.
ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS usuarios_email_key;
CREATE UNIQUE INDEX IF NOT EXISTS unq_usuarios_cooperativa_email
    ON usuarios(cooperativa_id, email);

CREATE UNIQUE INDEX IF NOT EXISTS unq_fornecedores_cooperativa_cpf
    ON fornecedores(cooperativa_id, cpf_cnpj)
    WHERE cpf_cnpj IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS unq_notas_cooperativa_numero
    ON notas_fiscais(cooperativa_id, numero_nota);

-- Chaves compostas: os IDs sozinhos não podem apontar para outra cooperativa.
CREATE UNIQUE INDEX IF NOT EXISTS unq_fornecedor_id_cooperativa
    ON fornecedores(id, cooperativa_id);
CREATE UNIQUE INDEX IF NOT EXISTS unq_cliente_id_cooperativa
    ON clientes(id, cooperativa_id);
CREATE UNIQUE INDEX IF NOT EXISTS unq_produto_id_cooperativa
    ON produtos(id, cooperativa_id);
CREATE UNIQUE INDEX IF NOT EXISTS unq_nota_id_cooperativa
    ON notas_fiscais(id, cooperativa_id);
CREATE UNIQUE INDEX IF NOT EXISTS unq_usuario_id_cooperativa
    ON usuarios(id, cooperativa_id);

ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS usuarios_fornecedor_id_fkey;
ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS fk_usuario_fornecedor_tenant;
ALTER TABLE usuarios
    ADD CONSTRAINT fk_usuario_fornecedor_tenant
    FOREIGN KEY (fornecedor_id, cooperativa_id)
    REFERENCES fornecedores(id, cooperativa_id);

ALTER TABLE notas_fiscais DROP CONSTRAINT IF EXISTS notas_fiscais_cliente_id_fkey;
ALTER TABLE notas_fiscais DROP CONSTRAINT IF EXISTS fk_nota_cliente_tenant;
ALTER TABLE notas_fiscais
    ADD CONSTRAINT fk_nota_cliente_tenant
    FOREIGN KEY (cliente_id, cooperativa_id)
    REFERENCES clientes(id, cooperativa_id);

ALTER TABLE nota_itens
    ALTER COLUMN cooperativa_id SET NOT NULL;
ALTER TABLE nota_itens DROP CONSTRAINT IF EXISTS nota_itens_nota_id_fkey;
ALTER TABLE nota_itens DROP CONSTRAINT IF EXISTS nota_itens_produto_id_fkey;
ALTER TABLE nota_itens DROP CONSTRAINT IF EXISTS fk_nota_item_nota_tenant;
ALTER TABLE nota_itens
    ADD CONSTRAINT fk_nota_item_nota_tenant
    FOREIGN KEY (nota_id, cooperativa_id)
    REFERENCES notas_fiscais(id, cooperativa_id)
    ON DELETE CASCADE;
ALTER TABLE nota_itens DROP CONSTRAINT IF EXISTS fk_nota_item_produto_tenant;
ALTER TABLE nota_itens
    ADD CONSTRAINT fk_nota_item_produto_tenant
    FOREIGN KEY (produto_id, cooperativa_id)
    REFERENCES produtos(id, cooperativa_id);

ALTER TABLE pagamentos_fornecedores DROP CONSTRAINT IF EXISTS pagamentos_fornecedores_fornecedor_id_fkey;
ALTER TABLE pagamentos_fornecedores DROP CONSTRAINT IF EXISTS pagamentos_fornecedores_nota_id_fkey;
ALTER TABLE pagamentos_fornecedores DROP CONSTRAINT IF EXISTS pagamentos_fornecedores_cliente_id_fkey;
ALTER TABLE pagamentos_fornecedores DROP CONSTRAINT IF EXISTS fk_pagamento_fornecedor_tenant;
ALTER TABLE pagamentos_fornecedores
    ADD CONSTRAINT fk_pagamento_fornecedor_tenant
    FOREIGN KEY (fornecedor_id, cooperativa_id)
    REFERENCES fornecedores(id, cooperativa_id);
ALTER TABLE pagamentos_fornecedores DROP CONSTRAINT IF EXISTS fk_pagamento_nota_tenant;
ALTER TABLE pagamentos_fornecedores
    ADD CONSTRAINT fk_pagamento_nota_tenant
    FOREIGN KEY (nota_id, cooperativa_id)
    REFERENCES notas_fiscais(id, cooperativa_id)
    ON DELETE SET NULL;
ALTER TABLE pagamentos_fornecedores DROP CONSTRAINT IF EXISTS fk_pagamento_cliente_tenant;
ALTER TABLE pagamentos_fornecedores
    ADD CONSTRAINT fk_pagamento_cliente_tenant
    FOREIGN KEY (cliente_id, cooperativa_id)
    REFERENCES clientes(id, cooperativa_id);

ALTER TABLE estoque_pendente DROP CONSTRAINT IF EXISTS estoque_pendente_fornecedor_id_fkey;
ALTER TABLE estoque_pendente DROP CONSTRAINT IF EXISTS fk_estoque_fornecedor_tenant;
ALTER TABLE estoque_pendente
    ADD CONSTRAINT fk_estoque_fornecedor_tenant
    FOREIGN KEY (fornecedor_id, cooperativa_id)
    REFERENCES fornecedores(id, cooperativa_id);

ALTER TABLE contrato_cooperado
    ALTER COLUMN cooperativa_id SET NOT NULL;
ALTER TABLE contrato_cooperado DROP CONSTRAINT IF EXISTS contrato_cooperado_cliente_id_fkey;
ALTER TABLE contrato_cooperado DROP CONSTRAINT IF EXISTS contrato_cooperado_fornecedor_id_fkey;
ALTER TABLE contrato_cooperado DROP CONSTRAINT IF EXISTS fk_contrato_cliente_tenant;
ALTER TABLE contrato_cooperado
    ADD CONSTRAINT fk_contrato_cliente_tenant
    FOREIGN KEY (cliente_id, cooperativa_id)
    REFERENCES clientes(id, cooperativa_id)
    ON DELETE CASCADE;
ALTER TABLE contrato_cooperado DROP CONSTRAINT IF EXISTS fk_contrato_fornecedor_tenant;
ALTER TABLE contrato_cooperado
    ADD CONSTRAINT fk_contrato_fornecedor_tenant
    FOREIGN KEY (fornecedor_id, cooperativa_id)
    REFERENCES fornecedores(id, cooperativa_id)
    ON DELETE CASCADE;

CREATE UNIQUE INDEX IF NOT EXISTS unq_contrato_cooperado
    ON contrato_cooperado(cooperativa_id, cliente_id, fornecedor_id);

-- Papel "cooperativa" passa a ser "admin".
-- Remove as duas regras antigas antes de converter os dados (bancos criados na versao anterior).
ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS usuarios_check;
ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS usuarios_tipo_check;
UPDATE usuarios SET tipo = 'admin' WHERE tipo = 'cooperativa';
ALTER TABLE usuarios
    ADD CONSTRAINT usuarios_tipo_check
    CHECK (tipo IN ('cooperado', 'admin'));
ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS usuarios_check;
ALTER TABLE usuarios
    ADD CONSTRAINT usuarios_check
    CHECK ((tipo = 'cooperado' AND fornecedor_id IS NOT NULL) OR (tipo = 'admin'));