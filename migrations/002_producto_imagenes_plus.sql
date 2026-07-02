-- ==============================================================================
-- Migración: Tabla producto_imagenes para plan Plus (multigalería por producto)
-- Fecha: 2026-07-02
-- Autor: Hermes Agent (Nezzura Digital)
--
-- Objetivo:
--   Permitir que tenants con plan "plus" suban hasta 5 imágenes adicionales
--   por producto. La imagen principal sigue en productos.imagen (Cloudinary).
--   Las imágenes extra viven en esta tabla nueva.
--
-- Estructura:
--   - id: SERIAL PK
--   - producto_id: FK a productos(id) ON DELETE CASCADE
--   - tenant_id: UUID (aislamiento multitenant, mismo patrón que el resto)
--   - url: TEXT (URL de Cloudinary, igual que productos.imagen)
--   - orden: INT (1-5, controla el orden del carousel)
--   - created_at: TIMESTAMPTZ
--
-- Seguridad:
--   - RLS activado con policy tenant_id = auth.uid()
--   - Constraint UNIQUE (producto_id, orden) evita duplicados de posición
--   - Constraint CHECK (orden BETWEEN 1 AND 5) limita a máximo 5 imágenes
--   - FK ON DELETE CASCADE: si se borra el producto, se borran sus imágenes
-- ==============================================================================

-- Crear tabla
CREATE TABLE IF NOT EXISTS public.producto_imagenes (
    id          SERIAL PRIMARY KEY,
    producto_id integer NOT NULL,
    tenant_id   uuid,
    url         text NOT NULL,
    orden       integer NOT NULL DEFAULT 1,
    created_at  timestamp with time zone DEFAULT now(),
    CONSTRAINT producto_imagenes_producto_id_fkey
        FOREIGN KEY (producto_id) REFERENCES public.productos(id) ON DELETE CASCADE,
    CONSTRAINT producto_imagenes_orden_check
        CHECK (orden BETWEEN 1 AND 5),
    CONSTRAINT producto_imagenes_orden_unique
        UNIQUE (producto_id, orden)
);

-- Activar RLS
ALTER TABLE public.producto_imagenes ENABLE ROW LEVEL SECURITY;

-- Policy: aislamiento multitenant (mismo patrón que las demás tablas)
DROP POLICY IF EXISTS tenant_isolation ON public.producto_imagenes;
CREATE POLICY tenant_isolation ON public.producto_imagenes
    FOR ALL USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

-- Índices para consultas frecuentes
CREATE INDEX IF NOT EXISTS idx_producto_imagenes_tenant_id
    ON public.producto_imagenes (tenant_id);
CREATE INDEX IF NOT EXISTS idx_producto_imagenes_producto_orden
    ON public.producto_imagenes (producto_id, orden);
