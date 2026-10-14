-- ==============================================================================
-- Migración: Renombrar el template 'menu-carta' a 'grid-card'
-- Fecha: 2026-10-14
--
-- Objetivo:
--   El template de cards estilo carta pasa a llamarse 'grid-card' (nombre
--   correcto en inglés), reservando 'menu' para una plantilla NUEVA (solo
--   secciones por categoría, descripción y precio — sin imágenes).
--
--   Considera ambas variantes previas por si la base ya pasó por una de
--   ellas: 'menu-carta' (original) o 'grid-carta' (versión parcial de esta
--   misma migración).
--
--   Se renombra el valor guardado en catalogo_config.template y se recrea el
--   CHECK constraint.
--
-- Idempotente: los UPDATE no hacen nada si no hay coincidencias y el bloque
-- de constraint verifica si necesita ser recreado.
-- ==============================================================================

-- 1. Renombrar el valor en los datos existentes
UPDATE catalogo_config
SET template = 'grid-card'
WHERE template IN ('menu-carta', 'grid-carta');

-- 2. Recrear el CHECK con la lista de valores vigente
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'catalogo_config_template_check'
    ) THEN
        EXECUTE 'ALTER TABLE catalogo_config DROP CONSTRAINT catalogo_config_template_check';
    END IF;
    EXECUTE 'ALTER TABLE catalogo_config ADD CONSTRAINT catalogo_config_template_check
             CHECK (template = ANY (ARRAY[''grid-clasico'', ''grid-card'']))';
END $$;
