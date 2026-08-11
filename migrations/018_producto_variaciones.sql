-- =============================================================================
-- Migración 018 — Variaciones de producto (Fase 2)
--
-- Una variación es una opción de presentación con su PROPIO precio para un
-- mismo producto (ej. hamburguesa Sencilla $60 / Doble $95; remera S/M/L;
-- corte de cabello Caballero $80 / Dama $120).
--
-- Aplica a CUALQUIER tipo de producto (stock, servicio o compuesto).
-- En esta fase una variación NO consume nada extra: solo cambia el precio.
-- =============================================================================

-- 1. Tabla de variaciones
CREATE TABLE IF NOT EXISTS producto_variaciones (
    id          BIGSERIAL PRIMARY KEY,
    producto_id INTEGER NOT NULL REFERENCES productos(id) ON DELETE CASCADE,
    nombre      TEXT    NOT NULL,
    precio      REAL    NOT NULL DEFAULT 0,
    tenant_id   UUID    NOT NULL,
    UNIQUE(producto_id, nombre)
);

-- Índice para resolver variaciones por producto y tenant
CREATE INDEX IF NOT EXISTS idx_producto_variaciones_producto
    ON producto_variaciones (producto_id, tenant_id);

-- 2. Las ventas guardan QUÉ variación se vendió (texto, para el historial)
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS variacion text DEFAULT '';
