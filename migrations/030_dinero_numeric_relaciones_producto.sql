-- Dinero con precisión decimal y relaciones de inventario por ID.
-- La API puede seguir recibiendo nombres de producto; el backend los resuelve
-- a producto_id antes de escribir.

-- Campos monetarios. Las cantidades y el stock permanecen REAL porque no son dinero.
ALTER TABLE productos
    ALTER COLUMN costo_servicio TYPE NUMERIC(14,2) USING ROUND(costo_servicio::numeric, 2),
    ALTER COLUMN precio_servicio TYPE NUMERIC(14,2) USING ROUND(precio_servicio::numeric, 2);

ALTER TABLE producto_variaciones
    ALTER COLUMN precio TYPE NUMERIC(14,2) USING ROUND(precio::numeric, 2);

ALTER TABLE lotes
    ALTER COLUMN costo TYPE NUMERIC(14,2) USING ROUND(costo::numeric, 2),
    ALTER COLUMN precio_venta TYPE NUMERIC(14,2) USING ROUND(precio_venta::numeric, 2);

ALTER TABLE ventas
    ALTER COLUMN precio_lista TYPE NUMERIC(14,2) USING ROUND(precio_lista::numeric, 2),
    ALTER COLUMN precio_real TYPE NUMERIC(14,2) USING ROUND(precio_real::numeric, 2),
    ALTER COLUMN costo_unitario TYPE NUMERIC(14,2) USING ROUND(costo_unitario::numeric, 2),
    ALTER COLUMN total_venta TYPE NUMERIC(14,2) USING ROUND(total_venta::numeric, 2),
    ALTER COLUMN ganancia_bruta TYPE NUMERIC(14,2) USING ROUND(ganancia_bruta::numeric, 2);

ALTER TABLE gastos
    ALTER COLUMN monto TYPE NUMERIC(14,2) USING ROUND(monto::numeric, 2);

-- Referencia estable al producto. Producto sigue existiendo como texto por
-- compatibilidad de API/exportaciones y como copia legible histórica.
ALTER TABLE lotes ADD COLUMN IF NOT EXISTS producto_id INTEGER;
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS producto_id INTEGER;

UPDATE lotes l
SET producto_id = p.id
FROM productos p
WHERE l.producto_id IS NULL
  AND l.producto = p.producto
  AND l.tenant_id = p.tenant_id;

UPDATE ventas v
SET producto_id = p.id
FROM productos p
WHERE v.producto_id IS NULL
  AND v.producto = p.producto
  AND v.tenant_id = p.tenant_id;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM lotes WHERE producto_id IS NULL) THEN
        RAISE EXCEPTION 'No se puede crear FK: existen lotes sin producto_id';
    END IF;
    IF EXISTS (SELECT 1 FROM ventas WHERE producto_id IS NULL) THEN
        RAISE EXCEPTION 'No se puede crear FK: existen ventas sin producto_id';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'lotes_producto_id_fkey'
    ) THEN
        ALTER TABLE lotes
            ADD CONSTRAINT lotes_producto_id_fkey
            FOREIGN KEY (producto_id) REFERENCES productos(id) ON DELETE RESTRICT;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ventas_producto_id_fkey'
    ) THEN
        ALTER TABLE ventas
            ADD CONSTRAINT ventas_producto_id_fkey
            FOREIGN KEY (producto_id) REFERENCES productos(id) ON DELETE RESTRICT;
    END IF;
END $$;

ALTER TABLE lotes ALTER COLUMN producto_id SET NOT NULL;
ALTER TABLE ventas ALTER COLUMN producto_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_lotes_tenant_producto_id
    ON lotes (tenant_id, producto_id, estado);
CREATE INDEX IF NOT EXISTS idx_ventas_tenant_producto_id
    ON ventas (tenant_id, producto_id);
