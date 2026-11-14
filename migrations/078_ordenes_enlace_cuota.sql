-- ==============================================================================
-- 078: Corrección de 077 — el enlace de la orden puede apuntar a un COBRO
-- puntual o a una CUOTA (venta a plazos generada desde "Convertir en cobro").
-- Un solo cobro_id con FK a cobros no alcanza: la cuota violaba la FK.
-- Solución relacional limpia: dos columnas, dos FKs.
-- ==============================================================================

ALTER TABLE ordenes_servicio ADD COLUMN IF NOT EXISTS cuota_id
    UUID REFERENCES cuotas_clientes(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_ordenes_serv_cuota ON ordenes_servicio (tenant_id, cuota_id);
