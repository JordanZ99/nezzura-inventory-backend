-- ==============================================================================
-- 079: Intereses moratorios (giro 'servicios').
--
-- tenants.moratorios (JSONB) = política del tenant:
--   NULL o {"activo": false, ...} → DESACTIVADO (default: nadie carga moras).
--   {"activo": true, "tasa_mensual": 4, "dias_gracia": 0}
--
-- El interés DERIVADO nunca se almacena (misma filosofía que saldo/estado):
--   moratorios = saldo_vencido × (tasa/100) / 30 × (dias_vencido − gracia),
--   tope 0 por abajo, interés SIMPLE (sin capitalización), y descontando lo
--   YA FACTURADO via cobros.interes_cargado.
--
-- interes_cargado es el ÚNICO dato nuevo por cobro y es un HECHO (dinero que
-- ya se le facturó como mora), no un derivado: "Cargar moratorios" lo
-- incrementa al materializar el interés en un cobro extra, evitando doble
-- cobro en la próxima lectura.
-- ==============================================================================

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS moratorios JSONB;

ALTER TABLE cobros ADD COLUMN IF NOT EXISTS interes_cargado NUMERIC(12,2) NOT NULL DEFAULT 0;
