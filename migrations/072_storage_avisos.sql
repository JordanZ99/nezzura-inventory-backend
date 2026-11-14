-- ==============================================================================
-- 072: Avisos de almacenamiento (migración 071, Fase C).
--
-- Dedupe de notificaciones al cruzar umbrales de uso: un aviso por (tenant,
-- umbral) hasta que el uso vuelva a bajar de ese umbral — entonces la fila se
-- borra y un nuevo cruce vuelve a avisar. Lo maneja el cron nocturno
-- (scripts/reconciliar_storage.py → almacenamiento.procesar_avisos), nunca el
-- path de subida (una subida no debe pagar la latencia de un email).
--
-- Umbrales: 80 (espacio por llenarse), 95 (casi lleno), 100 (excedido).
-- ==============================================================================

CREATE TABLE IF NOT EXISTS storage_avisos (
    tenant_id  UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    umbral     INTEGER     NOT NULL CHECK (umbral IN (80, 95, 100)),
    enviado_en TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, umbral)
);

-- RLS con el patrón del resto de tablas por tenant
ALTER TABLE storage_avisos ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON storage_avisos;
CREATE POLICY tenant_isolation ON storage_avisos
    FOR ALL USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());
