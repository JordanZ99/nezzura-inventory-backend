-- ==============================================================================
-- Migración: Productos por fila en móvil (configurable)
-- Fecha: 2026-08-10
--
-- Objetivo:
--   Permitir al tenant elegir cuántos productos se muestran por fila en
--   dispositivos móviles (1 o 2), tanto en las filas compactas del carrusel
--   como en el grid expandido ("Ver todos") y el grid plano.
--   En escritorio el catálogo mantiene 4 por fila (sin cambios).
--
--     - columnas_movil = 1 → un producto grande por fila en móvil
--     - columnas_movil = 2 (default) → dos productos por fila en móvil
--
-- Idempotente: ADD COLUMN IF NOT EXISTS no lanza error si la columna ya existe.
-- También se auto-aplica en backend/database/conexion.py (inicializar_db).
-- ==============================================================================

ALTER TABLE catalogo_config
    ADD COLUMN IF NOT EXISTS columnas_movil integer DEFAULT 2;

-- Constraint CHECK: solo permite 1 o 2
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'catalogo_config_columnas_movil_check'
    ) THEN
        EXECUTE 'ALTER TABLE catalogo_config ADD CONSTRAINT catalogo_config_columnas_movil_check
                 CHECK (columnas_movil = ANY (ARRAY[1, 2]))';
    END IF;
END $$;
