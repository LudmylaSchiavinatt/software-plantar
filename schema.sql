-- Banco de dados SQLite para gestão financeira da COOAIPRO

-- Tabela de Fornecedores (Produtores / Cooperados)
CREATE TABLE IF NOT EXISTS fornecedores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome VARCHAR(100) NOT NULL,
    cpf_cnpj VARCHAR(20),
    cidade VARCHAR(50),
    ativo BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Tabela de Usuários (login/cadastro — cooperado ou cooperativa)
CREATE TABLE IF NOT EXISTS usuarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email VARCHAR(120) NOT NULL UNIQUE,
    senha_hash VARCHAR(255) NOT NULL,
    tipo TEXT NOT NULL CHECK(tipo IN ('cooperado', 'cooperativa')),
    fornecedor_id INTEGER,              -- só preenchido quando tipo = 'cooperado'
    ativo BOOLEAN DEFAULT 1,
    aprovado BOOLEAN DEFAULT 0,         -- cooperado começa pendente até staff aprovar
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (fornecedor_id) REFERENCES fornecedores(id),
    -- garante que cooperado sempre tenha um fornecedor vinculado,
    -- e que cooperativa nunca precise disso
    CHECK (
        (tipo = 'cooperado' AND fornecedor_id IS NOT NULL)
        OR (tipo = 'cooperativa')
    )
);

-- Índice pra acelerar a busca "quais logins pertencem a esse fornecedor"
CREATE INDEX IF NOT EXISTS idx_usuarios_fornecedor ON usuarios(fornecedor_id);

-- Tabela de Clientes (Prefeituras/Instituições)
CREATE TABLE IF NOT EXISTS clientes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome VARCHAR(100) NOT NULL,
    tipo TEXT DEFAULT 'prefeitura',
    cidade VARCHAR(50),
    valor_total_contrato DECIMAL(10,2) DEFAULT 0,
    prazo_pagamento_dias INTEGER DEFAULT 30,
    ativo BOOLEAN DEFAULT 1
);
-- Tabela de Produtos
CREATE TABLE IF NOT EXISTS produtos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome VARCHAR(100) NOT NULL,
    variedade VARCHAR(50),
    unidade_medida TEXT DEFAULT 'kg',
    preco_referencia DECIMAL(10,2)
);

-- Tabela de Notas Fiscais (A Receber - das prefeituras)
CREATE TABLE IF NOT EXISTS notas_fiscais (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    numero_nota VARCHAR(20) NOT NULL UNIQUE,
    data_emissao DATE NOT NULL,
    cliente_id INTEGER NOT NULL,
    peso_liquido DECIMAL(10,2),
    quantidade_cx INTEGER,
    valor_total DECIMAL(10,2) NOT NULL,
    valor_frete DECIMAL(10,2) DEFAULT 0,
    previsao_pagamento DATE,
    data_pagamento DATE,
    status TEXT DEFAULT 'emitida',
    observacoes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (cliente_id) REFERENCES clientes(id)
);

-- Itens da Nota Fiscal (produtos incluídos)
CREATE TABLE IF NOT EXISTS nota_itens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nota_id INTEGER NOT NULL,
    produto_id INTEGER NOT NULL,
    quantidade DECIMAL(10,2) NOT NULL,
    valor_unitario DECIMAL(10,2) NOT NULL,
    valor_total DECIMAL(10,2) NOT NULL,
    FOREIGN KEY (nota_id) REFERENCES notas_fiscais(id),
    FOREIGN KEY (produto_id) REFERENCES produtos(id)
);

-- Tabela de Pagamentos a Fornecedores (A Pagar)
CREATE TABLE IF NOT EXISTS pagamentos_fornecedores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fornecedor_id INTEGER NOT NULL,
    nota_id INTEGER, -- referência à nota fiscal (quando aplicável)
    data_emissao DATE NOT NULL,
    prefeitura VARCHAR(100),
    produto_descricao TEXT,
    numero_nota_fornecedor VARCHAR(20),
    peso_liquido DECIMAL(10,2),
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
    FOREIGN KEY (fornecedor_id) REFERENCES fornecedores(id),
    FOREIGN KEY (nota_id) REFERENCES notas_fiscais(id)
);

-- View: custos agregados por nota fiscal
DROP VIEW IF EXISTS vw_custos_por_nota;
CREATE VIEW vw_custos_por_nota AS
SELECT
    pf.nota_id,
    COUNT(pf.id) as total_custos,
    IFNULL(SUM(pf.valor_compra), 0) as total_valor_compra,
    IFNULL(SUM(pf.funrural + pf.taxa_cooperativa + pf.desconto_aipro +
        pf.pg_caixa_entrega + pf.pg_frete_coop + pf.pg_caixa_papelao +
        pf.frete_cooaipro + pf.pg_extra_paa + pf.pg_frete_kits_mogi +
        pf.pg_ref_uso_hr + pf.pg_ref_uso_ford + pf.ref_frete_entrega_poa +
        pf.pg_afranio + pf.pg_frete_mogi + pf.vlr_pago_combustivel +
        pf.frete_entrega_sao_jose + pf.pg_uso_camara_fria +
        pf.pg_doacao_produtor + pf.pg_extra + pf.adiantamento +
        pf.descontos_outros), 0) as total_descontos,
    IFNULL(SUM(pf.valor_liquido), 0) as total_liquido_fornecedores
