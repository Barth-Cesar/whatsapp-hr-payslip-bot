-- =====================================================================
-- Chatbot RH - Holerites via WhatsApp | Autor: César Barth · 2026
-- Estrutura do banco PostgreSQL usada pelo fluxo n8n e pelo separador
-- de holerites (Python).
-- =====================================================================

-- Cadastro de colaboradores (importado da base do RH)
CREATE TABLE IF NOT EXISTS colaboradores (
    id          SERIAL PRIMARY KEY,
    matricula   TEXT UNIQUE,              -- "Código" impresso no holerite
    nome        TEXT NOT NULL,
    cpf         TEXT NOT NULL,            -- usado só para confirmar identidade (5 primeiros dígitos)
    telefone    TEXT NOT NULL,            -- identifica quem está falando no WhatsApp
    ativo       BOOLEAN NOT NULL DEFAULT true
);

-- Holerites publicados (um registro por colaborador / mês / tipo de folha)
CREATE TABLE IF NOT EXISTS folhas_pagamento (
    id               SERIAL PRIMARY KEY,
    id_colaborador   INTEGER NOT NULL REFERENCES colaboradores(id),
    mes_referencia   DATE    NOT NULL,              -- sempre dia 01 do mês
    tipo_folha       TEXT    NOT NULL DEFAULT 'Mensal',
    caminho_arquivo  TEXT    NOT NULL,              -- caminho do PDF separado no disco
    criado_em        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id_colaborador, mes_referencia, tipo_folha)
);

-- Estado da conversa de cada número (máquina de estados do bot)
CREATE TABLE IF NOT EXISTS chatbot_sessoes (
    telefone          TEXT PRIMARY KEY,
    estado            TEXT NOT NULL DEFAULT 'INICIO',
    id_colaborador    INTEGER REFERENCES colaboradores(id),
    tentativas_cpf    INTEGER NOT NULL DEFAULT 0,
    bloqueado_ate     TIMESTAMPTZ,
    atendimento_ate   TIMESTAMPTZ,
    ultimo_wamid      TEXT,                          -- evita responder em dobro a reenvios da Meta
    ultima_interacao  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Trilha de auditoria (LGPD): quem pediu o quê e quando
CREATE TABLE IF NOT EXISTS auditoria_chatbot (
    id              BIGSERIAL PRIMARY KEY,
    telefone        TEXT NOT NULL,
    id_colaborador  INTEGER REFERENCES colaboradores(id),
    evento          TEXT NOT NULL,   -- CPF_CONFIRMADO, CPF_INCORRETO, BLOQUEIO_CPF, PEDIDO_HOLERITE, ENCAMINHADO_RH...
    detalhe         TEXT,
    criado_em       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_auditoria_tel_data ON auditoria_chatbot (telefone, criado_em);
