-- ==============================================================================
-- 049: Layout personalizable del Hero (Fase 1, catalogo_config).
--
-- Complementa el modo 'hero' (migración 047) con un dict PARCIAL de reglas.
-- Las claves ausentes están vacías → el render usa los defaults actuales
-- (texto centro, logo y botón visibles, sin redes sociales). Cero regresiones.
--
--   hero_layout : {
--     texto_posicion : 'centro' | 'arriba-izq' | 'abajo-izq'
--     mostrar_logo   : boolean (null/true = visible)
--     mostrar_redes  : boolean (null/false = oculto, default)
--     mostrar_boton  : boolean (null/true = botón "Ver el catálogo" visible)
--   }
--
-- Se guarda como JSONB para poder extender la Fase 2 (coordenadas por
-- breakpoint) sin otra migración.
-- ==============================================================================

ALTER TABLE catalogo_config ADD COLUMN IF NOT EXISTS hero_layout JSONB;
