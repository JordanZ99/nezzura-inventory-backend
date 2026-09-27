-- ==============================================================================
-- 046: Tamaño del producto en centímetros (columna de productos).
--
-- Campo OPCIONAL por producto (NULL por defecto). Se muestra como pill en el
-- modal del catálogo público (ej. "25 cm h") para plantas y otros ítems cuyo
-- tamaño físico es parte de la decisión de compra del cliente.
-- NUMERIC(8,2) admite tamaños hasta ~999999.99 cm con 2 decimales.
-- ==============================================================================

ALTER TABLE productos ADD COLUMN IF NOT EXISTS tamano_cm NUMERIC(8,2);
