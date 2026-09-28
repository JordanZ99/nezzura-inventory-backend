-- =============================================================================
-- Migración 019 — Productos compuestos con receta (BOM) (Fase 3)
--
-- Un producto COMPUESTO (ej. hamburguesa, silla, pulsera) no tiene stock
-- propio: al venderse, consume stock de sus MATERIALES según una receta.
--
-- Ej. 1 hamburguesa = 1 pan + 150g carne + 2 rebanadas queso
--   producto_id = hamburguesa, material_id = pan, cantidad = 1
--   producto_id = hamburguesa, material_id = carne, cantidad = 150
--
-- Referencias por ID (no por nombre) → sobreviven renombrados.
-- La cantidad es REAL → permite 0.5 pan, 150 g, 0.25 m, etc.
-- =============================================================================

-- 1. Tabla de recetas (Bill of Materials)
CREATE TABLE IF NOT EXISTS producto_recetas (
    id          BIGSERIAL PRIMARY KEY,
    producto_id INTEGER NOT NULL REFERENCES productos(id) ON DELETE CASCADE,  -- el compuesto
    material_id INTEGER NOT NULL REFERENCES productos(id) ON DELETE CASCADE,   -- el material (producto de stock)
    cantidad    REAL    NOT NULL DEFAULT 1,
    tenant_id   UUID    NOT NULL,
    UNIQUE(producto_id, material_id)
);

CREATE INDEX IF NOT EXISTS idx_producto_recetas_producto
    ON producto_recetas (producto_id, tenant_id);

-- 2. Las ventas de compuestos guardan el CONSUMO real de materiales:
--    [{material, id_lote, cantidad, costo}] → permite anular/editar la venta
--    revirtiendo EXACTAMENTE los lotes que se usaron (corrige el bug de
--    "restaurar al lote equivocado" para compuestos).
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS consumo jsonb DEFAULT NULL;
