-- ==============================================================================
-- Migración: Personalización del texto del banner (modo imagen)
-- Fecha: 2026-08-10
--
-- Objetivo:
--   Dar al tenant control sobre el texto que se muestra sobre el banner
--   cuando la portada está en modo "imagen" (hero_estilo = 'imagen'):
--
--     - banner_texto_color   → color del título/subtítulo sobre la foto (hex)
--     - banner_mostrar_texto → true (default): título + subtítulo sobre la foto
--                              false: solo se muestra la imagen, sin texto encima
--
--   Solo aplican al modo imagen: en modo gradiente el texto va sobre el
--   degradado del tema y se mantiene blanco siempre.
--
-- Idempotente: ADD COLUMN IF NOT EXISTS no lanza error si ya existen.
-- También se auto-aplica en backend/database/conexion.py (inicializar_db).
-- ==============================================================================

ALTER TABLE catalogo_config
    ADD COLUMN IF NOT EXISTS banner_texto_color text DEFAULT '#ffffff';

ALTER TABLE catalogo_config
    ADD COLUMN IF NOT EXISTS banner_mostrar_texto boolean DEFAULT true;
