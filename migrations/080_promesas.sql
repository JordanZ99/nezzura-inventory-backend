-- ==============================================================================
-- 080: Promesas de pago y recordatorios (giro 'servicios').
--
-- promesa_fecha: "me dijo que paga el viernes" — el hecho queda registrado;
-- mientras la promesa esté vigente (hoy <= fecha), los recordatorios
-- automáticos se pausan; si la fecha pasa y sigue pendiente, la UI la marca
-- como incumplida y vuelve la agenda.
--
-- ultimo_recordatorio: dedupe de emails (job nocturno, scripts/
-- enviar_recordatorios.py): cadencia vence mañana / vence hoy / vencido
-- 1, 7, 15 y 30 días — a lo más un email por cobro y por día.
-- ==============================================================================

ALTER TABLE cobros ADD COLUMN IF NOT EXISTS promesa_fecha DATE;
ALTER TABLE cobros ADD COLUMN IF NOT EXISTS ultimo_recordatorio DATE;

CREATE INDEX IF NOT EXISTS idx_cobros_promesa
    ON cobros (tenant_id, promesa_fecha) WHERE promesa_fecha IS NOT NULL;
