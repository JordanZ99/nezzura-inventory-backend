-- =============================================================================
-- Migración 020 — Recetas por variación (Fase 4)
--
-- Una misma variación del compuesto puede gastar materiales DIFERENTES:
--   Hamburguesa Sencilla → 1 pan + 100g carne
--   Hamburguesa Doble    → 2 panes + 200g carne
--
-- Se añade variacion_id a producto_recetas:
--   NULL  = receta BASE (se usa si la variación vendida no tiene receta propia)
--   {id}  = receta específica de esa variación (producto_variaciones.id)
--
-- El UNIQUE(producto_id, material_id) original no sirve con variaciones
-- (dos variaciones podrían usar el mismo material). Se reemplaza por dos
-- índices únicos PARCIALES:
--   - (producto_id, material_id) WHERE variacion_id IS NULL        → la base
--   - (producto_id, variacion_id, material_id) WHERE variacion_id IS NOT NULL
-- =============================================================================

-- 1. Columna variacion_id (NULL = receta base)
ALTER TABLE producto_recetas ADD COLUMN IF NOT EXISTS variacion_id INTEGER
    REFERENCES producto_variaciones(id) ON DELETE CASCADE;

-- 2. Quitar el UNIQUE(producto_id, material_id) que bloqueaba variaciones.
--    El nombre por defecto de PostgreSQL es producto_recetas_producto_id_material_id_key.
ALTER TABLE producto_recetas DROP CONSTRAINT IF EXISTS producto_recetas_producto_id_material_id_key;

-- 3. Índices únicos parciales (la base y cada variación por separado)
DROP INDEX IF EXISTS uq_producto_recetas_base;
CREATE UNIQUE INDEX uq_producto_recetas_base
    ON producto_recetas (producto_id, material_id)
    WHERE variacion_id IS NULL;

DROP INDEX IF EXISTS uq_producto_recetas_variacion;
CREATE UNIQUE INDEX uq_producto_recetas_variacion
    ON producto_recetas (producto_id, variacion_id, material_id)
    WHERE variacion_id IS NOT NULL;
