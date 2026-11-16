-- ==============================================================================
-- 081_mcp_roles.sql — Roles funcionales para las claves del servidor MCP.
--
-- Reemplaza la escalera de niveles (1/2/3) como criterio de QUÉ TOOLS ve cada
-- clave (muse/roles.py es la fuente única de verdad). El campo `nivel` NO se
-- toca: se sigue derivando de los roles (máximo) porque alimenta los rate
-- limits (066/067) y la lógica de 'ver costos' (068).
--
-- Formato: texto 'rol1,rol2' (lista corta y legible; la validación fina vive
-- en la app, muse/roles.normalizar_roles). NULL/'' = todas las tools (solo
-- debería existir transitoriamente si esta migración corre a medias).
--
-- Mapeo de las claves EXISTENTES por su nivel (quedan afiladas, no
-- todoterreno — el nivel ya codificaba la intención del dueño):
--   nivel 1 → publicador                     (publicar en redes/Marketplace)
--   nivel 2 → publicador,analista            (+ análisis, plan Plus)
--   nivel 3 → inventario,disenador           (+ crear/editar y fotos)
-- ==============================================================================

ALTER TABLE mcp_api_keys
    ADD COLUMN IF NOT EXISTS roles TEXT;

UPDATE mcp_api_keys
SET roles = CASE nivel
                WHEN 1 THEN 'publicador'
                WHEN 2 THEN 'publicador,analista'
                ELSE 'inventario,disenador'
            END
WHERE roles IS NULL OR roles = '';
