-- ==============================================================================
-- 042: Orden manual de categorías del catálogo.
--
-- El catálogo público ordenaba las categorías SIEMPRE alfabético
-- (ordenarCategorias en frontend). Esta migración agrega `orden INTEGER`
-- a la tabla `categorias` (por tenant, cada fila ya es del tenant) para
-- permitir arrastrarlas desde Personalización > Catálogo:
--   - NULL = sin orden personalizado → todo sigue alfabético (compatibilidad)
--   - Backfill inicial: posiciones alfabéticas por tenant, de modo que el
--     primer drag & drop parte de lo que el tenant ya veía.
-- Nueva categoría creada después = orden NULL (se muestra al final del
-- grupo ordenado; ver PATCH /inventario/categorias/reordenar).
-- ==============================================================================

ALTER TABLE categorias ADD COLUMN IF NOT EXISTS orden INTEGER;

-- Backfill: numerar alfabéticamente dentro de cada tenant (solo donde sea NULL)
UPDATE categorias c
SET orden = sub.rn
FROM (
    SELECT id, ROW_NUMBER() OVER (PARTITION BY tenant_id ORDER BY nombre ASC) AS rn
    FROM categorias
    WHERE orden IS NULL
) sub
WHERE c.id = sub.id;

-- Índice para elORDER BY orden del listado
CREATE INDEX IF NOT EXISTS idx_categorias_tenant_orden
    ON categorias (tenant_id, orden);
