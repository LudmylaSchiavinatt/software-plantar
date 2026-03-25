-- schema.sql
-- Banco de dados SQLite para gestão financeira da COOAIPRO

-- Tabela de Fornecedores (Produtores)
CREATE TABLE IF NOT EXISTS fornecedores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome VARCHAR(100) NOT NULL,
    cpf_cnpj VARCHAR(20),
    cidade VARCHAR(50),
    ativo BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Tabela de Clientes (Prefeituras/Instituições)
CREATE TABLE IF NOT EXISTS clientes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome VARCHAR(100) NOT NULL,
    tipo TEXT DEFAULT 'prefeitura',
    cidade VARCHAR(50),
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

-- Tabela de Contratos por Prefeitura (resultados agregados com custos descontados)
DROP VIEW IF EXISTS vw_resultados_por_prefeitura;
CREATE VIEW vw_resultados_por_prefeitura AS
SELECT
    c.id,
    c.nome as prefeitura,
    c.cidade,
    COUNT(DISTINCT nf.id) as total_notas,
    IFNULL(SUM(nf.valor_total), 0) as valor_total_contratos,
    IFNULL(SUM(nf.valor_frete), 0) as total_frete,
    IFNULL(SUM(CASE WHEN nf.status = 'paga' THEN nf.valor_total ELSE 0 END), 0) as valor_recebido,
    IFNULL(SUM(CASE WHEN nf.status IN ('pendente','emitida') THEN nf.valor_total ELSE 0 END), 0) as valor_a_receber,
    IFNULL(SUM(cn.total_liquido_fornecedores), 0) as total_custos,
    IFNULL(SUM(nf.valor_total), 0) - IFNULL(SUM(cn.total_liquido_fornecedores), 0) as resultado_liquido,
    MAX(nf.data_emissao) as ultima_nota
FROM clientes c
LEFT JOIN notas_fiscais nf ON c.id = nf.cliente_id
LEFT JOIN vw_custos_por_nota cn ON cn.nota_id = nf.id
GROUP BY c.id, c.nome, c.cidade;

-- View para resumo de pagamentos a fornecedores
DROP VIEW IF EXISTS vw_pagamentos_resumo;
CREATE VIEW vw_pagamentos_resumo AS
SELECT 
    f.nome as fornecedor,
    COUNT(pf.id) as total_pagamentos,
    SUM(pf.valor_compra) as total_bruto,
    SUM(pf.funrural + pf.taxa_cooperativa + pf.desconto_aipro + 
        pf.pg_caixa_entrega + pf.pg_frete_coop + pf.pg_caixa_papelao + 
        pf.frete_cooaipro + pf.pg_extra_paa + pf.pg_frete_kits_mogi + 
        pf.pg_ref_uso_hr + pf.pg_ref_uso_ford + pf.ref_frete_entrega_poa + 
        pf.pg_afranio + pf.pg_frete_mogi + pf.vlr_pago_combustivel + 
        pf.frete_entrega_sao_jose + pf.pg_uso_camara_fria + 
        pf.pg_doacao_produtor + pf.pg_extra + pf.adiantamento + 
        pf.descontos_outros) as total_descontos,
    SUM(pf.valor_liquido) as total_liquido,
    SUM(CASE WHEN pf.status = 'pendente' THEN pf.valor_liquido ELSE 0 END) as total_a_pagar
FROM pagamentos_fornecedores pf
JOIN fornecedores f ON pf.fornecedor_id = f.id
GROUP BY f.id, f.nome;