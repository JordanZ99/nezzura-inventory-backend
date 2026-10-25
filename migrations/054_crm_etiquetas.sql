-- ==============================================================================
-- 054: CRM — etiquetas libres de clientes + cumpleaños + origen de campaña.
--
-- Fase A del CRM interno (sustituye/añade sobre la cartera de la mig 038).
--
-- etiquetas: catálogo LIBRE por tenant (el dueño crea las que quiera:
--   'VIP', 'Vecino', 'Mayorista'...). 'color' es un hex para pintar el chip
--   en la UI. Unicidad insensible a mayúsculas dentro del tenant (índice
--   funcional sobre lower(nombre)) para evitar 'VIP' vs 'vip'.
--
-- cliente_etiqueta: relación n:n (PK compuesta). CASCADE por ambos lados:
--   si muere el cliente o la etiqueta, se limpia la relación sola.
--
-- clientes (ALTER): 'cumpleanos' (DATE, base para promos / segmento
--   'cumpleañeros') y 'origen' (TEXT libre, de dónde llegó el cliente:
--   campaña de marketing, referred, walk-in... — requisito de integración de
--   campaña del CRM). Nombres ASCII (sin ñ) a propósito.
-- ==============================================================================

CREATE TABLE IF NOT EXISTS etiquetas (
    id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    nombre    TEXT NOT NULL,
    color     TEXT NOT NULL DEFAULT '#6366f1'
);

ALTER TABLE etiquetas ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON etiquetas;
CREATE POLICY tenant_isolation ON etiquetas
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

-- Un nombre (insensible a mayúsculas) por tenant.
CREATE UNIQUE INDEX IF NOT EXISTS uq_etiquetas_tenant_nombre
    ON etiquetas (tenant_id, lower(nombre));
CREATE INDEX IF NOT EXISTS idx_etiquetas_tenant
    ON etiquetas (tenant_id);

-- ── Relación cliente ↔ etiqueta (n:n, PK compuesta = imposible duplicar) ──
CREATE TABLE IF NOT EXISTS cliente_etiqueta (
    cliente_id  UUID NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    etiqueta_id UUID NOT NULL REFERENCES etiquetas(id) ON DELETE CASCADE,
    PRIMARY KEY (cliente_id, etiqueta_id)
);

ALTER TABLE cliente_etiqueta ENABLE ROW LEVEL SECURITY;
-- cliente_etiqueta NO lleva tenant_id propio (el dueño es el CLIENTE). La
-- policy aísla vía subquery: una fila solo es visible/emitable si su cliente
-- pertenece al tenant del usuario (Supabase permite subqueries en las policies).
DROP POLICY IF EXISTS tenant_isolation ON cliente_etiqueta;
CREATE POLICY tenant_isolation ON cliente_etiqueta
    FOR ALL
    USING (
        cliente_id IN (
            SELECT id FROM clientes WHERE tenant_id = auth.uid()
        )
    )
    WITH CHECK (
        cliente_id IN (
            SELECT id FROM clientes WHERE tenant_id = auth.uid()
        )
    );

-- Chips en la lista de clientes: etiquetas por cliente, lo más común.
CREATE INDEX IF NOT EXISTS idx_cliente_etiqueta_cliente
    ON cliente_etiqueta (cliente_id);
-- Inverso (fichas de la etiqueta: doorway para segmentos futuros).
CREATE INDEX IF NOT EXISTS idx_cliente_etiqueta_etiqueta
    ON cliente_etiqueta (etiqueta_id);

-- ── El cliente se enriquece: cumpleaños y origen de campaña ──
ALTER TABLE clientes ADD COLUMN IF NOT EXISTS cumpleanos DATE;
ALTER TABLE clientes ADD COLUMN IF NOT EXISTS origen TEXT;
