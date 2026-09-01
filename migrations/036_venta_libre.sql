-- ==============================================================================
-- 036: Venta libre (Fase 1). Un producto genérico por tenant para cobrar
-- cosas que NO están registradas en inventario: el cereal que le pidieron al
-- restaurante, la silla que le vendió el emprendedor de peluches, el platillo
-- que aún no da de alta. Contablemente TODO cae en "Venta libre" (una sola
-- línea en estadísticas/top ventas, sin fragmentar por nombre); el texto libre
-- ("Cereal", "Silla usada") vive en ventas.descripcion y se muestra en el
-- ticket y en el historial.
--
-- El producto genérico es tipo 'servicio' (sin inventario: no toca lotes ni
-- PEPS; su costo lo manda el POS en cada venta, default 0) y NO aparece en
-- inventario, gestor ni catálogo: el backend lo excluye con es_generico y el
-- POS lo representa con su tile fijo "＋ Venta libre".
-- ==============================================================================

ALTER TABLE productos ADD COLUMN IF NOT EXISTS es_generico BOOLEAN NOT NULL DEFAULT FALSE;

-- Texto libre del renglón ("Cereal", "Silla usada..."); NULL en ventas normales.
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS descripcion TEXT;

-- Un solo producto genérico por tenant (idempotente). Se omite el tenant que
-- ya tenga un producto real llamado 'Venta libre' (chocaría con el índice
-- UNIQUE(Producto, tenant_id) de _upsert_producto).
INSERT INTO productos (Producto, Descripcion, Imagen, Estado, tenant_id,
                       tipo_producto, es_generico, visible_en_catalogo)
SELECT 'Venta libre',
       'Artículo vendido sin estar registrado en inventario',
       'No hay foto',
       'Activo',
       t.id,
       'servicio',
       TRUE,
       FALSE
FROM tenants t
WHERE NOT EXISTS (SELECT 1 FROM productos p WHERE p.tenant_id = t.id AND p.es_generico)
  AND NOT EXISTS (SELECT 1 FROM productos p WHERE p.tenant_id = t.id AND p.Producto = 'Venta libre');

-- Garantía estructural: máximo UN producto genérico por tenant.
CREATE UNIQUE INDEX IF NOT EXISTS idx_productos_generico_tenant
    ON productos (tenant_id) WHERE es_generico;
