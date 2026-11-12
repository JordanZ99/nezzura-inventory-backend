-- ==============================================================================
-- 066: Tamaño de la VARIACIÓN en centímetros (columna de producto_variaciones).
--
-- Complementa 046 (productos.tamano_cm): cuando un mismo producto se vende en
-- varios tamaños mediante variaciones (ej. peluche "Freddy" en 30/40/50 cm),
-- el tamaño pertenece a CADA variación, no al producto. Campo OPCIONAL
-- (NULL por defecto); el catálogo lo muestra como pill "X cm" al seleccionar
-- la variación, igual que el pill del producto.
-- NUMERIC(8,2), misma semántica que 046.
-- ==============================================================================

ALTER TABLE producto_variaciones ADD COLUMN IF NOT EXISTS tamano_cm NUMERIC(8,2);
