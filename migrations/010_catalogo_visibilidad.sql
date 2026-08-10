-- ==============================================================================
-- Migración: Visibilidad del catálogo (ocultar stock / ocultar agotados)
-- Fecha: 2026-08-10
--
-- Objetivo:
--   Dar al tenant más control sobre la visibilidad del catálogo público:
--
--     - ocultar_agotados = true  → los productos sin stock desaparecen del catálogo
--     - ocultar_agotados = false (default) → se muestran (con su stock si está visible)
--
--   Además, se asegura que mostrar_stock por defecto sea TRUE (el stock se ve):
--   se normalizan los NULLs existentes y se fija el DEFAULT de la columna,
--   ya que el nuevo toggle del panel "Ocultar stock" es el inverso de mostrar_stock.
--
-- Idempotente: ADD COLUMN IF NOT EXISTS y UPDATE no lanzan error si ya existen.
-- También se auto-aplica en backend/database/conexion.py (inicializar_db).
-- ==============================================================================

ALTER TABLE catalogo_config
    ADD COLUMN IF NOT EXISTS ocultar_agotados boolean DEFAULT false;

-- El stock se muestra por defecto: normalizar registros antiguos con NULL
UPDATE catalogo_config SET mostrar_stock = true WHERE mostrar_stock IS NULL;

-- Fijar el default de la columna para los registros nuevos
ALTER TABLE catalogo_config ALTER COLUMN mostrar_stock SET DEFAULT true;
