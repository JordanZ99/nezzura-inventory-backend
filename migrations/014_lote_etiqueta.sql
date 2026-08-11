-- 014_lote_etiqueta.sql
-- Etiqueta/presentación opcional por lote (tamaño de maceta, calidad, oferta, etc.)
-- Se muestra en el selector de lote del POS y en la gestión de lotes.
-- Ej: "20cm", "Premium", "Hojas dañadas - oferta"
ALTER TABLE lotes ADD COLUMN IF NOT EXISTS etiqueta text DEFAULT '';
