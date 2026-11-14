-- ==============================================================================
-- 071: Cuota de almacenamiento por tenant según su plan (planes_config).
--
-- Diseño (Google Photos style): básico y plus tienen las MISMAS funciones;
-- lo único que cambia es el límite de peso que el tenant sube. El medidor
-- es visible en tiempo real y, al cruzar el límite, las subidas nuevas se
-- BLOQUEAN (el tenant libera espacio borrando imágenes, o mejora a Plus).
--
-- Principios:
--   - El medidor vive SOLO en la BD propia (tenant_storage + storage_eventos).
--     Nunca consulta APIs del proveedor de imágenes para decidir: la futura
--     migración de Cloudinary a otro proveedor no toca este sistema.
--   - Se cuenta lo que el tenant SUBE (bytes del archivo que envía), no lo
--     que el proveedor guarda: número honesto y defendible ante el tenant.
--   - El límite se lee SIEMPRE vía JOIN con planes_config: cambiar de plan
--     (upgrade/downgrade) no requiere tocar el contador.
--   - Solo existen 2 planes ('basico' | 'plus'). El Modo Local es un MODO
--     disponible dentro de ambos, no un plan: la cuota no lo distingue.
--
-- Estructura:
--   planes_config    — configuración de planes (límites editables en BD).
--   tenant_storage   — contador por tenant (lectura rápida para medidor/guardián).
--   storage_eventos  — ledger append-only: cada subida/borrado/ajuste queda
--                      registrado. La reconciliación recomputa el contador
--                      desde aquí (detecta drift de requests que crashearon).
-- ==============================================================================

-- ── Configuración de planes ──────────────────────────────────────────────────
-- limite_bytes: 1 GB = 1073741824 (binario). Cambiar un límite es un UPDATE
-- aquí (o una migración nueva); nunca un deploy.
CREATE TABLE IF NOT EXISTS planes_config (
    plan           TEXT PRIMARY KEY,
    nombre         TEXT NOT NULL,
    precio_mxn     NUMERIC(8,2) NOT NULL DEFAULT 0,
    limite_bytes   BIGINT NOT NULL,
    activo         BOOLEAN NOT NULL DEFAULT TRUE,
    actualizado_en TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO planes_config (plan, nombre, precio_mxn, limite_bytes) VALUES
    ('basico', 'Básico', 99,  1073741824),
    ('plus',   'Plus',   245, 10737418240)
ON CONFLICT (plan) DO NOTHING;

-- Normalización defensiva: el plan 'local' fue absorbido como MODO dentro de
-- básico/plus. Si algún tenant quedó con ese valor (o con cualquier otro no
-- reconocido), se mapea a 'basico' para que el JOIN del medidor siempre
-- encuentre su límite. Idempotente.
UPDATE tenants SET plan = 'basico'
WHERE plan IS NULL OR plan NOT IN ('basico', 'plus');

-- ── Contador por tenant (cache del ledger) ───────────────────────────────────
-- bytes_usados: lo que cuenta la cuota (bytes subidos por el tenant).
-- bytes_reales: lo que pesa de verdad en el proveedor (informativo / costos).
CREATE TABLE IF NOT EXISTS tenant_storage (
    tenant_id      UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    bytes_usados   BIGINT NOT NULL DEFAULT 0,
    bytes_reales   BIGINT NOT NULL DEFAULT 0,
    actualizado_en TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Ledger append-only ───────────────────────────────────────────────────────
-- evento:
--   'upload'         — subida confirmada al proveedor. liberado=FALSE mientras
--                      su peso esté cobrado en el contador.
--   'delete'         — la subida asociada (por url) quedó liberada. bytes es
--                      informativo; el peso real lo libera liberado=TRUE del
--                      evento 'upload' original.
--   'ajuste'         — corrección manual de soporte (delta, puede ser negativo).
--   'reconciliacion' — drift corregido por el job nocturno.
CREATE TABLE IF NOT EXISTS storage_eventos (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    url          TEXT NOT NULL,
    bytes        BIGINT NOT NULL DEFAULT 0,
    bytes_reales BIGINT NOT NULL DEFAULT 0,
    evento       TEXT NOT NULL CHECK (evento IN ('upload','delete','ajuste','reconciliacion')),
    liberado     BOOLEAN NOT NULL DEFAULT FALSE,
    detalle      TEXT,
    creado_en    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_storage_eventos_tenant
    ON storage_eventos (tenant_id, creado_en DESC);
CREATE INDEX IF NOT EXISTS idx_storage_eventos_url
    ON storage_eventos (url);
-- El guardián y la liberación buscan el último upload SIN liberar de una URL:
-- índice parcial que hace esa búsqueda exacta.
CREATE INDEX IF NOT EXISTS idx_storage_eventos_url_activos
    ON storage_eventos (url) WHERE evento = 'upload' AND NOT liberado;

-- ── RLS (mismo patrón tenant_isolation del resto de tablas) ─────────────────
ALTER TABLE tenant_storage ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON tenant_storage;
CREATE POLICY tenant_isolation ON tenant_storage
    FOR ALL USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

ALTER TABLE storage_eventos ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON storage_eventos;
CREATE POLICY tenant_isolation ON storage_eventos
    FOR ALL USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

-- planes_config es información del SaaS, no del negocio: lectura para todos
-- los tenants autenticados (el medidor del frontend la consume vía backend).
ALTER TABLE planes_config ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS planes_config_lectura ON planes_config;
CREATE POLICY planes_config_lectura ON planes_config
    FOR SELECT USING (auth.role() = 'authenticated');
