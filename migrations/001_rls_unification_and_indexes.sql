-- ==============================================================================
-- Migración: Unificación de policies RLS + índices de rendimiento
-- Fecha: 2026-07-02
-- Autor: Hermes Agent (Nezzura Digital)
--
-- Objetivo:
--   1. Simplificar 4 policies de RLS que usaban subquery innecesaria
--   2. Crear índices B-tree en tenant_id para todas las tablas de negocio
--   3. Crear índices compuestos para los patrones de consulta más frecuentes
--
-- Seguridad:
--   - Esta migración NO altera datos, NO cambia tipos de columnas,
--     NO modifica estructura de tablas.
--   - Solo modifica policies (DROP + CREATE) y crea índices (IF NOT EXISTS).
--   - Idempotente: se puede ejecutar múltiples veces sin efecto adverso.
--
-- Contexto:
--   Las policies de productos/lotes/ventas/gastos usaban el patrón:
--     tenant_id = (SELECT tenants.id FROM tenants WHERE tenants.id = auth.uid())
--   Como tenant_id guarda exactamente el UUID de auth.uid(), la subquery es
--   redundante. La comparación directa tenant_id = auth.uid() es equivalente
--   pero más rápida porque evita una subquery por fila evaluada.
-- ==============================================================================

-- ============================================================
-- 1. UNIFICAR POLICIES DE RLS
-- ============================================================

-- productos
DROP POLICY IF EXISTS tenant_isolation ON public.productos;
CREATE POLICY tenant_isolation ON public.productos
    FOR ALL USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

-- lotes
DROP POLICY IF EXISTS tenant_isolation ON public.lotes;
CREATE POLICY tenant_isolation ON public.lotes
    FOR ALL USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

-- ventas
DROP POLICY IF EXISTS tenant_isolation ON public.ventas;
CREATE POLICY tenant_isolation ON public.ventas
    FOR ALL USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

-- gastos
DROP POLICY IF EXISTS tenant_isolation ON public.gastos;
CREATE POLICY tenant_isolation ON public.gastos
    FOR ALL USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

-- ============================================================
-- 2. ÍNDICES EN tenant_id (una columna)
-- ============================================================
-- Todas las consultas del backend filtran por tenant_id (aislamiento multitenant).
-- Sin estos índices, PostgreSQL hace seq scan completo en cada query.

CREATE INDEX IF NOT EXISTS idx_productos_tenant_id       ON public.productos          (tenant_id);
CREATE INDEX IF NOT EXISTS idx_lotes_tenant_id           ON public.lotes              (tenant_id);
CREATE INDEX IF NOT EXISTS idx_ventas_tenant_id          ON public.ventas             (tenant_id);
CREATE INDEX IF NOT EXISTS idx_gastos_tenant_id          ON public.gastos             (tenant_id);
CREATE INDEX IF NOT EXISTS idx_gastos_programados_tenant ON public.gastos_programados (tenant_id);
CREATE INDEX IF NOT EXISTS idx_categorias_tenant_id      ON public.categorias         (tenant_id);

-- ============================================================
-- 3. ÍNDICES COMPUESTOS (patrones de consulta frecuentes)
-- ============================================================

-- Lotes: el algoritmo PEPS consulta lotes activos de un producto por tenant
-- Patrón: WHERE Producto=%s AND Estado='Activo' AND tenant_id=%s ORDER BY Fecha_Entrada ASC
CREATE INDEX IF NOT EXISTS idx_lotes_tenant_producto_estado
    ON public.lotes (tenant_id, producto, estado);

-- Ventas: dashboard y listado consultan ventas por tenant ordenadas por fecha desc
-- Patrón: WHERE tenant_id=%s ORDER BY Fecha DESC LIMIT %s
CREATE INDEX IF NOT EXISTS idx_ventas_tenant_fecha_desc
    ON public.ventas (tenant_id, fecha DESC);

-- Productos: validación de duplicados al crear producto
-- Patrón: WHERE Producto=%s AND tenant_id=%s
CREATE INDEX IF NOT EXISTS idx_productos_tenant_producto
    ON public.productos (tenant_id, producto);

-- Gastos: listado de gastos por tenant ordenado por fecha desc
-- Patrón: WHERE tenant_id=%s ORDER BY Fecha DESC
CREATE INDEX IF NOT EXISTS idx_gastos_tenant_fecha_desc
    ON public.gastos (tenant_id, fecha DESC);

-- Gastos programados: motor de verificación busca reglas vencidas por tenant
-- Patrón: WHERE tenant_id=%s ORDER BY proxima_fecha ASC
CREATE INDEX IF NOT EXISTS idx_gastos_prog_tenant_proxima
    ON public.gastos_programados (tenant_id, proxima_fecha);
