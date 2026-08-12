-- ==============================================================================
-- Migración: Relación global de las imágenes de producto (1:1 o 4:5)
-- Fecha: 2026-08-12
--
-- Objetivo:
--   Permitir al tenant elegir la relación de aspecto de TODAS las fotos de
--   producto de forma global (catálogo, punto de venta, gestor y crops):
--
--     - relacion_imagen = '1:1' (default) → cuadrada, como hasta ahora
--     - relacion_imagen = '4:5'           → vertical, estilo Instagram
--
--   Solo afecta a cómo se recortan y muestran las imágenes; no cambia el
--   almacenamiento (los archivos se guardan con la relación elegida en el
--   crop y se muestran en contenedores con esa misma relación).
--
-- Idempotente: ADD COLUMN IF NOT EXISTS no lanza error si la columna ya existe.
-- También se auto-aplica en backend/database/conexion.py (inicializar_db).
-- ==============================================================================

ALTER TABLE catalogo_config
    ADD COLUMN IF NOT EXISTS relacion_imagen text DEFAULT '1:1';

-- Constraint CHECK: solo permite '1:1' o '4:5'
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'catalogo_config_relacion_imagen_check'
    ) THEN
        EXECUTE 'ALTER TABLE catalogo_config ADD CONSTRAINT catalogo_config_relacion_imagen_check
                 CHECK (relacion_imagen = ANY (ARRAY[''1:1'', ''4:5'']))';
    END IF;
END $$;
