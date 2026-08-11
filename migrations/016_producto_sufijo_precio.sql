-- 016_producto_sufijo_precio.sql
-- Sufijo opcional del precio mostrado en el catálogo, por producto.
--   ''        → sin sufijo (solo el número, ej. "$35.00")
--   'c/u'     → ej. "$35.00 c/u"
--   'por kilo'→ ej. "$35.00 por kilo"
--   'por litro' → sufijo libre
-- Se guarda el texto final tal cual se mostrará (vacío = no mostrar nada).
ALTER TABLE productos ADD COLUMN IF NOT EXISTS sufijo_precio text DEFAULT '';
