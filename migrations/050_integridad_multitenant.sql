-- ==============================================================================
-- 050: Integridad multitenant (producto_imagenes, lotes, producto_variaciones,
--      producto_recetas).
--
-- 1. producto_imagenes.tenant_id era nullable y sin FK a tenants. Una fila con
--    tenant_id NULL queda INVISIBLE bajo RLS (NULL = auth.uid() → siempre
--    false) y huérfana si su producto desapareciera. Ahora: backfill desde
--    productos (el producto ya tiene FK CASCADE, así que su tenant es la fuente
--    de verdad), NOT NULL y FK a tenants ON DELETE CASCADE.
-- 2. lotes.ID_Lote tenía UNIQUE GLOBAL: dos tenants que usaran la misma
--    etiqueta de lote chocarían entre sí. Todas las consultas del backend ya
--    filtran por tenant_id (y los id_lote se generan con uuid4), así que el
--    correcto es UNIQUE(tenant_id, ID_Lote).
-- 3. producto_variaciones.tenant_id y producto_recetas.tenant_id no tenían FK
--    a tenants: al borrar un tenant sus filas sobrevivían huérfanas.
-- Idempotente: backfill con WHERE tenant_id IS NULL, constraints protegidos
-- con pg_constraint y CREATE UNIQUE INDEX IF NOT EXISTS.
-- ==============================================================================

-- ── 1. producto_imagenes: tenant derivado del producto ──────────────────────
UPDATE producto_imagenes pi
SET tenant_id = p.tenant_id
FROM productos p
WHERE pi.producto_id = p.id
  AND pi.tenant_id IS NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM producto_imagenes WHERE tenant_id IS NULL) THEN
        RAISE EXCEPTION 'producto_imagenes: filas con tenant_id NULL cuyo producto ya no existe (revisar antes de NOT NULL)';
    END IF;
    IF EXISTS (
        SELECT 1 FROM producto_imagenes pi
        LEFT JOIN tenants t ON t.id = pi.tenant_id
        WHERE t.id IS NULL
    ) THEN
        RAISE EXCEPTION 'producto_imagenes: filas con tenant_id que no existe en tenants';
    END IF;
    IF EXISTS (
        SELECT 1 FROM producto_imagenes pi
        JOIN productos p ON p.id = pi.producto_id
        WHERE pi.tenant_id IS DISTINCT FROM p.tenant_id
    ) THEN
        RAISE EXCEPTION 'producto_imagenes: filas cuyo tenant no coincide con el tenant de su producto';
    END IF;
END $$;

ALTER TABLE producto_imagenes ALTER COLUMN tenant_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'producto_imagenes_tenant_id_fkey'
    ) THEN
        ALTER TABLE producto_imagenes
            ADD CONSTRAINT producto_imagenes_tenant_id_fkey
            FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE;
    END IF;
END $$;

-- ── 2. lotes: la unicidad de ID_Lote pasa a ser POR TENANT ──────────────────
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM lotes
        GROUP BY tenant_id, ID_Lote
        HAVING COUNT(*) > 1
        LIMIT 1
    ) THEN
        RAISE EXCEPTION 'lotes: existen ID_Lote duplicados dentro del mismo tenant; resolver a mano antes de crear el índice único';
    END IF;
END $$;

-- El constraint global de la tabla base se llama lotes_id_lote_key (identificadores
-- sin comillas se guardan en minúscula). Se elimina si existe.
ALTER TABLE lotes DROP CONSTRAINT IF EXISTS lotes_id_lote_key;

CREATE UNIQUE INDEX IF NOT EXISTS uq_lotes_tenant_id_lote
    ON lotes (tenant_id, ID_Lote);

-- ── 3. FK a tenants en producto_variaciones / producto_recetas ──────────────
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM producto_variaciones v
        LEFT JOIN tenants t ON t.id = v.tenant_id
        WHERE t.id IS NULL
    ) THEN
        RAISE EXCEPTION 'producto_variaciones: filas con tenant_id que no existe en tenants';
    END IF;
    IF EXISTS (
        SELECT 1 FROM producto_recetas r
        LEFT JOIN tenants t ON t.id = r.tenant_id
        WHERE t.id IS NULL
    ) THEN
        RAISE EXCEPTION 'producto_recetas: filas con tenant_id que no existe en tenants';
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'producto_variaciones_tenant_id_fkey'
    ) THEN
        ALTER TABLE producto_variaciones
            ADD CONSTRAINT producto_variaciones_tenant_id_fkey
            FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'producto_recetas_tenant_id_fkey'
    ) THEN
        ALTER TABLE producto_recetas
            ADD CONSTRAINT producto_recetas_tenant_id_fkey
            FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE;
    END IF;
END $$;
