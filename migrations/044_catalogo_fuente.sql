-- ==============================================================================
-- 044: Tipografía del catálogo (columna fuente en catalogo_config).
--
-- Fuente "display" (nombres, precios, títulos de sección y hero) elegible por
-- el tenant desde Personalización > Apariencia. El descuento (descripciones,
-- stock, UI) queda en la sans neutra por contraste visual.
--   'serif' = default de TODOS los catálogos (la serif elegante del Menú Carta)
--   Otras claves del frontend/lib/catalogo-fuentes.ts (Playfair, Poppins,
--   Quicksand, Oswald, Baloo 2, sistema…); clave desconocida → 'serif'.
-- ==============================================================================

ALTER TABLE catalogo_config ADD COLUMN IF NOT EXISTS fuente TEXT NOT NULL DEFAULT 'serif';
