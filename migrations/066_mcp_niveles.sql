-- ==============================================================================
-- 066: Niveles de permisos para las claves del agente IA + borradores.
--
-- Tres niveles por clave (el dueño elige al crearla, con advertencia):
--   1 "Lectura básica"    → catálogo para publicar (los 6 tools originales).
--   2 "Lectura avanzada"  → + análisis del negocio con datos reales
--                           (resumen_negocio, hallazgos_inteligentes). Exclusivo
--                           plan Plus, igual que /analitica en el gestor.
--   3 "Escritura básica"  → + crear productos con patrón borrador→confirmar
--                           (proponer_producto, confirmar_producto). Cada
--                           confirmación es una llamada destructive que el
--                           cliente (Muse) debe someter a aprobación humana,
--                           y cada escritura queda en el registro de actividad
--                           con origen 'agente_ia'.
--
-- Escrituras contadas desde el día 1 (escrituras_hoy / dia_escrituras): el
-- límite se puede bajar luego cambiando una constante, sin tocar código.
--
-- mcp_borradores: el transporte MCP es stateless, así que el borrador de un
-- producto vive AQUÍ (con TTL de 15 minutos) y no en memoria. confirmar
-- ejecuta SOLO desde lo guardado en el borrador: el agente no puede alterar
-- los datos entre proponer y confirmar.
-- ==============================================================================

ALTER TABLE mcp_api_keys
    ADD COLUMN IF NOT EXISTS nivel SMALLINT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS escrituras_hoy INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS dia_escrituras DATE;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'mcp_api_keys_nivel_check'
    ) THEN
        ALTER TABLE mcp_api_keys
            ADD CONSTRAINT mcp_api_keys_nivel_check CHECK (nivel IN (1, 2, 3));
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS mcp_borradores (
    id          SERIAL PRIMARY KEY,
    token       TEXT NOT NULL UNIQUE,
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    clave_id    INTEGER,
    datos       JSONB NOT NULL,
    creado_en   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expira_en   TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mcp_borradores_tenant ON mcp_borradores(tenant_id);
CREATE INDEX IF NOT EXISTS idx_mcp_borradores_expira ON mcp_borradores(expira_en);

ALTER TABLE mcp_borradores ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON mcp_borradores;
CREATE POLICY tenant_isolation ON mcp_borradores
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());
