-- ==============================================================================
-- 031: Fechas con zona horaria por negocio (Fase 1: columnas + backfill + base)
--
-- Tres conceptos separados:
--   Instante        → ventas.fecha_ts / lotes.fecha_entrada_ts  (TIMESTAMPTZ)
--   Fecha negocio   → derivada: (fecha_ts AT TIME ZONE tz)::date (se calcula al leer)
--   Fecha capturada → gastos.fecha_negocio (DATE puro)
--
-- Los TEXT legados (ventas.fecha, lotes.fecha_entrada, gastos.fecha) se siguen
-- escribiendo (dual-write) y quedan como copia de display hasta la Fase 3.
--
-- Reglas de backfill identificando el formato de origen:
--   naive sin zona  → era hora del servidor (UTC): se importa como UTC
--   con offset/Z    → se parsea como instante (correcto tal cual)
--   date-only       → DATE tal cual (fecha de negocio)
-- ==============================================================================

-- 1. Zona horaria del negocio (nombre IANA; 'America/Cancun' como default actual)
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS zona_horaria TEXT;
UPDATE tenants SET zona_horaria = 'America/Cancun'
WHERE zona_horaria IS NULL OR zona_horaria = '';
ALTER TABLE tenants ALTER COLUMN zona_horaria SET NOT NULL;
ALTER TABLE tenants ALTER COLUMN zona_horaria SET DEFAULT 'America/Cancun';

-- 2. Columnas de instante y fecha de negocio
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS fecha_ts TIMESTAMPTZ;
ALTER TABLE lotes ADD COLUMN IF NOT EXISTS fecha_entrada_ts TIMESTAMPTZ;
ALTER TABLE gastos ADD COLUMN IF NOT EXISTS fecha_negocio DATE;

-- 3. Backfill de instantes (ventas)
UPDATE ventas SET fecha_ts = CASE
    WHEN fecha ~ '(Z|[+-][0-9]{2}:?[0-9]{2})$' THEN fecha::timestamptz
    WHEN LENGTH(fecha) > 10 THEN (fecha::timestamp) AT TIME ZONE 'UTC'
    ELSE NULL
END
WHERE fecha_ts IS NULL;

-- 4. Backfill de instantes (lotes)
UPDATE lotes SET fecha_entrada_ts = CASE
    WHEN fecha_entrada ~ '(Z|[+-][0-9]{2}:?[0-9]{2})$' THEN fecha_entrada::timestamptz
    WHEN LENGTH(fecha_entrada) > 10 THEN (fecha_entrada::timestamp) AT TIME ZONE 'UTC'
    ELSE NULL
END
WHERE fecha_entrada_ts IS NULL;

-- 5. Backfill de fecha de negocio (gastos): los instantes se convierten al
--    día de la zona del tenant; los date-only quedan tal cual.
UPDATE gastos g
SET fecha_negocio = CASE
    WHEN g.fecha ~ '(Z|[+-][0-9]{2}:?[0-9]{2})$'
        THEN (g.fecha::timestamptz AT TIME ZONE COALESCE(t.zona_horaria, 'America/Cancun'))::date
    ELSE LEFT(g.fecha, 10)::date
END
FROM tenants t
WHERE g.tenant_id = t.id AND g.fecha_negocio IS NULL;

-- 6. Índices sobre las columnas canónicas
CREATE INDEX IF NOT EXISTS idx_ventas_tenant_fecha_ts
    ON ventas (tenant_id, fecha_ts DESC);
CREATE INDEX IF NOT EXISTS idx_lotes_tenant_fecha_ts
    ON lotes (tenant_id, fecha_entrada_ts);
CREATE INDEX IF NOT EXISTS idx_gastos_tenant_fecha_negocio
    ON gastos (tenant_id, fecha_negocio);

-- 7. Garantías: el backfill debe estar completo antes de NOT NULL
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM ventas WHERE fecha_ts IS NULL) THEN
        RAISE EXCEPTION 'Backfill incompleto: ventas con fecha_ts NULL';
    END IF;
    IF EXISTS (SELECT 1 FROM lotes WHERE fecha_entrada_ts IS NULL) THEN
        RAISE EXCEPTION 'Backfill incompleto: lotes con fecha_entrada_ts NULL';
    END IF;
    IF EXISTS (SELECT 1 FROM gastos WHERE fecha_negocio IS NULL) THEN
        RAISE EXCEPTION 'Backfill incompleto: gastos con fecha_negocio NULL';
    END IF;
END $$;

ALTER TABLE ventas ALTER COLUMN fecha_ts SET NOT NULL;
ALTER TABLE lotes ALTER COLUMN fecha_entrada_ts SET NOT NULL;
ALTER TABLE gastos ALTER COLUMN fecha_negocio SET NOT NULL;
