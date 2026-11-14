-- ==============================================================================
-- 075: "Mis cuentas" (giro 'servicios'): registro MANUAL de qué plan del gestor
-- tiene cada cliente. Es un cuaderno de control personal — NO un panel admin:
--
--   clientes_planes → "Le di el Plan Plus a Alejandra el 5-nov, de momento
--     NO le estoy cobrando" (estado_cobro='gratis'). Nunca genera cobros ni
--     toca dinero: si decides cobrar, la cuota se crea aparte en cuotas_clientes.
--
-- Migración de datos incluida: los cobros HUÉRFANOS (origen='cuota', cuota
-- eliminada, sin un solo abono) eran deudas fantasma del flujo anterior
-- (cuota creada → generada → cuota borrada = deuda de $99 que ya no se cobra).
-- Se convierten en registros 'gratis' de este cuaderno y se eliminan.
-- Los huérfanos CON abonos se conservan intactos: ese dinero sí existió.
-- ==============================================================================

CREATE TABLE IF NOT EXISTS clientes_planes (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    cliente_id    UUID NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    -- Nombre del plan otorgado, texto libre: 'local', 'plus', 'básico'...
    plan          TEXT NOT NULL,
    -- El estado del COBRO de ese plan (no del plan en sí):
    --   cobrando = le estoy cobrando (la cuota vive en cuotas_clientes)
    --   gratis   = le di el plan y de momento no le cobro nada
    --   pausado  = acuerdo congelado (se retoma después)
    estado_cobro  TEXT NOT NULL DEFAULT 'cobrando'
                  CHECK (estado_cobro IN ('cobrando', 'gratis', 'pausado')),
    -- Informativo: cuánto se acordó cobrar (cuando no es gratis).
    monto_acordado NUMERIC(12,2),
    -- Fecha en que se otorgó el plan.
    otorgado      DATE,
    notas         TEXT,
    creado_en     TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE clientes_planes ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON clientes_planes;
CREATE POLICY tenant_isolation ON clientes_planes
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

CREATE INDEX IF NOT EXISTS idx_planes_tenant ON clientes_planes (tenant_id, estado_cobro);
CREATE INDEX IF NOT EXISTS idx_planes_cliente ON clientes_planes (tenant_id, cliente_id);

-- ── Historial (062): el CHECK de dominios gana 'plan' (mis cuentas) y
-- 'orden' (órdenes de servicio, migración 077) para que sus altas/ediciones
-- también queden en el Historial de Estadísticas con badge propio.
ALTER TABLE registro_actividad DROP CONSTRAINT IF EXISTS registro_actividad_dominio_check;
ALTER TABLE registro_actividad ADD CONSTRAINT registro_actividad_dominio_check
    CHECK (dominio IN ('producto', 'cliente', 'cobro', 'venta', 'gasto', 'plan', 'orden'));

-- ── Migración de datos: deudas fantasma → registros 'gratis' ─────────────────
-- Solo huérfanos SIN abonos (pagos vacío): si hay dinero, la deuda es real y
-- se queda. El monto acordado se conserva como dato informativo.
INSERT INTO clientes_planes (tenant_id, cliente_id, plan, estado_cobro, monto_acordado, otorgado, notas)
SELECT b.tenant_id, b.cliente_id, b.concepto, 'gratis', b.monto, (b.creado_en)::date,
       'Migrado del cobro huérfano del periodo ' || COALESCE(b.periodo, '?')
FROM cobros b
WHERE b.origen = 'cuota'
  AND b.cuota_id IS NULL
  AND (b.pagos IS NULL OR b.pagos = '[]'::jsonb);

DELETE FROM cobros b
WHERE b.origen = 'cuota'
  AND b.cuota_id IS NULL
  AND (b.pagos IS NULL OR b.pagos = '[]'::jsonb);
