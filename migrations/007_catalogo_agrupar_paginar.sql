-- ==============================================================================
-- Migración: Agrupación por categoría configurable en el catálogo público
-- Fecha: 2026-08-10
--
-- Objetivo:
--   Permitir al tenant separar los productos por secciones según su categoría
--   (encabezados tipo "Peluches", "Bolsas", ...) en el catálogo público.
--     - agrupar_por_categoria = true  → productos agrupados por secciones
--     - agrupar_por_categoria = false (default) → lista/grid plano con paginación
--
-- Idempotente: ADD COLUMN IF NOT EXISTS no lanza error si la columna ya existe.
-- También se auto-aplica en backend/database/conexion.py (inicializar_db).
-- ==============================================================================

ALTER TABLE catalogo_config
    ADD COLUMN IF NOT EXISTS agrupar_por_categoria boolean;  -- NULL = no configurado (fallback por template)
