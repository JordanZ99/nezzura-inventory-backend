-- ==============================================================================
-- 067: Claves MCP editables — nivel y límites por clave + eliminación real.
--
-- Nivel editable: cambiar nivel NO toca el hash de la clave → el agente no se
-- reconecta; el middleware lee el nivel en cada request y tools/list se ajusta
-- solo.
--
-- Límites por clave: NULL = usa el default del nivel (mismo comportamiento de
-- siempre). Valor = override para apretar o aflojar a UNA clave sin tocar
-- código ni crear claves nuevas.
--
-- La eliminación (hard DELETE de la fila) no necesita migración: la clave
-- deja de validar al instante porque la validación busca el hash y ya no
-- existe. Los borradores huérfanos de esa clave expiran solos y sus altas ya
-- registradas en registro_actividad se conservan.
-- ==============================================================================

ALTER TABLE mcp_api_keys
    ADD COLUMN IF NOT EXISTS limite_llamadas_diarias INTEGER,
    ADD COLUMN IF NOT EXISTS limite_escrituras_diarias INTEGER;

-- Los overrides no tienen sentido en 0 o negativos: NULL siempre es "default".
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'mcp_limite_llamadas_check'
    ) THEN
        ALTER TABLE mcp_api_keys
            ADD CONSTRAINT mcp_limite_llamadas_check CHECK (limite_llamadas_diarias IS NULL OR limite_llamadas_diarias > 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'mcp_limite_escrituras_check'
    ) THEN
        ALTER TABLE mcp_api_keys
            ADD CONSTRAINT mcp_limite_escrituras_check CHECK (limite_escrituras_diarias IS NULL OR limite_escrituras_diarias > 0);
    END IF;
END $$;

-- La lista ya no muestra revocadas (eliminar = borrar la fila); purga las
-- legacy para que no queden claves muertas invisibles.
DELETE FROM mcp_api_keys WHERE revocada_en IS NOT NULL;
