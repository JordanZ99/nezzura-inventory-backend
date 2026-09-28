-- ==============================================================================
-- 043: Datos de contacto e identidad del negocio (columnas de tenants).
--
-- Campos OPCIONALES que el tenant config desde Personalización > Mi Cuenta >
-- Identidad del Negocio. El cliente los envía directo a Supabase (mismo
-- flujo de empresa/logo, Table no expone nada sensible):
--   - telefono: número visible para clientes (WhatsApp/calls es el mismo valor)
--   - correo:   email de contacto
--   - instagram / facebook / tiktok: redes sociales (nombre de usuario o link)
--   - sitio_web: cualquier link genérico (página, carta PDF, menú, etc.)
--   - maps: ubicación de negocios físicos — acepta Plus Code ("8F26+3F ...")
--     o un link completo de Google Maps; el frontend decide cómo mostrarlo.
-- Todos son NULL por defecto (opcionales), TEXT para tolerar formatos variados.
-- ==============================================================================

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS telefono   TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS correo     TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS instagram  TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS facebook   TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS tiktok     TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS sitio_web  TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS maps       TEXT;
