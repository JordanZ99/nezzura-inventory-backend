-- =============================================================================
-- 060_venta_libre_categoria.sql
-- Categoría para la venta libre (extensión de la migración 036).
--
--   - ventas.categoria → texto de la categoría elegida en el POS para el
--     renglón del producto genérico 'Venta libre' (NULL en ventas normales).
--     Filosofía 036 intacta: contablemente TODO sigue cayendo en la línea
--     única 'Venta libre'; la categoría no fragmenta estadísticas, solo
--     clasifica el renglón (chips en historial y vista de mesa/cocina).
--
--   - mesa_items.categoria → la orden abierta de una mesa conserva la
--     categoría de su venta libre hasta el cobro (mismo patrón que
--     descripcion/costo).
-- =============================================================================

ALTER TABLE ventas
    ADD COLUMN IF NOT EXISTS categoria TEXT;

ALTER TABLE mesa_items
    ADD COLUMN IF NOT EXISTS categoria TEXT;
