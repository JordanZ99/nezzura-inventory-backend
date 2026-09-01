-- ==============================================================================
-- 037: Mesas (Fase 2, preset restaurante). Ciclo de vida de la orden abierta.
--
-- DECISIÓN DE DISEÑO: la orden abierta NO toca `ordenes`/`ventas` con estados
-- nuevos. Vive en tablas de staging propias (mesas + mesa_items) y al cobrar
-- se convierte en una orden normal reutilizando cobrar_carrito EN LA MISMA
-- transacción (cero cambios en estadísticas, anulación, _recalcular_orden y
-- turnos, que asumen el binomio Activa/Anulada). Las órdenes cobradas ganan
-- mesa_id/mesa_nombre como snapshot de trazabilidad (la orden nace y queda
-- cobrada, exactamente igual que hoy).
--
-- Estados de mesa: 'Libre' | 'Ocupada' | 'Cuenta'.
-- La orden abierta es IMPLÍCITA: mesa Ocupada/Cuenta ⇒ tiene items en
-- mesa_items (una mesa = una orden; juntar mesas = usar otra mesa).
-- ==============================================================================

CREATE TABLE IF NOT EXISTS mesas (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    nombre     TEXT NOT NULL,
    capacidad  INTEGER,
    orden      INTEGER NOT NULL DEFAULT 0,     -- posición en la parrilla (drag & drop)
    estado     TEXT NOT NULL DEFAULT 'Libre',
    abierta_en TIMESTAMPTZ,                    -- cuándo se abrió la orden (NULL = Libre)
    creada_en  TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE mesas ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON mesas;
CREATE POLICY tenant_isolation ON mesas
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'mesas_tenant_nombre_key'
    ) THEN
        ALTER TABLE mesas
            ADD CONSTRAINT mesas_tenant_nombre_key UNIQUE (tenant_id, nombre);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_mesas_tenant_orden ON mesas (tenant_id, orden);

-- Renglones de la orden abierta de una mesa (pre-cobro). El precio lo fija el
-- POS al pedir (precio sugerido / variación / capturado); el costo de los
-- productos de stock y compuestos se resuelve con PEPS al cobrar (igual que el
-- carrito), por eso solo `costo` (opcional) para la venta libre.
CREATE TABLE IF NOT EXISTS mesa_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    mesa_id         UUID NOT NULL REFERENCES mesas(id) ON DELETE CASCADE,
    producto        TEXT NOT NULL,
    descripcion     TEXT,                        -- texto libre de la venta libre
    variacion       TEXT,
    cantidad        NUMERIC(12,3) NOT NULL DEFAULT 1,
    precio_unitario NUMERIC(14,2) NOT NULL DEFAULT 0,
    costo           NUMERIC(14,2),               -- solo venta libre; NULL = resolver al cobrar
    notas           TEXT,
    creado_en       TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE mesa_items ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON mesa_items;
CREATE POLICY tenant_isolation ON mesa_items
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

CREATE INDEX IF NOT EXISTS idx_mesa_items_mesa ON mesa_items (tenant_id, mesa_id, creado_en);

-- Trazabilidad: el ticket guarda de qué mesa salió (snapshot del nombre por si
-- la mesa se renombra después).
ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS mesa_id UUID;
ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS mesa_nombre TEXT;
CREATE INDEX IF NOT EXISTS idx_ordenes_mesa ON ordenes (mesa_id);
