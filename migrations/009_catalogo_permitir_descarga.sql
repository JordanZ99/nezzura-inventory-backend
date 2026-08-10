-- ==============================================================================
-- Migración: Permitir descargar fotos del catálogo (configurable)
-- Fecha: 2026-08-10
--
-- Objetivo:
--   Permitir al tenant decidir si sus clientes pueden descargar las fotos
--   de los productos desde el catálogo público.
--
--     - permitir_descarga = true  → el modal de producto muestra el botón de descarga
--     - permitir_descarga = false (default) → sin botón de descarga
--
-- Idempotente: ADD COLUMN IF NOT EXISTS no lanza error si la columna ya existe.
-- También se auto-aplica en backend/database/conexion.py (inicializar_db).
-- ==============================================================================

ALTER TABLE catalogo_config
    ADD COLUMN IF NOT EXISTS permitir_descarga boolean DEFAULT false;
