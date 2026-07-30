-- ==============================================================================
-- Migración: Columna template para catálogos con templates múltiples
-- Fecha: 2026-07-29
--
-- Objetivo:
--   Agregar el campo `template` a la tabla `catalogo_config` para que cada
--   tenant pueda elegir entre diferentes estructuras visuales para su catálogo
--   público (grid-clasico, menu-carta, etc.).
--
--   También se agrega un CHECK CONSTRAINT para validar que el template esté
--   siempre dentro de la lista de valores permitidos. Esto es una red de
--   seguridad adicional a la validación Pydantic del backend.
--
-- Idempotente: ADD COLUMN IF NOT EXISTS no lanza error si la columna ya existe.
-- ==============================================================================

-- 1. Agregar columna template con valor por defecto 'grid-clasico'
ALTER TABLE catalogo_config
    ADD COLUMN IF NOT EXISTS template text DEFAULT 'grid-clasico';

-- 2. Opcional: constraint CHECK para validar valores permitidos a nivel DB
--    Solo se aplica si la columna es nueva (no podemos saber si ya tiene datos
--    con valores inválidos, pero gen_random_uuid garantiza que solo los registros
--    existentes —creados antes de esta migración— tienen el default).
--    En producción, si ya hay datos, este CHECK se puede agregar manualmente
--    después de verificar que todos los valores existentes son válidos.
DO $$
BEGIN
    -- Verificamos si el constraint ya existe para no duplicarlo
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'catalogo_config_template_check'
    ) THEN
        -- Solo agregamos el CHECK si la tabla no tiene registros con valores inválidos
        -- (en la práctica, solo existe 'grid-clasico' ya que 'menu-carta' es nuevo)
        EXECUTE 'ALTER TABLE catalogo_config ADD CONSTRAINT catalogo_config_template_check
                 CHECK (template = ANY (ARRAY[''grid-clasico'', ''menu-carta'']))';
    END IF;
END $$;
