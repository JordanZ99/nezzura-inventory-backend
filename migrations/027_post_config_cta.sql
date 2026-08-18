-- ==============================================================================
-- 027_post_config_cta.sql
-- Fase 4 de Posts Automáticos: CTA configurable en la tarjeta (§9.4).
--
-- La tarjeta PNG no es clicable, pero el CTA se DIBUJA como un botón visual
-- (pill) con el texto del negocio (ej. "Pedir por WhatsApp", "Ver catálogo").
--   - cta_texto: texto del botón (NULL = sin CTA).
--   - cta_url:   URL opcional (no se dibuja en el PNG; útil para la descripción
--                al compartir o para el futuro).
--
-- El override por producto puede llevar {"cta": {"texto": "...", "url": "..."}}
-- en productos.post_override (JSONB libre, sin migración extra).
--
-- Idempotente: ADD COLUMN IF NOT EXISTS no lanza error si ya existen.
-- Se auto-aplica en backend/database/conexion.py (inicializar_db).
-- ==============================================================================

ALTER TABLE post_config ADD COLUMN IF NOT EXISTS cta_texto TEXT;
ALTER TABLE post_config ADD COLUMN IF NOT EXISTS cta_url TEXT;
