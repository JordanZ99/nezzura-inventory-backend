-- ==============================================================================
-- 034: Terminales con comisión (Fase B) y turnos con arqueo (Fase C).
--
-- Fase B: cada negocio registra SUS terminales bancarias con su tarifa real
-- (débito y crédito suelen diferir). Las comisiones se calculan al cobrar y se
-- guardan dentro de ordenes.pagos + comision_total. Opcionalmente se generan
-- como gasto automático ("Comisiones bancarias") si el tenant lo activa.
--
-- Fase C: turno de caja. Se abre con fondo de cajón; los cobros realizados con
-- un turno abierto se le asignan (ordenes.turno_id); al cerrar se cuenta el
-- cajón y se compara contra el efectivo esperado (apertura + cobros en
-- efectivo). Un único turno abierto por tenant (índice parcial único).
-- ==============================================================================

-- ── Fase B: terminales ──
CREATE TABLE IF NOT EXISTS terminales (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    nombre              TEXT NOT NULL,
    banco               TEXT,
    comision_debito_pct REAL NOT NULL DEFAULT 0,
    comision_credito_pct REAL NOT NULL DEFAULT 0,
    comision_fija       REAL NOT NULL DEFAULT 0,
    activo              BOOLEAN NOT NULL DEFAULT true
);

ALTER TABLE terminales ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON terminales;
CREATE POLICY tenant_isolation ON terminales
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'terminales_tenant_nombre_key'
    ) THEN
        ALTER TABLE terminales
            ADD CONSTRAINT terminales_tenant_nombre_key UNIQUE (tenant_id, nombre);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_terminales_tenant ON terminales (tenant_id, activo);

-- Gasto automático de comisiones (opcional, por tenant)
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS gasto_comision_automatico BOOLEAN NOT NULL DEFAULT false;

-- ── Fase C: turnos ──
CREATE TABLE IF NOT EXISTS turnos (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    estado            TEXT NOT NULL DEFAULT 'Abierto',
    abierta_en        TIMESTAMPTZ NOT NULL,
    cerrada_en        TIMESTAMPTZ,
    monto_apertura    NUMERIC(14,2) NOT NULL DEFAULT 0,
    efectivo_esperado NUMERIC(14,2),
    efectivo_contado  NUMERIC(14,2),
    diferencia        NUMERIC(14,2),
    notas             TEXT
);

ALTER TABLE turnos ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON turnos;
CREATE POLICY tenant_isolation ON turnos
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

-- Un solo turno abierto por negocio (protege contra carreras de "abrir turno")
CREATE UNIQUE INDEX IF NOT EXISTS uq_turnos_abierto_por_tenant
    ON turnos (tenant_id) WHERE estado = 'Abierto';
CREATE INDEX IF NOT EXISTS idx_turnos_tenant_abierta ON turnos (tenant_id, abierta_en DESC);

-- Los cobros se adscriben al turno abierto al momento del cobro
ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS turno_id UUID REFERENCES turnos(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_ordenes_turno ON ordenes (turno_id);
