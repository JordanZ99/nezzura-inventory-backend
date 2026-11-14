-- ==============================================================================
-- 076: Ventas a plazos (giro 'servicios'). Una cuota puede ahora ser una VENTA
-- ("le vendí una laptop en $7,000 y me paga $100/semana") en vez de solo una
-- suscripción de servicio:
--
--   tipo = 'cuota' → suscripción de siempre (concepto libre, meta opcional).
--   tipo = 'venta' → artículo vendido: concepto = el artículo ("Laptop HP 15"),
--     monto = abono por periodo, monto_meta = PRECIO TOTAL (obligatorio),
--     costo = lo que te costó a ti (opcional) → ganancia derivada.
--
-- El generador de cobros NO cambia: ya respeta monto_meta (064) y corta el
-- último abono al residuo. Solo añadimos el etiquetado y el costo/ganancia.
-- Las cuotas existentes quedan 'cuota' (compatibilidad total).
-- ==============================================================================

ALTER TABLE cuotas_clientes ADD COLUMN IF NOT EXISTS tipo TEXT NOT NULL DEFAULT 'cuota'
    CHECK (tipo IN ('cuota', 'venta'));
ALTER TABLE cuotas_clientes ADD COLUMN IF NOT EXISTS costo NUMERIC(12,2)
    CHECK (costo IS NULL OR costo >= 0);

-- Para una VENTA, el precio total es la esencia (sin meta no hay venta a plazos
-- que terminar): CHECK de integridad de datos nuevos.
ALTER TABLE cuotas_clientes DROP CONSTRAINT IF EXISTS ck_cuotas_venta_meta;
ALTER TABLE cuotas_clientes ADD CONSTRAINT ck_cuotas_venta_meta
    CHECK (tipo = 'cuota' OR (tipo = 'venta' AND monto_meta IS NOT NULL));

CREATE INDEX IF NOT EXISTS idx_cuotas_tenant_tipo ON cuotas_clientes (tenant_id, tipo);
