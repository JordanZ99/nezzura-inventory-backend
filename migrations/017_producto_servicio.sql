-- 017_producto_servicio.sql
-- Productos de servicio (sin stock) — Fase 1
--   'stock'    → producto normal con lotes (comportamiento actual)
--   'servicio' → no tiene inventario (ej. corte de cabello); se vende infinito
-- Los servicios guardan su costo y precio de venta en el propio producto,
-- porque no tienen lotes donde vivan esos valores.
ALTER TABLE productos ADD COLUMN IF NOT EXISTS tipo_producto text DEFAULT 'stock';
ALTER TABLE productos ADD COLUMN IF NOT EXISTS costo_servicio real DEFAULT 0;
ALTER TABLE productos ADD COLUMN IF NOT EXISTS precio_servicio real DEFAULT 0;

-- Las ventas guardan el tipo del producto vendido (stock | servicio): así,
-- al editar o anular una venta, el sistema sabe si debe tocar inventario.
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS tipo_producto text DEFAULT 'stock';
