-- ==============================================================================
-- 064: Cuentas finitas — meta total opcional en las cuotas.
--
-- "Paga $500 cada semana hasta llegar a $7,000": monto_meta = 7000.
-- El generador deja de emitir cuando lo programado alcanza la meta y el
-- ÚLTIMO abono se recorta al restante ($7,000 / $500 = 14 exactos; si la
-- cuenta no divide, el último sale con residuo).
--
-- Sin meta (NULL) la cuota es infinita: comportamiento de siempre.
-- La lógica del generador vive en database/cobros.py (_fechas + meta).
-- No hay columna de 'progreso': el programado es SUM(cobros.cuota_id) y el
-- abonado es SUM(pagos JSONB) — ambos DERIVADOS en cada lectura.
-- ==============================================================================

ALTER TABLE cuotas_clientes ADD COLUMN IF NOT EXISTS monto_meta NUMERIC(12,2);

ALTER TABLE cuotas_clientes DROP CONSTRAINT IF EXISTS ck_cuotas_meta_positiva;
ALTER TABLE cuotas_clientes ADD CONSTRAINT ck_cuotas_meta_positiva
    CHECK (monto_meta IS NULL OR monto_meta > 0);

-- El generador pregunta el restante por cuota: este índice vuelve barato
-- el GROUP BY cuota_id SIN tocar tenants ajenos.
CREATE INDEX IF NOT EXISTS idx_cobros_tenant_cuota
    ON cobros (tenant_id, cuota_id)
    WHERE cuota_id IS NOT NULL;
