-- ==============================================================================
-- Migración: Renombrar el template 'menu-carta' a 'grid-card'
-- Fecha: 2026-10-14
--
-- Objetivo:
--   El template de cards estilo carta pasa a llamarse 'grid-card' (nombre
--   correcto en inglés), reservando 'menu' para una plantilla NUEVA (solo
--   secciones por categoría, descripción y precio — sin imágenes).
--
--   ORDEN CRÍTICO: primero se SUELTA el CHECK (que no permite 'grid-card'),
--   después se renombra el dato y al final se recrea el constraint.
--   Antes el UPDATE iba primero y crasheaba el arranque con:
--     "violates check constraint catalogo_config_template_check"
--   en bases que tenían rows 'menu-carta'.
--
--   Idempotente: DROP IF EXISTS + UPDATE sin filas coincidentes no hacen nada
--   + checar recreate con la lista completa vigente (incluye 'menu', la
--   agrega la migración 053 en el orden normal, pero aquí ya se permite para
--   re-ejecuciones sobre bases donde 053 corrió antes).
-- ==============================================================================

-- 1. Soltar el CHECK antes de tocar los datos
ALTER TABLE catalogo_config
    DROP CONSTRAINT IF EXISTS catalogo_config_template_check;

-- 2. Renombrar el valor en los datos existentes
UPDATE catalogo_config
SET template = 'grid-card'
WHERE template IN ('menu-carta', 'grid-carta');

-- 3. Recrear el CHECK con la lista vigente (la misma de la 053)
ALTER TABLE catalogo_config
    ADD CONSTRAINT catalogo_config_template_check
    CHECK (template = ANY (ARRAY['grid-clasico'::text, 'grid-card'::text, 'menu'::text]));
