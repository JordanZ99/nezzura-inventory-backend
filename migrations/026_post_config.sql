-- ==============================================================================
-- 026_post_config.sql
-- Fase 1 de Posts Automáticos: tarjetas de producto pre-renderizadas.
--
-- 1. post_config: defaults del NEGOCIO para las tarjetas (una fila por tenant).
--    - template_default: 'marco' | 'overlay' | 'tarjeta'  (Fase 1 solo 'marco')
--    - color: key de la paleta ('default' | 'midnightBlack' | 'strawberry' | 'cozyYellow' | 'white')
--    - font: 'moderna' | 'elegante' | 'redondeada'
--    - posicion: 'arriba' | 'abajo'  (solo relevante para la plantilla Overlay, Fase 2)
--    - mostrar: JSONB {nombre, precio, negocio} — qué líneas se dibujan en la tarjeta
--
-- 2. productos.post_override: JSONB nullable. NULL = el producto usa los defaults
--    del negocio. Si no, es un dict parcial tipo:
--      {"template":"marco","color":"strawberry","posicion":"abajo"}
--    (las claves ausentes se heredan de post_config).
--
-- Idempotente: CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS no
-- lanzan error si ya existen. También se auto-aplica en
-- backend/database/conexion.py (inicializar_db → _ejecutar_migraciones).
-- ==============================================================================

CREATE TABLE IF NOT EXISTS post_config (
    tenant_id        UUID PRIMARY KEY,
    template_default TEXT NOT NULL DEFAULT 'marco',
    color            TEXT NOT NULL DEFAULT 'default',
    font             TEXT NOT NULL DEFAULT 'moderna',
    posicion         TEXT NOT NULL DEFAULT 'abajo',
    mostrar          JSONB NOT NULL DEFAULT '{"nombre": true, "precio": true, "negocio": true}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Override por producto (nullable): NULL = usa los defaults del negocio
ALTER TABLE productos ADD COLUMN IF NOT EXISTS post_override JSONB;

-- Constraints CHECK (valores válidos de cada campo), con guard IF NOT EXISTS
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'post_config_template_check') THEN
        EXECUTE 'ALTER TABLE post_config ADD CONSTRAINT post_config_template_check
                 CHECK (template_default = ANY (ARRAY[''marco'', ''overlay'', ''tarjeta'']))';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'post_config_color_check') THEN
        EXECUTE 'ALTER TABLE post_config ADD CONSTRAINT post_config_color_check
                 CHECK (color = ANY (ARRAY[''default'', ''midnightBlack'', ''strawberry'', ''cozyYellow'', ''white'']))';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'post_config_font_check') THEN
        EXECUTE 'ALTER TABLE post_config ADD CONSTRAINT post_config_font_check
                 CHECK (font = ANY (ARRAY[''moderna'', ''elegante'', ''redondeada'']))';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'post_config_posicion_check') THEN
        EXECUTE 'ALTER TABLE post_config ADD CONSTRAINT post_config_posicion_check
                 CHECK (posicion = ANY (ARRAY[''arriba'', ''abajo'']))';
    END IF;
END $$;
