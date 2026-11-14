-- ==============================================================================
-- 077: Órdenes de servicio (giro 'servicios'): el registro del TRABAJO, no del
-- dinero. Genérico a propósito — sirve para reparaciones, instalaciones,
-- consultoría, mantenimiento o lo que surja (no solo talleres).
--
-- El workflow mínimo de todo servicio real (patrón de los sistemas de
-- reparación: intake → diagnóstico → espera → trabajo → entrega):
--   recibido → en_proceso → esperando (cliente/insumo/aprobación) →
--   listo → entregado   (+ cancelado)
--
-- El dinero NO vive aquí: el cobro es una CONSECUENCIA (cobro puntual o
-- venta a plazos vía cobros/cuotas_clientes); si se cobra, `cobro_id` queda
-- enlazado por trazabilidad. Precio es informativo hasta cobrarse.
-- ==============================================================================

CREATE TABLE IF NOT EXISTS ordenes_servicio (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    cliente_id      UUID REFERENCES clientes(id) ON DELETE SET NULL,
    -- Qué tipo de trabajo es (texto con sugerencias en la UI; 'otro' libre).
    tipo            TEXT NOT NULL DEFAULT 'otro'
                    CHECK (tipo IN ('reparacion', 'instalacion', 'consultoria', 'mantenimiento', 'otro')),
    -- Qué se hizo/pide, en una línea ("Cambio de pantalla iPhone 11").
    titulo          TEXT NOT NULL,
    -- Sobre qué se trabaja (genérico: modelo/serie/IMEI, dominio, local...).
    equipo          TEXT,
    -- Lo que reportó el cliente (su versión, en sus palabras).
    detalle_reportado TEXT,
    -- Lo que encontraste/hiciste tú (tu versión, técnico).
    solucion        TEXT,
    -- Estado del flujo (CHECK arriba). fecha_entregado se llena al pasar a
    -- 'entregado' desde el backend.
    estado          TEXT NOT NULL DEFAULT 'recibido',
    fecha_recibido  DATE NOT NULL DEFAULT CURRENT_DATE,
    fecha_entregado DATE,
    -- Informativo hasta que se cobra (el dinero real vive en cobros/pagos).
    precio          NUMERIC(12,2) CHECK (precio IS NULL OR precio >= 0),
    -- Trazabilidad: el cobro (o la cuota-venta a plazos) generado por esta orden.
    cobro_id        UUID REFERENCES cobros(id) ON DELETE SET NULL,
    notas           TEXT,
    creado_en       TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE ordenes_servicio ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON ordenes_servicio;
CREATE POLICY tenant_isolation ON ordenes_servicio
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

CREATE INDEX IF NOT EXISTS idx_ordenes_serv_tenant ON ordenes_servicio (tenant_id, estado);
CREATE INDEX IF NOT EXISTS idx_ordenes_serv_cliente ON ordenes_servicio (tenant_id, cliente_id);
