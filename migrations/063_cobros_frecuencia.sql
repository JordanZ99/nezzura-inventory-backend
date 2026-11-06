-- ==============================================================================
-- 063: Frecuencias de cuota (mensual | semanal | quincenal).
--
-- Hasta 061 las cuotas solo eran mensuales (dia_cobro 1..28). Esto añade:
--   semanal   → un cobro por cada [dia_semana] del mes (Lun..Dom).
--   quincenal → dos cobros al mes: día 1 y día 15 (convención quincena).
--
-- La idempotencia del generador cambia de llave: el índice único pasa de
-- (tenant, cuota, periodo) a (tenant, cuota, VENCIMIENTO) — una cuota
-- semanal produce varias cobros en el MISMO periodo, pero cada uno con
-- fecha distinta. El monthly sigue cubierto (un vencimiento por mes).
-- ==============================================================================

-- ── Columnas nuevas en cuotas_clientes ──
ALTER TABLE cuotas_clientes ADD COLUMN IF NOT EXISTS frecuencia TEXT NOT NULL DEFAULT 'mensual';
ALTER TABLE cuotas_clientes ADD COLUMN IF NOT EXISTS dia_semana SMALLINT;  -- 0=Lunes..6=Domingo (solo semanal)

ALTER TABLE cuotas_clientes DROP CONSTRAINT IF EXISTS ck_cuotas_frecuencia;
ALTER TABLE cuotas_clientes ADD CONSTRAINT ck_cuotas_frecuencia
    CHECK (frecuencia IN ('mensual', 'semanal', 'quincenal'));

ALTER TABLE cuotas_clientes DROP CONSTRAINT IF EXISTS ck_cuotas_dia_semana;
ALTER TABLE cuotas_clientes ADD CONSTRAINT ck_cuotas_dia_semana
    CHECK (dia_semana IS NULL OR dia_semana BETWEEN 0 AND 6);

-- Una cuota semanal DEBE declarar su día de la semana.
ALTER TABLE cuotas_clientes DROP CONSTRAINT IF EXISTS ck_cuotas_frecuencia_dia;
ALTER TABLE cuotas_clientes ADD CONSTRAINT ck_cuotas_frecuencia_dia
    CHECK (frecuencia <> 'semanal' OR dia_semana IS NOT NULL);

-- ── Nueva llave de idempotencia del generador ──
DROP INDEX IF EXISTS uq_cobros_cuota_periodo;
CREATE UNIQUE INDEX IF NOT EXISTS uq_cobros_cuota_vencimiento
    ON cobros (tenant_id, cuota_id, vencimiento)
    WHERE cuota_id IS NOT NULL;
