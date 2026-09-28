-- 015_tenant_modo_precio_sugerido.sql
-- Modo de precio sugerido en el punto de venta (por tenant).
--   'antiguo'  → lote más antiguo con stock (recomendado, coincide con PEPS)
--   'maximo'   → máximo precio entre los lotes con stock
--   'reciente' → lote más reciente con stock
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS modo_precio_sugerido text DEFAULT 'antiguo';
