-- =============================================================================
-- 059_catalogo_banner_redes.sql
-- Nuevo toggle: iconos de redes sociales del negocio sobre el banner/hero.
--
--   - banner_mostrar_redes → true (default): muestra Instagram, Facebook,
--     TikTok y WhatsApp del negocio en la esquina inferior derecha del
--     banner (modo imagen) y al final del header (modo gradiente).
--   - false: oculta los iconos en ambos modos.
--
-- Solo se dibujan las redes que el negocio llenó en Identidad del Negocio
-- (contacto del tenant); el contenido no cambia, solo la visibilidad.
-- =============================================================================

ALTER TABLE catalogo_config
    ADD COLUMN IF NOT EXISTS banner_mostrar_redes boolean DEFAULT true;
