-- ==============================================================================
-- Migración: Banner específico para móvil
-- Fecha: 2026-08-10
--
-- Objetivo:
--   Permitir al tenant subir dos banners distintos para el catálogo:
--
--     - banner_url       → banner para escritorio (1920 × 373 recomendado)
--     - banner_url_movil → banner para móviles (750 × 310 recomendado)
--
--   En el catálogo público, si hay banner móvil y el visitante está en un
--   dispositivo pequeño, se usa el banner móvil; si no, el de escritorio.
--
-- Idempotente: ADD COLUMN IF NOT EXISTS no lanza error si ya existe.
-- También se auto-aplica en backend/database/conexion.py (inicializar_db).
-- ==============================================================================

ALTER TABLE catalogo_config
    ADD COLUMN IF NOT EXISTS banner_url_movil text DEFAULT '';
