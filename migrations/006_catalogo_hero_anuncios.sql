-- ==============================================================================
-- Migración: Hero + Anuncios para el catálogo público
-- Fecha: 2026-07-30
--
-- Objetivo:
--   Permitir personalizar la cabecera del catálogo público:
--     - banner_url    : URL de imagen de banner/hero (Cloudinary)
--     - hero_estilo   : 'gradiente' (actual) | 'imagen' (banner como fondo del hero)
--     - anuncio_texto : texto de la barra de anuncios (vacío = oculta)
--
-- Idempotente: ADD COLUMN IF NOT EXISTS no lanza error si la columna ya existe.
-- ==============================================================================

-- 1. Agregar columnas con valores por defecto
ALTER TABLE catalogo_config
    ADD COLUMN IF NOT EXISTS banner_url    text DEFAULT '',
    ADD COLUMN IF NOT EXISTS hero_estilo   text DEFAULT 'gradiente',
    ADD COLUMN IF NOT EXISTS anuncio_texto text DEFAULT '';

-- 2. Constraint CHECK para hero_estilo (misma técnica que 003_catalogo_templates.sql)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'catalogo_config_hero_estilo_check'
    ) THEN
        EXECUTE 'ALTER TABLE catalogo_config ADD CONSTRAINT catalogo_config_hero_estilo_check
                 CHECK (hero_estilo = ANY (ARRAY[''gradiente'', ''imagen'']))';
    END IF;
END $$;
