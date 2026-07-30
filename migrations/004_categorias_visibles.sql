-- ==============================================================================
-- Migración: Columna visible_en_catalogo para filtrar categorías en catálogo público
-- Fecha: 2026-07-30
--
-- Objetivo:
--   Permitir que cada tenant decida qué categorías de productos se muestran
--   en su catálogo público. Por defecto, todas las categorías son visibles.
--
--   El filtro se aplica en la subquery de categorías del endpoint público
--   (publico.py) para que los productos con categorías ocultas simplemente
--   no muestren esas categorías.
--
-- Idempotente: ADD COLUMN IF NOT EXISTS no lanza error si la columna ya existe.
-- ==============================================================================

ALTER TABLE categorias
    ADD COLUMN IF NOT EXISTS visible_en_catalogo boolean DEFAULT true;
