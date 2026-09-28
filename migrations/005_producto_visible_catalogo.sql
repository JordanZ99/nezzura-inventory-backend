-- ==============================================================================
-- Migración: Columna visible_en_catalogo para productos en catálogo público
-- Fecha: 2026-07-30
--
-- Objetivo:
--   Permitir que cada producto decida si se muestra en el catálogo público,
--   independientemente de su Estado (Activo/Inactivo).
--
--   - Estado = 'Activo'/'Inactivo'   → controla el ciclo operativo (ventas, stock)
--   - visible_en_catalogo = true/false → controla SOLO la visibilidad pública
--
--   Por defecto, todos los productos son visibles (true).
--
-- Idempotente: ADD COLUMN IF NOT EXISTS no lanza error si la columna ya existe.
-- ==============================================================================

ALTER TABLE productos
    ADD COLUMN IF NOT EXISTS visible_en_catalogo boolean DEFAULT true;
