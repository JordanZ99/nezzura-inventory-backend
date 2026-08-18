-- ==============================================================================
-- 028_post_config_colores.sql
-- Colores de texto personalizables de las tarjetas de post (extensión de la
-- paleta de acento: ahora el texto tiene colores editables).
--
--   - color_primario   → color del NOMBRE DEL PRODUCTO + NOMBRE DEL NEGOCIO
--                        (hex #RRGGBB, o NULL/'' = color automático por plantilla).
--   - color_secundario → color del PRECIO (hex #RRGGBB, o NULL/'' = automático).
--
-- El override por producto puede llevar {"color_primario": "...",
-- "color_secundario": "..."} en productos.post_override (JSONB libre, sin
-- migración extra).
--
-- Idempotente: ADD COLUMN IF NOT EXISTS no lanza error si ya existen.
-- Se auto-aplica en backend/database/conexion.py (inicializar_db).
-- ==============================================================================

ALTER TABLE post_config ADD COLUMN IF NOT EXISTS color_primario TEXT;
ALTER TABLE post_config ADD COLUMN IF NOT EXISTS color_secundario TEXT;
