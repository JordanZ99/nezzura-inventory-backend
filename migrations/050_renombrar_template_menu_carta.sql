-- ==============================================================================
-- Migración: Renombrar el template 'menu-carta' a 'grid-carta'
-- Fecha: 2026-10-14
--
-- Objetivo:
--   El template de cards estilo carta pasa a llamarse 'grid-carta' para
--   reservar el nombre 'menu' para una plantilla NUEVA (solo secciones por
--   categoría, descripción y precio — sin imágenes).
--
--   Se renombra el valor guardado en catalogo_config.template y se recrea el
--   CHECK constraint (el original, de la migración 003, solo aceptaba
--   'grid-clasico' y 'menu-carta').
--
-- Idempotente: los UPDATE no hacen nada si no hay coincidencias y el bloque
-- de constraint verifica si necesita ser recreado.
-- ==============================================================================

-- 1. Renombrar el valor en los datos existentes
UPDATE catalogo_config
SET template = 'grid-carta'
WHERE template = 'menu-carta';

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
             CHECK (template = ANY (ARRAY[''grid-clasico'', ''grid-carta'']))';
END $$;
