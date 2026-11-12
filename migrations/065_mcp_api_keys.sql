-- ==============================================================================
-- 065: Claves API para agentes IA (servidor MCP de solo lectura del catálogo).
--
-- Cada tenant genera sus claves desde Ajustes → Negocio → "Conectar mi IA".
-- La clave viaja en el header X-API-Key y el middleware del servidor MCP
-- (muse/auth.py) la resuelve a un tenant_id: TODAS las queries llevan
-- WHERE tenant_id = %s, igual que el resto del backend. El agente NUNCA
-- decide el tenant — lo deriva la clave.
--
-- Seguridad:
--   - key_hash: SHA-256 de la clave. La clave en claro SOLO se muestra una
--     vez al crearse (el backend nunca la guarda).
--   - key_prefix: primeros 12 chars para identificarla en la UI sin exponerla.
--   - revocada_en: soft-delete. Revocar en el gestor desconecta el agente
--     al instante aunque este conserve la clave guardada.
--   - llamadas_hoy + dia_uso: rate limit simple (300/día por clave) contado
--     en la misma tabla, sin infraestructura extra.
-- ==============================================================================

CREATE TABLE IF NOT EXISTS mcp_api_keys (
    id            SERIAL PRIMARY KEY,
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    nombre        TEXT NOT NULL DEFAULT 'Mi IA',
    key_hash      TEXT NOT NULL UNIQUE,
    key_prefix    TEXT NOT NULL,
    creada_en     TIMESTAMPTZ NOT NULL DEFAULT now(),
    ultimo_uso_en TIMESTAMPTZ,
    llamadas_hoy  INTEGER NOT NULL DEFAULT 0,
    dia_uso       DATE,
    revocada_en   TIMESTAMPTZ
);

ALTER TABLE mcp_api_keys ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON mcp_api_keys;
CREATE POLICY tenant_isolation ON mcp_api_keys
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

-- Lookup por hash (validación de cada request al /mcp) y listado por tenant.
CREATE INDEX IF NOT EXISTS idx_mcp_api_keys_tenant ON mcp_api_keys(tenant_id);
CREATE INDEX IF NOT EXISTS idx_mcp_api_keys_hash ON mcp_api_keys(key_hash);
