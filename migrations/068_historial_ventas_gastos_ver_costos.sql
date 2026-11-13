-- ==============================================================================
-- 068: Historial de ventas y gastos + permiso "ver_costos" por clave MCP.
--
-- Dos cambios independientes que comparten migración por conveniencia:
--
-- 1. El CHECK de dominios de registro_actividad (062) solo permitía
--    'producto'|'cliente'|'cobro'. Las ediciones de VENTAS y GASTOS ahora
--    también se registran (para el dueño Y para el agente IA — atribución
--    "El agente IA editó..."), así que se amplía con 'venta' y 'gasto'.
--    La acción 'editar' ya estaba en el CHECK de 062.
--
-- 2. Columna ver_costos en mcp_api_keys: permiso por CLAVE (independiente del
--    nivel) para que el agente lea costos internos y márgenes. Default FALSE:
--    el costo sigue siendo el dato más íntimo del negocio y solo se comparte
--    si el dueño activa el switch de ESA clave. La red de seguridad no
--    cambia: datos_para_publicar JAMÁS incluye costos (es el canal público).
-- ==============================================================================

-- ── 1. Dominios nuevos del Historial ─────────────────────────────────────────
-- El constraint de 062 no trajo nombre explícito: Postgres le asignó
-- registro_actividad_dominio_check. Drop+recreate idempotente.
ALTER TABLE registro_actividad
    DROP CONSTRAINT IF EXISTS registro_actividad_dominio_check;
ALTER TABLE registro_actividad
    ADD CONSTRAINT registro_actividad_dominio_check
    CHECK (dominio IN ('producto', 'cliente', 'cobro', 'venta', 'gasto'));

-- ── 2. ver_costos por clave MCP ──────────────────────────────────────────────
ALTER TABLE mcp_api_keys
    ADD COLUMN IF NOT EXISTS ver_costos BOOLEAN NOT NULL DEFAULT FALSE;
