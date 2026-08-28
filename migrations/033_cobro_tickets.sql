-- ==============================================================================
-- 033: Cobro de tickets (Fase A): método de pago, propina, mixto y cambio.
--
-- Contabilidad: `total`/`ganancia` siguen siendo SOLO productos; la propina se
-- guarda aparte (es del staff, no es ganancia del negocio). La suma de los
-- pagos en `pagos` debe cuadrar con total + propina (la valida el backend).
-- Comisiones de terminal: columna preparada (comision_total) para la Fase B;
-- en Fase A siempre 0. Órdenes legadas: metodo_pago NULL = "No registrado".
-- ==============================================================================

ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS metodo_pago TEXT;
ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS pagos JSONB;
ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS propina NUMERIC(14,2) NOT NULL DEFAULT 0;
ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS monto_recibido NUMERIC(14,2);
ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS cambio NUMERIC(14,2);
ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS comision_total NUMERIC(14,2) NOT NULL DEFAULT 0;

-- Método de pago con el que el POS preselecciona el cobro de cada negocio
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS metodo_pago_default TEXT NOT NULL DEFAULT 'efectivo';
UPDATE tenants SET metodo_pago_default = 'efectivo'
WHERE metodo_pago_default IS NULL OR metodo_pago_default NOT IN
    ('efectivo', 'tarjeta_debito', 'tarjeta_credito');