FROM pagamentos_fornecedores pf
WHERE pf.nota_id IS NOT NULL
GROUP BY pf.nota_id;
DROP VIEW IF EXISTS vw_resultados_por_prefeitura;
CREATE VIEW vw_resultados_por_prefeitura AS
SELECT
    c.id,
    c.nome as prefeitura,
    c.cidade,
    (SELECT COUNT(*) FROM notas_fiscais WHERE cliente_id = c.id) as total_notas,

    -- Valor Total do Contrato: agora é dado de entrada (input manual)
    IFNULL(c.valor_total_contrato, 0) as valor_total_contratos,

    -- Valor Entregue: soma das notas lançadas (não canceladas) ligadas ao contrato
    (SELECT IFNULL(SUM(valor_total), 0)
     FROM notas_fiscais
     WHERE cliente_id = c.id AND status != 'cancelada') as valor_recebido,

    -- Valor Aberto do Contrato: contrato menos entregue
    (IFNULL(c.valor_total_contrato, 0) -
     (SELECT IFNULL(SUM(valor_total), 0)
      FROM notas_fiscais
      WHERE cliente_id = c.id AND status != 'cancelada')) as valor_a_receber,

    (SELECT IFNULL(SUM(cn.total_liquido_fornecedores), 0)
     FROM notas_fiscais nf_sub
     JOIN vw_custos_por_nota cn ON cn.nota_id = nf_sub.id
     WHERE nf_sub.cliente_id = c.id) as total_custos,

    ((SELECT IFNULL(SUM(valor_total), 0)
      FROM notas_fiscais WHERE cliente_id = c.id AND status != 'cancelada') -
     (SELECT IFNULL(SUM(cn.total_liquido_fornecedores), 0)
      FROM notas_fiscais nf_sub
      JOIN vw_custos_por_nota cn ON cn.nota_id = nf_sub.id
      WHERE nf_sub.cliente_id = c.id)) as resultado_liquido
FROM clientes c;

-- View: relatório por cooperado
DROP VIEW IF EXISTS vw_relatorio_cooperado;
CREATE VIEW vw_relatorio_cooperado AS
SELECT
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
JOIN fornecedores f ON f.id = pf.fornecedor_id
GROUP BY f.id, f.nome;

-- View para resumo de pagamentos a fornecedores
-- (versão única — antes existia duplicada, a segunda sobrescrevia a primeira)
DROP VIEW IF EXISTS vw_pagamentos_resumo;
CREATE VIEW vw_pagamentos_resumo AS
SELECT
    f.id,
    f.nome as fornecedor,
    IFNULL(lim.limite_calculado, 0) as cota, -- PUXA A SOMA DAS NOTAS FISCAIS
    COUNT(pf.id) as total_pagamentos,
    IFNULL(SUM(pf.valor_compra), 0) as total_bruto,
    (IFNULL(lim.limite_calculado, 0) - IFNULL(SUM(pf.valor_compra), 0)) as saldo, -- CALCULA O SALDO: COTA - BRUTO
    IFNULL(SUM(pf.funrural + pf.taxa_cooperativa + pf.desconto_aipro +
         pf.pg_caixa_entrega + pf.pg_frete_coop + pf.pg_caixa_papelao +
         pf.frete_cooaipro + pf.pg_extra_paa + pf.pg_frete_kits_mogi +
         pf.pg_ref_uso_hr + pf.pg_ref_uso_ford + pf.ref_frete_entrega_poa +
         pf.pg_afranio + pf.pg_frete_mogi + pf.vlr_pago_combustivel +
         pf.frete_entrega_sao_jose + pf.pg_uso_camara_fria +
         pf.pg_doacao_produtor + pf.pg_extra + pf.adiantamento +
         pf.descontos_outros), 0) as total_descontos,
    IFNULL(SUM(pf.valor_liquido), 0) as total_liquido,
    IFNULL(SUM(CASE WHEN pf.status = 'pendente' THEN pf.valor_liquido ELSE 0 END), 0) as total_a_pagar
FROM fornecedores f
LEFT JOIN pagamentos_fornecedores pf ON f.id = pf.fornecedor_id
-- ESSA É A SUB-ROTINA QUE DESCOBRE A COTA BASEADA NAS NOTAS FISCAIS
LEFT JOIN (
    SELECT pf_dist.fornecedor_id, SUM(nf.valor_total) as limite_calculado
    FROM (SELECT DISTINCT fornecedor_id, nota_id FROM pagamentos_fornecedores WHERE nota_id IS NOT NULL) pf_dist
    JOIN notas_fiscais nf ON pf_dist.nota_id = nf.id
    GROUP BY pf_dist.fornecedor_id
) lim ON f.id = lim.fornecedor_id
GROUP BY f.id, f.nome, lim.limite_calculado;