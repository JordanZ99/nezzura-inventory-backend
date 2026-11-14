-- ==============================================================================
-- 070: Sincronización del Modo Offline (plan básico/plus con modo híbrido).
--
-- El dispositivo opera con repos locales (SQLite/Dexie) y al reconectar
-- sincroniza contra la nube:
--   * SUBIR   → POST /sync/subir (append-only + replay PEPS, idempotente)
--   * BAJAR   → GET  /sync/bajar?desde=<cursor> (filas con updated_at > cursor)
--
-- Esta migración es 100% ADITIVA (columnas nuevas + índices + tabla de log):
--
--   client_uid (ventas, gastos): id generado por el dispositivo; el delta lo
--     usa para idempotencia (reenviar una orden 2 veces = no-op). La tabla
--     `ordenes` NO la necesita (su id ya es UUID del cliente) y `lotes` ya
--     tiene uq_lotes_tenant_id_lote (el restock offline mapea por id_lote).
--
--   updated_at (productos, categorias, lotes, producto_variaciones): cursor
--     de la bajada. DEFAULT now() rellena las filas existentes con el momento
--     de la migración, así un dispositivo que arranca con cursor epoch-0
--     descarga TODO el negocio la primera vez. Trigger BEFORE UPDATE para
--     que cada cambio en la nube avance el cursor.
--
--   sync_log: auditoría de la resolución de conflictos LWW (quién ganó, qué
--     versión se descartó). "La app se comió mi cambio" se vuelve evento
--     consultable, no silencio.
--
-- NUNCA se sincroniza el stock como valor: los lotes se decuentan por
-- eventos (descontar_stock_peps con el id_lote que PEPS local ya asignó).
-- ==============================================================================

-- ── Idempotencia de la subida ──
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS client_uid TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_ventas_tenant_client_uid
    ON ventas(tenant_id, client_uid) WHERE client_uid IS NOT NULL;

ALTER TABLE gastos ADD COLUMN IF NOT EXISTS client_uid TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_gastos_tenant_client_uid
    ON gastos(tenant_id, client_uid) WHERE client_uid IS NOT NULL;

-- ── Cursor de la bajada ──
ALTER TABLE productos ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE categorias ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE lotes ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE producto_variaciones ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE OR REPLACE FUNCTION tocar_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_productos_updated_at ON productos;
CREATE TRIGGER trg_productos_updated_at
    BEFORE UPDATE ON productos FOR EACH ROW EXECUTE FUNCTION tocar_updated_at();

DROP TRIGGER IF EXISTS trg_categorias_updated_at ON categorias;
CREATE TRIGGER trg_categorias_updated_at
    BEFORE UPDATE ON categorias FOR EACH ROW EXECUTE FUNCTION tocar_updated_at();

DROP TRIGGER IF EXISTS trg_lotes_updated_at ON lotes;
CREATE TRIGGER trg_lotes_updated_at
    BEFORE UPDATE ON lotes FOR EACH ROW EXECUTE FUNCTION tocar_updated_at();

DROP TRIGGER IF EXISTS trg_variaciones_updated_at ON producto_variaciones;
CREATE TRIGGER trg_variaciones_updated_at
    BEFORE UPDATE ON producto_variaciones FOR EACH ROW EXECUTE FUNCTION tocar_updated_at();

-- Las migraciones 066_variacion_tamano_cm / 049 usan este patrón también para
-- imágenes: los triggers de UPDATE marcan el cursor al cambiar cualquier fila.

-- ── Auditoría de conflictos (LWW + alertas) ──
CREATE TABLE IF NOT EXISTS sync_log (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    tabla       TEXT NOT NULL,
    clave       TEXT NOT NULL,
    campo       TEXT,
    valor_previo JSONB,
    valor_nuevo JSONB,
    resolucion  TEXT NOT NULL,   -- 'lww_dispositivo_gana' | 'lww_nube_gana' | 'stock_negativo' | ...
    creado_en   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sync_log_tenant ON sync_log(tenant_id, creado_en DESC);

ALTER TABLE sync_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON sync_log;
CREATE POLICY tenant_isolation ON sync_log
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());
