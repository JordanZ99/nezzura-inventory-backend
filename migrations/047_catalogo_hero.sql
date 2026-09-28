-- ==============================================================================
-- 047: Modo Hero de portada (catalogo_config).
--
-- Nuevo valor de hero_estilo: 'hero'. Cuando está activo y hay imagen
-- cargada, el catálogo muestra una portada a PANTALLA COMPLETA (100vh) con
-- overlay configurable y botón para bajar al catálogo (heredable en cualquier
-- plantilla: grid-clasico y menu-carta).
--
--   hero_url        : URL Cloudinary de la imagen hero para ESCRITORIO
--                     (crop 16:9, recomendado 1920×1080).
--   hero_url_movil  : URL Cloudinary para MÓVIL (crop retrato, 750×1334).
--                     '' = usa hero_url con recorte centrado.
--   hero_color      : hex #RRGGBB del velo sobre la imagen ('' = gradiente
--                     oscuro página, similar al modo gradiente del tema).
--   hero_opacidad   : 0-100, opacidad del velo sobre la imagen.
--
-- Comportamiento por defecto (columnas vacías): idéntico al actual — hero
-- desactivado, cero regresiones.
-- ==============================================================================

ALTER TABLE catalogo_config ADD COLUMN IF NOT EXISTS hero_url TEXT;
ALTER TABLE catalogo_config ADD COLUMN IF NOT EXISTS hero_url_movil TEXT;
ALTER TABLE catalogo_config ADD COLUMN IF NOT EXISTS hero_color TEXT;
ALTER TABLE catalogo_config ADD COLUMN IF NOT EXISTS hero_opacidad INT;

-- Defaults idempotentes (corrige instalaciones ya creadas)
ALTER TABLE catalogo_config ALTER COLUMN hero_color SET DEFAULT '';
ALTER TABLE catalogo_config ALTER COLUMN hero_opacidad SET DEFAULT 40;
