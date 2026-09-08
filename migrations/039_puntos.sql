-- ==============================================================================
-- 039: Sistema de puntos (Fase A: libro de movimientos + config por tenant).
--
-- Regla de oro (doc sistemaPuntos.md): el saldo del cliente SIEMPRE se calcula
-- a partir del ledger puntos_movimientos, nunca de una columna "saldo" editable.
-- - tipo 'ganados'   → +puntos al cobrar una venta con cliente
-- - tipo 'canjeados' → −puntos al pagar con puntos
-- - tipo 'ajuste'    → ±puntos manual del tenant (promos, correcciones)
-- Anular una orden revierte sus movimientos (Fase B los crea con orden_id).
--
-- valor_monetario es el snapshot de cuánto dinero valían los puntos al momento
-- del movimiento: protege la analítica si el tenant luego cambia el valor del
-- punto (ej. infla de 1 pt = $1 a 100 pts = $1).
-- ==============================================================================

CREATE TABLE IF NOT EXISTS puntos_movimientos (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    cliente_id      UUID NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    orden_id        UUID REFERENCES ordenes(id) ON DELETE SET NULL,
    tipo            TEXT NOT NULL CHECK (tipo IN ('ganados', 'canjeados', 'ajuste')),
    puntos          INTEGER NOT NULL,            -- + gana, − gasta/canjea
    valor_monetario NUMERIC(14,2),               -- $ que representaban esos puntos al momento
    concepto        TEXT,
    fecha           TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE puntos_movimientos ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON puntos_movimientos;
CREATE POLICY tenant_isolation ON puntos_movimientos
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

-- El saldo por cliente es SUM(puntos): índice para que sea barato siempre.
CREATE INDEX IF NOT EXISTS idx_puntos_cliente ON puntos_movimientos (tenant_id, cliente_id, fecha);
CREATE INDEX IF NOT EXISTS idx_puntos_orden ON puntos_movimientos (orden_id);

-- ── Config del programa de puntos en tenants ──
-- puntos_valor_punto: cuánto DINERO vale 1 punto al canjear. Default 1.0
--   (1 punto = $1). Para "100 pts = $1" el tenant pone 0.01.
-- puntos_modo 'por_gasto': X puntos por cada $Y de compra (puntos_gasto_pts /
--   puntos_gasto_monto, ej. 1 pt por cada $10). 'fijo': puntos_fijos por venta.
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS puntos_activos BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS puntos_valor_punto NUMERIC(14,4) NOT NULL DEFAULT 1.0;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS puntos_modo TEXT NOT NULL DEFAULT 'por_gasto';
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS puntos_gasto_monto NUMERIC(14,2) NOT NULL DEFAULT 10;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS puntos_gasto_pts INTEGER NOT NULL DEFAULT 1;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS puntos_fijos INTEGER;
