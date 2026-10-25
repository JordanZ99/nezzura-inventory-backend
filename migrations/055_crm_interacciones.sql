-- ==============================================================================
-- 055: CRM — interacciones de seguimiento por cliente (timeline).
--
-- Segundo pilar del CRM: línea de tiempo de TODO lo que el dueño registra
-- sobre un cliente. Complementa las métricas automáticas (compras de la mig
-- 038 y puntos de la 039) con lo que NO sale de la caja: llamadas, notas,
-- visitas y promos entregadas.
--
-- tipo      = naturaleza del registro (CHECK cerrado para la UI):
--             'nota'    → texto libre del dueño (preventivo/compromiso/gusto)
--             'llamada' → llamada de seguimiento
--             'visita'  → motivo de la visita presencial
--             'promo'   → promo/oferta enviada (cómo reaccionó queda en 'texto')
-- texto     = contenido libre del registro (SIEMPRE conviene llenarlo).
-- creado_por = quién lo escribió como TEXT (modelo single-owner del proyecto:
--             el nombre que el front mande, ej. el apuntador en el terminal).
--             NOT NULL DEFAULT '' para no obligar a pedirlo al registrar.
--
-- DECISIÓN: NO lleva trigger append-only (a diferencia de los ledgers
-- contables inventario_movimientos / puntos_movimientos). Una interacción es
-- contenido del CRM, no evidencia contable: el dueño puede corregir un error
-- de dedo. La integridad no sufre: FECHA y cliente_id quedan intactos.
-- ==============================================================================

CREATE TABLE IF NOT EXISTS crm_interacciones (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    cliente_id UUID NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    tipo       TEXT NOT NULL CHECK (tipo IN ('nota', 'llamada', 'visita', 'promo')),
    texto      TEXT NOT NULL DEFAULT '',
    creado_por TEXT NOT NULL DEFAULT '',
    fecha      TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE crm_interacciones ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON crm_interacciones;
CREATE POLICY tenant_isolation ON crm_interacciones
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

-- Timeline de la ficha: lo más nuevo primero, barato.
CREATE INDEX IF NOT EXISTS idx_crm_interacciones_cliente
    ON crm_interacciones (tenant_id, cliente_id, fecha DESC);
-- Historial unificado si algún día se navega por tipo ('llamadas de la semana').
CREATE INDEX IF NOT EXISTS idx_crm_interacciones_tenant_fecha
    ON crm_interacciones (tenant_id, fecha DESC);
