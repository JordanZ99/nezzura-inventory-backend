-- ==============================================================================
-- 061: Cobros (giro 'servicios'): mensualidades por cliente + trabajos extra.
--
-- Dos tablas, una deuda por fila:
--   cuotas_clientes → la SUSCRIPCIÓN fija ("Contabilidad mensual $2000, día 10").
--     No guarda dinero: al generar un periodo, cada cuota activa produce UN cobro.
--   cobros          → cada deuda concreta (la cuota de noviembre o el anticipo
--     de un desarrollo). El dinero recibido vive en pagos (JSONB, mismo patrón
--     que ordenes.pagos) y el saldo SIEMPRE se deriva: monto - SUM(pagos).
--
-- Reglas que no negociamos (patrón Stripe/Xero):
-- - El estado (pendiente|parcial|pagado) NUNCA se almacena: se calcula del
--   cruce monto↔pagos. Un abono de más no corrompe nada.
-- - El programa (plan de pagos comprometido: [{etiqueta, monto, fecha}]) es un
--   DATO aparte del dinero recibido; las fechas vencidas se derivan de él
--   (vencimiento para cobros simples, programa para planes por partes).
-- - Idempotencia del generador: índice parcial único (tenant, cuota, periodo).
-- ==============================================================================

CREATE TABLE IF NOT EXISTS cuotas_clientes (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    cliente_id  UUID NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    concepto    TEXT NOT NULL,
    monto       NUMERIC(12,2) NOT NULL CHECK (monto > 0),
    -- Día del mes en que vence la cuota. 1..28 para no pelear con fechas
    -- inexistentes (31 de febrero). El generador arma la fecha con este día.
    dia_cobro   INT NOT NULL DEFAULT 1 CHECK (dia_cobro BETWEEN 1 AND 28),
    activo      BOOLEAN NOT NULL DEFAULT true,
    notas       TEXT,
    creado_en   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cobros (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    cliente_id  UUID NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    -- NULL para 'extra' (trabajo puntual); la cuota que lo generó si 'cuota'.
    -- ON DELETE SET NULL: si la cuota se borra, el historial de cobros queda.
    cuota_id    UUID REFERENCES cuotas_clientes(id) ON DELETE SET NULL,
    origen      TEXT NOT NULL DEFAULT 'extra' CHECK (origen IN ('cuota', 'extra')),
    concepto    TEXT NOT NULL,
    -- 'YYYY-MM' SOLO para origen 'cuota' (el periodo que representa). Los
    -- extra no tienen periodo: no se repiten.
    periodo     TEXT CHECK (periodo IS NULL OR periodo ~ '^\d{4}-\d{2}$'),
    monto       NUMERIC(12,2) NOT NULL CHECK (monto > 0),
    -- Fecha comprometida de pago; NULL = "cuando pueda". Para cobros de cuota
    -- la fija el generador (dia_cobro del periodo); los extra la escriben libre.
    vencimiento DATE,
    -- Plan de pagos comprometido (opcional, estilo Stripe): lista de
    -- {etiqueta, monto, fecha 'YYYY-MM-DD'} — ej. Anticipo 50% / Entrega 50%.
    -- Es el ACUERDO, no el dinero: los pagos reales viven en `pagos`.
    programa    JSONB NOT NULL DEFAULT '[]'::jsonb,
    notas       TEXT,
    -- Dinero entrante: [{fecha 'YYYY-MM-DD', monto, metodo}]. Fuente de verdad
    -- del saldo: monto - SUM(pagos->>'monto'). Writen igual que ordenes.pagos.
    pagos       JSONB NOT NULL DEFAULT '[]'::jsonb,
    creado_en   TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE cuotas_clientes ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON cuotas_clientes;
CREATE POLICY tenant_isolation ON cuotas_clientes
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

ALTER TABLE cobros ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON cobros;
CREATE POLICY tenant_isolation ON cobros
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

CREATE INDEX IF NOT EXISTS idx_cuotas_tenant ON cuotas_clientes (tenant_id, activo);
CREATE INDEX IF NOT EXISTS idx_cuotas_cliente ON cuotas_clientes (tenant_id, cliente_id);

CREATE INDEX IF NOT EXISTS idx_cobros_tenant ON cobros (tenant_id, vencimiento);
CREATE INDEX IF NOT EXISTS idx_cobros_tenant_periodo ON cobros (tenant_id, periodo);
CREATE INDEX IF NOT EXISTS idx_cobros_tenant_cliente ON cobros (tenant_id, cliente_id);

-- El generador de periodos pega a este índice: si el cobro del periodo ya
-- existe, re-generar el mismo mes NO duplica nada (idempotente por diseño).
CREATE UNIQUE INDEX IF NOT EXISTS uq_cobros_cuota_periodo
    ON cobros (tenant_id, cuota_id, periodo)
    WHERE cuota_id IS NOT NULL AND periodo IS NOT NULL;
