-- ==============================================================================
-- 040: Ledger de movimientos de inventario (historial de cambios append-only).
--
-- Regla de oro: cada cambio de stock/costo de un lote deja UN renglón nuevo;
-- el renglón nunca se edita ni se borra (trigger append-only). El stock de un
-- lote es verificable contra su historial: entradas − salidas ± ajustes.
--
-- tipo    = naturaleza del movimiento:
--           'entrada' (+stock), 'salida' (−stock), 'ajuste' (corrección manual
--           o delta de una edición; puede ser ±).
-- origen  = por qué ocurrió:
--           'venta'         → cobro en el POS (incluye consumo de compuestos)
--           'restock'       → alta de producto/variación o restock
--           'ajuste_manual' → edición del lote poniendo el stock absoluto
--           'edicion_venta' → editar la cantidad de una venta ya cobrada
--           'anulacion'     → anulación de venta o ticket (devuelve stock)
--           'baja_lote'     → lote dado de baja (stock a 0)
-- cantidad         = unidades con signo (+ entra, − sale).
-- stock_resultante = snapshot del Stock_Lote del lote justo tras el movimiento
--                    (protege la lectura histórica aunque el lote siga moviéndose).
-- referencia_id    = orden_id (UUID) cuando el movimiento nace de un ticket.
-- producto_id NO lleva FK a propósito: el movimiento debe sobrevivir aunque el
-- producto o el lote desaparezcan después.
-- ==============================================================================

CREATE TABLE IF NOT EXISTS inventario_movimientos (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    producto_id      INTEGER,
    producto         TEXT NOT NULL,
    variacion        TEXT,
    id_lote          TEXT,
    tipo             TEXT NOT NULL CHECK (tipo IN ('entrada', 'salida', 'ajuste')),
    origen           TEXT NOT NULL CHECK (origen IN ('venta', 'restock', 'ajuste_manual', 'edicion_venta', 'anulacion', 'baja_lote')),
    cantidad         NUMERIC(14,3) NOT NULL,
    stock_resultante NUMERIC(14,3),
    referencia_id    TEXT,
    concepto         TEXT,
    fecha            TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE inventario_movimientos ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON inventario_movimientos;
CREATE POLICY tenant_isolation ON inventario_movimientos
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

-- Lectura del historial: siempre por tenant, lo más nuevo primero.
CREATE INDEX IF NOT EXISTS idx_movimientos_tenant_fecha
    ON inventario_movimientos (tenant_id, fecha DESC);
-- Filtro por producto (buscador de la tab Historial de cambios).
CREATE INDEX IF NOT EXISTS idx_movimientos_tenant_producto
    ON inventario_movimientos (tenant_id, producto);

-- ── Append-only: el ledger NO admite UPDATE ni DELETE ──
CREATE OR REPLACE FUNCTION prohibir_mutar_movimientos() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'inventario_movimientos es append-only: no se permite %', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_movimientos_append_only ON inventario_movimientos;
CREATE TRIGGER trigger_movimientos_append_only
    BEFORE UPDATE OR DELETE ON inventario_movimientos
    FOR EACH ROW EXECUTE FUNCTION prohibir_mutar_movimientos();
