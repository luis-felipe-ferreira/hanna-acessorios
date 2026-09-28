CREATE TABLE IF NOT EXISTS categorias (
    id SERIAL PRIMARY KEY,
    nome TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS produtos (
    id SERIAL PRIMARY KEY,
    nome TEXT NOT NULL,
    preco NUMERIC(10, 2) NOT NULL CHECK (preco >= 0),
    categoria_id INTEGER REFERENCES categorias(id) ON DELETE SET NULL,
    foto_url TEXT NOT NULL,
    quantidade INTEGER NOT NULL DEFAULT 0 CHECK (quantidade >= 0),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS vendas (
    id SERIAL PRIMARY KEY,
    asaas_id TEXT NOT NULL UNIQUE,
    produto_id INTEGER REFERENCES produtos(id) ON DELETE SET NULL,
    cliente_nome TEXT NOT NULL,
    cliente_cpf TEXT,
    cliente_email TEXT,
    cliente_telefone TEXT,
    valor NUMERIC(10, 2) NOT NULL CHECK (valor >= 0),
    status TEXT NOT NULL DEFAULT 'pendente',
    entrega_tipo TEXT NOT NULL DEFAULT 'entrega',
    entrega_cep TEXT,
    entrega_rua TEXT,
    entrega_numero TEXT,
    entrega_complemento TEXT,
    entrega_bairro TEXT,
    entrega_cidade TEXT,
    entrega_estado TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_produtos_categoria_id
ON produtos (categoria_id);

CREATE INDEX IF NOT EXISTS idx_vendas_produto_id
ON vendas (produto_id);

CREATE INDEX IF NOT EXISTS idx_vendas_status
ON vendas (status);

CREATE INDEX IF NOT EXISTS idx_vendas_created_at
ON vendas (created_at DESC);
