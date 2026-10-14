-- ==============================================================================
-- Migración: Agregar el template 'menu' a catalogo_config
-- Fecha: 2026-10-14
--
-- Objetivo:
--   Nueva plantilla de catálogo SIN imágenes: secciones divididas por
--   categoría con platillos (nombre, descripción y precio), como una carta
--   impresa. Se agrega al CHECK constraint de la migración 050.
--
-- Idempotente: el bloque verifica si el constraint necesita ser recreado.
-- No hay datos previos con 'menu', así que solo toca la lista de valores.
-- ==============================================================================

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'catalogo_config_template_check'
    ) THEN
        EXECUTE 'ALTER TABLE catalogo_config DROP CONSTRAINT catalogo_config_template_check';
    END IF;
    EXECUTE 'ALTER TABLE catalogo_config ADD CONSTRAINT catalogo_config_template_check
             CHECK (template = ANY (ARRAY[''grid-clasico'', ''grid-card'', ''menu'']))';
END $$;
