-- =============================================================================
-- Migracion 029: Correccion de seguridad RLS
-- Habilita Row-Level Security en las 4 tablas que estaban sin proteger.
-- Creada el 2026-08-21 como respuesta a alerta critica de Supabase.
-- =============================================================================

-- 1. gastos_categorias (categorias de gasto editables)
ALTER TABLE gastos_categorias ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON gastos_categorias;
CREATE POLICY tenant_isolation ON gastos_categorias
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

-- 2. post_config (configuracion de posts por negocio)
ALTER TABLE post_config ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON post_config;
CREATE POLICY tenant_isolation ON post_config
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

-- 3. producto_recetas (recetas/materiales de productos compuestos)
ALTER TABLE producto_recetas ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON producto_recetas;
CREATE POLICY tenant_isolation ON producto_recetas
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

-- 4. producto_variaciones (variaciones de productos con precios)
ALTER TABLE producto_variaciones ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON producto_variaciones;
CREATE POLICY tenant_isolation ON producto_variaciones
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());
