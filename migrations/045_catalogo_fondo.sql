-- ==============================================================================
-- 045: Fondo del catálogo con imagen (textura) + color + opacidad.
--
-- El catálogo solo podía pintar el banner como imagen de portada (hero imagen);
-- el fondo del cuerpo era exclusivamente el color del tema elegido. Con esto el
-- tenant puede subir una imagen/textura de fondo y configurarla:
--
--   fondo_url      : URL de Cloudinary ('' = sin imagen de fondo, usa el tema).
--   fondo_modo     : 'cover' (foto a pantalla completa) | 'repeat' (textura
--                    tileada). Desconocido → 'cover'.
--   fondo_opacidad : 0-100. La imagen se dibuja ENCIMA del color de fondo con
--                    esta opacidad: al bajarla se mezcla con el color elegido.
--   fondo_color    : hex #RRGGBB para el fondo ('' = usa el color del tema).
--
-- Comportamiento por defecto (todas las columnas vacías): idéntico al actual,
-- el fondo sigue siendo el color del tema — cero regresiones.
-- ==============================================================================

ALTER TABLE catalogo_config ADD COLUMN IF NOT EXISTS fondo_url TEXT;
ALTER TABLE catalogo_config ADD COLUMN IF NOT EXISTS fondo_modo TEXT;
ALTER TABLE catalogo_config ADD COLUMN IF NOT EXISTS fondo_opacidad INT;
ALTER TABLE catalogo_config ADD COLUMN IF NOT EXISTS fondo_color TEXT;

-- Defaults idempotentes (corrige instalaciones ya creadas)
ALTER TABLE catalogo_config ALTER COLUMN fondo_modo SET DEFAULT 'cover';
ALTER TABLE catalogo_config ALTER COLUMN fondo_opacidad SET DEFAULT 100;
ALTER TABLE catalogo_config ALTER COLUMN fondo_color SET DEFAULT '';
