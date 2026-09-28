-- ==============================================================================
-- 048: Corrección del CHECK constraint de hero_estilo (permite 'hero').
--
-- La migración 006_catalogo_hero_anuncios.sql creó el constraint
-- catalogo_config_hero_estilo_check validando solo ('gradiente', 'imagen').
-- La migración 047 introdujo el modo 'hero' (portada a pantalla completa)
-- pero no actualizó el constraint, por lo que el catálogo rechazaba el valor
-- con SQLSTATE 23514 (violates check constraint) al guardarlo desde Ajustes.
--
-- Idempotente: DROP CONSTRAINT IF EXISTS + re-creación en DO block.
-- ==============================================================================

ALTER TABLE catalogo_config DROP CONSTRAINT IF EXISTS catalogo_config_hero_estilo_check;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'catalogo_config_hero_estilo_check'
    ) THEN
        EXECUTE 'ALTER TABLE catalogo_config ADD CONSTRAINT catalogo_config_hero_estilo_check
                 CHECK (hero_estilo = ANY (ARRAY[''gradiente'', ''imagen'', ''hero'']))';
    END IF;
END $$;
