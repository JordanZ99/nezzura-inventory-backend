-- ==============================================================================
-- 032: Órdenes de venta (Tickets)
--
-- La "venta" como transacción deja de ser implícita: cada cobro del carrito
-- crea UNA orden (cabecera) que agrupa sus renglones en ventas.orden_id.
--
--   - ordenes.n_ticket: folio POR ORDEN (antes el trigger daba folio por
--     renglón). Folio concurrente-seguro con advisory lock por tenant.
--   - Ventas legadas: cada fila se convierte en una orden de 1 renglón que
--     hereda su folio/fecha/totales (no se reconstruyen grupos del pasado).
--   - El trigger drift trigger_folio_ventas (solo existía en esta BD, no en
--     migraciones) se elimina; el folio nuevo vive en ordenes.
-- ==============================================================================

-- 1. Cabecera de orden
CREATE TABLE IF NOT EXISTS ordenes (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    n_ticket       INTEGER NOT NULL,
    fecha_ts       TIMESTAMPTZ NOT NULL,
    total          NUMERIC(14,2) NOT NULL DEFAULT 0,
    ganancia       NUMERIC(14,2) NOT NULL DEFAULT 0,
    cantidad_items REAL NOT NULL DEFAULT 0,
    estado         TEXT NOT NULL DEFAULT 'Activa'
);

-- 2. RLS igual que el resto de tablas de negocio
ALTER TABLE ordenes ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON ordenes;
CREATE POLICY tenant_isolation ON ordenes
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

-- 3. Renglones apuntando a su orden
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS orden_id UUID;

-- 4. Backfill: una orden por venta legada (hereda folio, instante y totales).
--    El estado de la orden refleja el de su venta.
DO $$
DECLARE
    v RECORD;
    new_id UUID;
BEGIN
    IF EXISTS (SELECT 1 FROM ventas WHERE orden_id IS NULL AND n_ticket IS NULL) THEN
        RAISE EXCEPTION 'Backfill imposible: hay ventas sin n_ticket';
    END IF;
    FOR v IN
        SELECT id, tenant_id, n_ticket, fecha_ts,
               COALESCE(total_venta, 0)  AS total,
               COALESCE(ganancia_bruta, 0) AS ganancia,
               COALESCE(cantidad, 0)     AS cantidad,
               estado
        FROM ventas WHERE orden_id IS NULL
    LOOP
        new_id := gen_random_uuid();
        INSERT INTO ordenes (id, tenant_id, n_ticket, fecha_ts, total, ganancia, cantidad_items, estado)
        VALUES (new_id, v.tenant_id, v.n_ticket, v.fecha_ts, v.total, v.ganancia, v.cantidad,
                CASE WHEN v.estado = 'Inactivo' THEN 'Anulada' ELSE 'Activa' END);
        UPDATE ventas SET orden_id = new_id WHERE id = v.id;
    END LOOP;
END $$;

-- 5. Índices y unicidad del folio por tenant
CREATE INDEX IF NOT EXISTS idx_ordenes_tenant_fecha_ts ON ordenes (tenant_id, fecha_ts DESC);
CREATE INDEX IF NOT EXISTS idx_ventas_orden ON ventas (orden_id);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ordenes_tenant_ticket_key'
    ) THEN
        ALTER TABLE ordenes
            ADD CONSTRAINT ordenes_tenant_ticket_key UNIQUE (tenant_id, n_ticket);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ventas_orden_id_fkey'
    ) THEN
        ALTER TABLE ventas
            ADD CONSTRAINT ventas_orden_id_fkey
            FOREIGN KEY (orden_id) REFERENCES ordenes(id) ON DELETE SET NULL;
    END IF;
END $$;

ALTER TABLE ventas ALTER COLUMN orden_id SET NOT NULL;

-- 6. Folio por orden. Se elimina el trigger drift de ventas (folio por renglón)
--    y se crea el canónico sobre ordenes con advisory lock por tenant, que
--    serializa cobros simultáneos del MISMO tenant sin bloquear a otros.
DROP TRIGGER IF EXISTS trigger_folio_ventas ON ventas;

CREATE OR REPLACE FUNCTION generar_numero_ticket_orden() RETURNS trigger AS $$
BEGIN
    IF NEW.n_ticket IS NOT NULL THEN
        RETURN NEW;
    END IF;
    PERFORM pg_advisory_xact_lock(770001, hashtext(NEW.tenant_id::text));
    SELECT COALESCE(MAX(n_ticket), 0) + 1
    INTO NEW.n_ticket
    FROM ordenes
    WHERE tenant_id = NEW.tenant_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_folio_ordenes ON ordenes;
CREATE TRIGGER trigger_folio_ordenes
    BEFORE INSERT ON ordenes
    FOR EACH ROW EXECUTE FUNCTION generar_numero_ticket_orden();
