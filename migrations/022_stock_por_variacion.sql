-- =============================================================================
-- Migración 022 — Stock por variación (Fase 6)
--
-- Cada variación puede tener su PROPIO stock: los lotes pueden pertenecer a
-- una variación concreta (lotes.variacion_id) en vez de al producto en general.
--
--   lotes.variacion_id = NULL     → stock del PRODUCTO (base/compartido)
--   lotes.variacion_id = {id}     → stock EXCLUSIVO de esa variación
--
--   productos.stock_por_variacion (boolean, default false):
--     false = comportamiento actual (las variaciones comparten el stock)
--     true  = TODO el stock del producto vive en lotes por variación
--             (ej. llavero Corazones: "Blanco" 3uds, "Rojo" 5uds, "Azul" 0uds)
--
-- La anulación/edición de ventas NO se ve afectada: las ventas apuntan al
-- id_lote exacto, así que revertir stock vuelve al lote correcto de la
-- variación que corresponda.
-- =============================================================================

-- 1. Los lotes pueden apuntar a una variación (NULL = stock del producto/base).
--    ON DELETE CASCADE: al eliminar una variación con "eliminar aún así",
--    sus lotes (y el stock que contienen) se eliminan con ella.
ALTER TABLE lotes ADD COLUMN IF NOT EXISTS variacion_id INTEGER
    REFERENCES producto_variaciones(id) ON DELETE CASCADE;

-- 2. Flag por producto: si está activo, todo el stock se maneja por variación.
ALTER TABLE productos ADD COLUMN IF NOT EXISTS stock_por_variacion boolean DEFAULT false;

-- 3. Índice para resolver stock por (producto, variación) rápido.
CREATE INDEX IF NOT EXISTS idx_lotes_variacion
    ON lotes (Producto, variacion_id, tenant_id);
