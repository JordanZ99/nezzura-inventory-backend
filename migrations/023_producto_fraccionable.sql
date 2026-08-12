-- 023_producto_fraccionable.sql
-- Define si un producto se puede vender por fracciones (0.5 kg, 1.5 lt, ...).
--   false (default) = solo unidades enteras (c/u, sin sufijo, u otros enteros)
--   true            = acepta decimales (kg, lt, mt y otros marcados fraccionables)
--
-- La migración además deriva el valor inicial de los productos EXISTENTES
-- según su sufijo actual, para no romper a los tenants que ya venden por kilo.

ALTER TABLE productos ADD COLUMN IF NOT EXISTS fraccionable boolean DEFAULT false;

-- Derivación inicial: los sufijos de peso/volumen/longitud conocidos pasan a fraccionable.
UPDATE productos
SET fraccionable = true
WHERE COALESCE(sufijo_precio, '') <> ''
  AND (
        lower(sufijo_precio) IN ('kg', 'lt', 'mt', 'g', 'ml', 'm',
                                 'por kilo', 'por litro', 'por metro',
                                 'kilo', 'litro', 'metro')
     OR lower(sufijo_precio) LIKE '%kilo%'
     OR lower(sufijo_precio) LIKE '%litro%'
     OR lower(sufijo_precio) LIKE '%metro%'
     OR lower(sufijo_precio) LIKE '%gramo%'
     OR lower(sufijo_precio) LIKE '%mililitro%'
  );
