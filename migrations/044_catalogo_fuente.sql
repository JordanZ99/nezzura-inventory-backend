-- ==============================================================================
-- 044: Tipografía del catálogo (columna fuente en catalogo_config).
--
-- Fuente "display" (nombres, precios, títulos de sección y hero) elegible por
-- el tenant desde Personalización > Apariencia. El resto (descripciones,
-- stock, UI) queda en la sans neutra por contraste visual.
--
--   'sistema' = default de TODOS los catálogos: la sans neutral del catálogo,
--               sin display (idéntico a su comportamiento histórico).
--   'serif'   = la serif elegante del Menú Carta (ahora es opcional).
--
-- Otras claves del frontend/lib/catalogo-fuentes.ts; clave desconocida → 'sistema'.
--
-- NOTA (one-time): si tu DB ya corrió la versión previa de 044 con default
-- 'serif', convierte las filas existentes manualmente UNA sola vez:
--   UPDATE catalogo_config SET fuente = 'sistema' WHERE fuente = 'serif';
-- (No va en este archivo para no sobreescribir la elección del tenant
--  en cada arranque del backend.)
-- ==============================================================================

ALTER TABLE catalogo_config ADD COLUMN IF NOT EXISTS fuente TEXT;

-- Asegura el nuevo default (idempotente; corrige instalaciones ya creadas)
ALTER TABLE catalogo_config ALTER COLUMN fuente SET DEFAULT 'sistema';
