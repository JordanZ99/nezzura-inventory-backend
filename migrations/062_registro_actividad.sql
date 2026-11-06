-- ==============================================================================
-- 062: Ledger de actividad general (historial de cambios NO de stock).
--
-- 040 ya cubre cada cambio de STOCK (inventario_movimientos, fiscal y
-- append-only). Esta tabla cubre TODO lo demás: ediciones de producto (foto,
-- precio, nombre, visibilidad...), CRM (clientes, etiquetas, interacciones,
-- puntos) y cobros del giro 'servicios' (altas, abonos, cuotas, generaciones).
--
-- Es un ledger de OBSERVACIÓN: no alimenta ningún saldo ni ecuación de
-- negocio, así es el ÚNICO ledger podable a futuro (retención 18-24 meses,
-- decisión postergada — hoy no hay límite).
--
-- dominio señala la sección de la app (badges del front):
--   'producto' | 'cliente' | 'cobro'
-- accion: 'crear' | 'editar' | 'eliminar' | 'abonar' | 'quitar_abono'
--         | 'generar' | 'ajustar_puntos'
-- detalle JSONB: SOLO lo que cambió — {campo: [antes, despues], ...} + llaves
-- extra con contexto (p.ej. {"puntos": -50, "concepto": "Promo"}).
-- ==============================================================================

CREATE TABLE IF NOT EXISTS registro_actividad (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    fecha      TIMESTAMPTZ NOT NULL DEFAULT now(),
    dominio    TEXT NOT NULL CHECK (dominio IN ('producto', 'cliente', 'cobro')),
    entidad    TEXT NOT NULL,
    accion     TEXT NOT NULL CHECK (accion IN (
                   'crear', 'editar', 'eliminar', 'abonar',
                   'quitar_abono', 'generar', 'ajustar_puntos')),
    entidad_id TEXT,
    resumen    TEXT NOT NULL,
    detalle    JSONB
);

ALTER TABLE registro_actividad ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON registro_actividad;
CREATE POLICY tenant_isolation ON registro_actividad
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

-- Feed del historial: más nuevo primero + filtro por dominio.
CREATE INDEX IF NOT EXISTS idx_actividad_tenant_fecha
    ON registro_actividad (tenant_id, fecha DESC);
CREATE INDEX IF NOT EXISTS idx_actividad_tenant_dominio
    ON registro_actividad (tenant_id, dominio, fecha DESC);

-- ── Append-only: la actividad NO se edita ni se borra desde la app ──
CREATE OR REPLACE FUNCTION prohibir_mutar_actividad() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'registro_actividad es append-only: no se permite %', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_actividad_append_only ON registro_actividad;
CREATE TRIGGER trigger_actividad_append_only
    BEFORE UPDATE OR DELETE ON registro_actividad
    FOR EACH ROW EXECUTE FUNCTION prohibir_mutar_actividad();
