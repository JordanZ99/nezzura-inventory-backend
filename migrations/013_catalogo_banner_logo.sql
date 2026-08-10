-- =============================================================================
-- 013_catalogo_banner_logo.sql
-- Nuevo toggle: mostrar el logo del negocio sobre el banner del catálogo.
--
--   - banner_mostrar_logo → true (default): muestra el logo encima del título
--     en el hero (modo imagen y modo gradiente).
--   - false: oculta el logo del hero (solo se ve el título/texto).
-- =============================================================================

ALTER TABLE catalogo_config
    ADD COLUMN IF NOT EXISTS banner_mostrar_logo boolean DEFAULT true;
