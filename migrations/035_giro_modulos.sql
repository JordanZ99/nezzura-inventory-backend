-- ==============================================================================
-- 035: Giros (Fase 0). El giro define el PRESET de módulos del negocio:
--   'tienda'      → preset actual (POS de productos con stock/servicio/compuesto)
--   'restaurante' → POS con parrilla de mesas (módulo 'mesas', Fase 2)
-- Todos los tenants existentes quedan en 'tienda' (compatibilidad total: la
-- app se comporta exactamente igual que antes).
--
-- tenants.modulos (JSONB) es el OVERRIDE del preset: NULL = "usa el preset del
-- giro" (el frontend resuelve con PRESETS_GIRO en useModulo). Cuando exista UI
-- de módulos, escribir aquí { "mesas": true } para prender/apagar por tenant
-- (ej. un restaurante que también vende merch, o una tienda que quiere mesas).
-- ==============================================================================

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS giro TEXT NOT NULL DEFAULT 'tienda';

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS modulos JSONB;
