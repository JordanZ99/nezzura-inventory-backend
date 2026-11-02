-- ==============================================================================
-- 058: Licencias de dispositivo (Modo Local, plan "local").
--
-- El Modo Local permite operar la app SIN internet: el teléfono del usuario
-- guarda TODO el negocio en su propio navegador (IndexedDB) y nunca consulta
-- el backend en el día a día. La única conexión necesaria es el login inicial
-- y la revalidación periódica de la licencia.
--
-- Flow:
--   1. Login con Supabase + plan del tenant = 'local'.
--   2. POST /licencias/activar → el backend registra el dispositivo en esta
--      tabla y firma un JWT de licencia ({sub: device_id, tenant_id, plan})
--      HS256 con LICENSE_SECRET, exp + N días (default 14).
--   3. El cliente valida la licencia offline: decodifica el payload y revisa
--      exp. El enforcement REAL es server-side: revalidar revoca/bloquea si
--      el plan ya no es 'local' o el dispositivo fue dado de baja.
--
-- Control para el dueño del negocio (y para ti como SaaS):
--   - activo  → revocar = la licencia muere en su próximo intento de revalidar
--     (máximo al vencimiento de la exp actual: un dispositivo ya activado
--     sigue funcionando hasta que su token actual caduca).
--   - CONFIDENCIALIDAD: esta tabla NO guarda datos del negocio — solo qué
--     aparato tiene licencia. Los datos del modo local jamás tocan este
--     backend (viven en IndexedDB del dispositivo; ver Fase 2: backup cifrado
--     E2E que sube blobs ilegibles).
-- ==============================================================================

-- ── Deprecación: planes viejos no cambian; este valor es ADITIVO ──
-- El plan se sigue leyendo de tenants.plan ('basico' | 'plus' | ahora 'local').

CREATE TABLE IF NOT EXISTS dispositivos_licencia (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID   NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    device_id  TEXT   NOT NULL,                     -- UUID v4 generado por el cliente
    nombre     TEXT   NOT NULL DEFAULT 'Dispositivo',
    activo     BOOLEAN NOT NULL DEFAULT TRUE,
    creado_en  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ultimo_uso TIMESTAMPTZ
);

ALTER TABLE dispositivos_licencia ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON dispositivos_licencia;
CREATE POLICY tenant_isolation ON dispositivos_licencia
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

-- Un dispositivo activo (o revocado, para auditar) por tenant; re-activar
-- el MISMO device_id actualiza la fila existente (upsert por llave única).
CREATE UNIQUE INDEX IF NOT EXISTS uq_dispositivos_tenant_device
    ON dispositivos_licencia (tenant_id, device_id);
