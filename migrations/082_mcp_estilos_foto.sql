-- ==============================================================================
-- 082_mcp_estilos_foto.sql — Presets de foto por comando para el conector MCP.
--
-- El dueño define COMANDOS de foto (ej. '/img') desde Ajustes → Negocio →
-- Conectar mi IA. Cuando el dueño escribe el comando en el chat (o pide
-- quitar/limpiar el fondo, retocar la foto...), el agente llama la tool
-- estilos_foto, toma el prompt del preset, retoca la imagen que le dieron y
-- la adjunta con adjuntar_imagen — nunca un prompt inventado.
--
-- Diseño:
--   - La fila clave='estudio' es el DEFAULT del sistema (mcp_estilos.py):
--     solo existe en la BD si el dueño la EDITÓ (override). Si no hay fila,
--     el backend sirve el prompt recomendado desde el código.
--   - Filas es_default=FALSE = presets extra que el dueño añadió (comandos
--     propios para otros estilos).
--   - activo=FALSE apaga un preset sin borrarlo (el agente deja de verlo).
--   - UNIQUE (tenant_id, comando): un comando por tenant, sin colisiones.
-- ==============================================================================

CREATE TABLE IF NOT EXISTS mcp_estilos_foto (
    id         SERIAL PRIMARY KEY,
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    clave      TEXT NOT NULL,
    comando    TEXT NOT NULL,
    nombre     TEXT NOT NULL DEFAULT 'Foto',
    prompt     TEXT NOT NULL,
    es_default BOOLEAN NOT NULL DEFAULT FALSE,
    activo     BOOLEAN NOT NULL DEFAULT TRUE,
    creada_en  TIMESTAMPTZ NOT NULL DEFAULT now(),
    editada_en TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, clave),
    UNIQUE (tenant_id, comando)
);

ALTER TABLE mcp_estilos_foto ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON mcp_estilos_foto;
CREATE POLICY tenant_isolation ON mcp_estilos_foto
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

CREATE INDEX IF NOT EXISTS idx_mcp_estilos_tenant ON mcp_estilos_foto(tenant_id);
