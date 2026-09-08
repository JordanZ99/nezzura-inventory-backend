-- ==============================================================================
-- 038: Cartera de clientes (Fase A del sistema de puntos, doc sistemaPuntos.md).
--
-- Un cliente pertenece a UN negocio (tenant). El nombre siempre es obligatorio;
-- email / telefono / contraseña (pin) son configurables por el tenant desde
-- Ajustes → Mi Negocio vía tenants.cliente_campos (JSONB): cada campo lleva
-- {activo, requerido}. Si un campo es "opcional" puede quedar NULL: por eso la
-- unicidad es un índice parcial único (los NULL nunca chocan entre sí).
--
-- El saldo de puntos NUNCA vive en el cliente: se calcula sumando el libro de
-- movimientos (puntos_movimientos, migración 039). Aquí solo dejamos el enlace
-- ventas↔cliente (ordenes.cliente_id) que alimenta la analítica.
-- ==============================================================================

CREATE TABLE IF NOT EXISTS clientes (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    nombre         TEXT NOT NULL,
    email          TEXT,
    telefono       TEXT,
    -- Contraseña de identificación en el POS (el cliente la dice al vendedor).
    -- Se guarda hasheada (pbkdf2_sha256$salt$digest), jamás en texto plano.
    pin_hash       TEXT,
    notas          TEXT,
    activo         BOOLEAN NOT NULL DEFAULT true,
    fecha_registro TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE clientes ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON clientes;
CREATE POLICY tenant_isolation ON clientes
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

-- Únicos por negocio SOLO cuando el dato existe (opcional ⇒ NULL permitido).
CREATE UNIQUE INDEX IF NOT EXISTS uq_clientes_tenant_email
    ON clientes (tenant_id, email) WHERE email IS NOT NULL AND email <> '';
CREATE UNIQUE INDEX IF NOT EXISTS uq_clientes_tenant_telefono
    ON clientes (tenant_id, telefono) WHERE telefono IS NOT NULL AND telefono <> '';

CREATE INDEX IF NOT EXISTS idx_clientes_tenant ON clientes (tenant_id, activo);
CREATE INDEX IF NOT EXISTS idx_clientes_busqueda ON clientes (tenant_id, lower(nombre));

-- Trazabilidad: el ticket completo se liga al cliente (la analítica por cliente
-- se construye sobre ordenes.total / fecha_ts; no hace falta tocar `ventas`).
ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS cliente_id UUID REFERENCES clientes(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_ordenes_cliente ON ordenes (tenant_id, cliente_id);

-- ── Config de la cartera en tenants (patrón establecido: columnas, no tabla) ──
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS clientes_activos BOOLEAN NOT NULL DEFAULT false;

-- Defaults según el diseño: email y teléfono activos y obligatorios; la
-- contraseña (pin) desactivada hasta que el tenant la pida. "nombre" es
-- siempre obligatorio y NO es configurable, por eso no va en el JSONB.
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS cliente_campos JSONB NOT NULL DEFAULT
    '{"email":{"activo":true,"requerido":true},"telefono":{"activo":true,"requerido":true},"pin":{"activo":false,"requerido":false}}'::jsonb;
