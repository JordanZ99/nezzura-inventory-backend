-- =============================================================================
-- Migración 021 — Fase 5 (Pulido): stock decimal + foto por variación
-- =============================================================================
-- 1) Stock decimal: lotes.Stock_Lote pasa de INTEGER a REAL para poder vender
--    fracciones (0.5 kg, 150g, 0.25m...). PostgreSQL convierte INTEGER → REAL
--    automáticamente (los valores existentes se conservan).
-- 2) ventas.Cantidad pasa de INTEGER a REAL: la venta registra exactamente lo
--    vendido (0.5, 1.5, ...) para que editar/anular reviertan sin redondeos.
--    Esto corrige la "deriva fraccionaria" documentada en la Fase 3: antes,
--    descontar 1.5 de un lote INTEGER redondeaba y el consumo quedaba
--    desincronizado del stock real.
-- 3) producto_variaciones.foto: foto propia por variación, para que el
--    catálogo muestre la foto de la presentación seleccionada (ej. la foto
--    de la hamburguesa Doble vs la Sencilla).
-- =============================================================================

ALTER TABLE lotes ALTER COLUMN Stock_Lote TYPE REAL;
ALTER TABLE ventas ALTER COLUMN Cantidad  TYPE REAL;

ALTER TABLE producto_variaciones ADD COLUMN IF NOT EXISTS foto TEXT DEFAULT '';
