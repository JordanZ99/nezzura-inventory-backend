-- ==============================================================================
-- 025_eliminar_stock_por_variacion.sql
-- Elimina la opción "stock por variación" y el flag productos.stock_por_variacion.
--
-- Regla nueva: un producto con variaciones SIEMPRE maneja stock por variación.
-- La presencia de variaciones es lo que define el comportamiento (restock,
-- ventas, catálogo, POS) — ya no hay flag por producto.
--
-- Migración de datos:
--   Los lotes base (variacion_id NULL) de productos que tenían variaciones con
--   el flag OFF (stock compartido legacy) se reasignan a la PRIMERA variación
--   del producto (MIN(id) = la más antigua). Sus demás variaciones quedan en 0.
-- ==============================================================================

-- 1. Reasignar lotes base → primera variación (solo productos con variaciones y flag OFF)
UPDATE lotes l
SET variacion_id = sub.primera_var
FROM (
    SELECT p.id, p.producto, p.tenant_id, MIN(v.id) AS primera_var
    FROM productos p
    JOIN producto_variaciones v ON v.producto_id = p.id
    WHERE COALESCE(p.stock_por_variacion, false) = false
    GROUP BY p.id, p.producto, p.tenant_id
) sub
WHERE l.producto = sub.producto
  AND l.tenant_id = sub.tenant_id
  AND l.variacion_id IS NULL
  AND l.estado = 'Activo';

-- 2. Eliminar el flag
ALTER TABLE productos DROP COLUMN IF EXISTS stock_por_variacion;
