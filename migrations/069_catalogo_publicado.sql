-- ==============================================================================
-- 069: Snapshot del catálogo publicado desde el Modo Local.
--
-- Los tenants del plan local operan SIN nube (SQLite en el dispositivo), pero
-- su gancho de suscripción es "Publicar catálogo online": desde la app
-- publican un DELTA de filas público a esta tabla y el catálogo de siempre
-- (GET /p/{slug}, servido por Vercel dibujando desde datos) los muestra.
--
-- Formato por FILAS (no un blob):
--   clave = '__config__'                    → config pública (titulo, logo,
--                                             contacto, orden_categorias…)
--   clave = 'producto||variacion'           → un producto público (variación
--                                             '' = producto base)
--   data  = JSONB con SOLO campos públicos
--           (NUNCA costos: costo_promedio/costo_unitario/costo_servicio)
--   hash  = hash determinista del contenido que calcula el cliente; sirve
--           para que el publish sea DELTA (solo sube filas que cambiaron).
--   orden = orden del producto en el catálogo (0 para __config__).
--
-- Cada fila es exactamente la materia prima que las vitrinas del Bazar
-- futuro necesitarán consultar: por eso el snapshot es por filas y no un
-- HTML estático ni un JSON gigante.
--
-- Seguridad:
--   - tenant_id viaja SOLO en el JWT de licencia (nunca en el body).
--   - DELETE CASCADE si el tenant desaparece.
--   - RLS tenant_isolation igual que el resto de tablas por tenant.
-- ==============================================================================

CREATE TABLE IF NOT EXISTS catalogo_publicado (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    clave        TEXT NOT NULL,
    hash         TEXT NOT NULL,
    data         JSONB NOT NULL,
    orden        INTEGER NOT NULL DEFAULT 0,
    publicado_en TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Una sola fila publicada por clave (el delta reemplaza in-place).
CREATE UNIQUE INDEX IF NOT EXISTS uq_catalogo_publicado_tenant_clave
    ON catalogo_publicado(tenant_id, clave);

ALTER TABLE catalogo_publicado ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON catalogo_publicado;
CREATE POLICY tenant_isolation ON catalogo_publicado
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

CREATE INDEX IF NOT EXISTS idx_catalogo_publicado_tenant ON catalogo_publicado(tenant_id);
