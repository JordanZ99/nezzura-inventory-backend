-- ==============================================================================
-- backend/tests/e2e/mcp_harness.sql
-- Harness mínimo y FIEL al esquema real para probar el conector IA (MCP)
-- contra un PostgreSQL vanilla (docker). Crea:
--   1. Un auth.uid() fake (las políticas RLS del proyecto usan auth.uid();
--      el backend conecta como superuser, que igual las ignora — como en Supabase)
--   2. Las tablas base con las columnas exactas que tocan las queries de
--      database/mcp_catalogo.py (copiadas de inicializar_db + migraciones
--      002/004/005/016/017/018/021/022/023/042/046)
--   3. schema_migrations pre-sembrada con TODAS las migraciones MENOS la 065,
--      para que el arranque del backend aplique únicamente la migración nueva.
-- ==============================================================================

-- ── Supabase-like: auth.uid() ────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS auth;
CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid
LANGUAGE sql STABLE AS $$ SELECT NULL::uuid $$;
CREATE OR REPLACE FUNCTION auth.role() RETURNS text
LANGUAGE sql STABLE AS $$ SELECT 'authenticated'::text $$;

-- ── tenants (referenciada por 065/066 y por el RLS) ──────────────────────────
CREATE TABLE IF NOT EXISTS tenants (
    id            uuid PRIMARY KEY,
    empresa       text,
    zona_horaria  text NOT NULL DEFAULT 'America/Cancun',
    plan          text,
    dias_cerrados jsonb DEFAULT '[]'::jsonb
);

-- ── Tablas core (idénticas a inicializar_db) ─────────────────────────────────
CREATE TABLE IF NOT EXISTS productos (
    id           SERIAL PRIMARY KEY,
    Producto     TEXT NOT NULL,
    Descripcion  TEXT,
    Imagen       TEXT DEFAULT 'No hay foto',
    Estado       TEXT DEFAULT 'Activo',
    tenant_id    UUID
);
CREATE TABLE IF NOT EXISTS lotes (
    id            SERIAL PRIMARY KEY,
    ID_Lote       TEXT NOT NULL UNIQUE,
    Producto      TEXT NOT NULL,
    Costo         REAL DEFAULT 0,
    Precio_Venta  REAL DEFAULT 0,
    Stock_Lote    REAL DEFAULT 0,
    Fecha_Entrada TEXT,
    Estado        TEXT DEFAULT 'Activo',
    tenant_id     UUID
);
CREATE TABLE IF NOT EXISTS ventas (
    id              SERIAL PRIMARY KEY,
    n_ticket        INTEGER,
    Fecha           TEXT,
    Producto        TEXT,
    Cantidad        REAL,
    Precio_Lista    REAL,
    Precio_Real     REAL,
    Costo_Unitario  REAL,
    Total_Venta     REAL,
    Ganancia_Bruta  REAL,
    Estado          TEXT DEFAULT 'Activo',
    ID_Lote         TEXT,
    tenant_id       UUID
);
CREATE TABLE IF NOT EXISTS gastos (
    id          SERIAL PRIMARY KEY,
    Fecha       TEXT,
    Categoria   TEXT,
    Descripcion TEXT,
    Monto       REAL,
    tenant_id   UUID
);
CREATE TABLE IF NOT EXISTS categorias (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL,
    nombre      TEXT NOT NULL,
    slug        TEXT NOT NULL,
    visible_en_catalogo BOOLEAN DEFAULT true,
    orden       INTEGER,
    UNIQUE(tenant_id, nombre)
);
CREATE TABLE IF NOT EXISTS producto_categorias (
    producto_id   INTEGER NOT NULL REFERENCES productos(id) ON DELETE CASCADE,
    categoria_id  UUID    NOT NULL REFERENCES categorias(id) ON DELETE CASCADE,
    PRIMARY KEY (producto_id, categoria_id)
);
CREATE TABLE IF NOT EXISTS gastos_categorias (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL,
    nombre      TEXT NOT NULL,
    UNIQUE(tenant_id, nombre)
);

-- ── Columnas de migraciones que tocan mis queries ────────────────────────────
ALTER TABLE productos ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE productos ADD COLUMN IF NOT EXISTS codigo_interno TEXT;
ALTER TABLE productos ADD COLUMN IF NOT EXISTS codigo_barras TEXT;
ALTER TABLE productos ADD COLUMN IF NOT EXISTS ubicacion TEXT;
ALTER TABLE productos ADD COLUMN IF NOT EXISTS sufijo_precio TEXT DEFAULT '';
ALTER TABLE productos ADD COLUMN IF NOT EXISTS fraccionable BOOLEAN DEFAULT false;
ALTER TABLE productos ADD COLUMN IF NOT EXISTS tamano_cm NUMERIC(8,2);
ALTER TABLE productos ADD COLUMN IF NOT EXISTS tipo_producto TEXT DEFAULT 'stock';
ALTER TABLE productos ADD COLUMN IF NOT EXISTS costo_servicio REAL DEFAULT 0;
ALTER TABLE productos ADD COLUMN IF NOT EXISTS precio_servicio REAL DEFAULT 0;
ALTER TABLE productos ADD COLUMN IF NOT EXISTS visible_en_catalogo BOOLEAN DEFAULT true;
ALTER TABLE productos ADD COLUMN IF NOT EXISTS stock_por_variacion BOOLEAN DEFAULT false;
-- Columna que usa el upsert de productos (_upsert_producto)
ALTER TABLE productos ADD COLUMN IF NOT EXISTS etiqueta TEXT DEFAULT '';
-- Columnas que usa el Análisis Inteligente (nivel 2)
ALTER TABLE productos ADD COLUMN IF NOT EXISTS es_generico BOOLEAN DEFAULT false;

-- Constraint del upsert de productos (migración 050 en la DB real)
ALTER TABLE productos DROP CONSTRAINT IF EXISTS productos_tenant_producto_unico;
ALTER TABLE productos ADD CONSTRAINT productos_tenant_producto_unico UNIQUE (tenant_id, producto);

ALTER TABLE lotes ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE lotes ADD COLUMN IF NOT EXISTS producto_id INTEGER;
ALTER TABLE lotes ADD COLUMN IF NOT EXISTS variacion_id INTEGER;
-- Columnas que usa la creación de lotes (_crear_lote_inicial)
ALTER TABLE lotes ADD COLUMN IF NOT EXISTS fecha_entrada_ts TIMESTAMPTZ;
ALTER TABLE lotes ADD COLUMN IF NOT EXISTS etiqueta TEXT DEFAULT '';

-- Columnas del Análisis Inteligente sobre ventas/gastos
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS fecha_ts TIMESTAMPTZ;
ALTER TABLE gastos ADD COLUMN IF NOT EXISTS fecha_negocio DATE;
ALTER TABLE gastos ADD COLUMN IF NOT EXISTS estado TEXT;

ALTER TABLE ventas ADD COLUMN IF NOT EXISTS Estado TEXT DEFAULT 'Activo';
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS ID_Lote TEXT;
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS n_ticket integer;
-- Columnas que usa actualizar_venta (ediciones por el agente IA / dueño)
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS producto_id INTEGER;
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS tipo_producto TEXT DEFAULT 'stock';
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS variacion TEXT;
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS orden_id UUID;
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS consumo JSONB;

-- ── registro_actividad (mig 062, con su CHECK de dominios ORIGINAL) ─────────
-- La migración 068 lo ampliará con 'venta' y 'gasto' al arrancar el backend:
-- así el test verifica el drop+recreate del constraint y los renglones nuevos.
CREATE TABLE IF NOT EXISTS registro_actividad (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    fecha      TIMESTAMPTZ NOT NULL DEFAULT now(),
    dominio    TEXT NOT NULL CHECK (dominio IN ('producto', 'cliente', 'cobro')),
    entidad    TEXT NOT NULL,
    accion     TEXT NOT NULL CHECK (accion IN (
                   'crear', 'editar', 'eliminar', 'abonar',
                   'quitar_abono', 'generar', 'ajustar_puntos')),
    entidad_id TEXT,
    resumen    TEXT NOT NULL,
    detalle    JSONB
);

-- ── producto_imagenes (mig 002, tenants Plus) ────────────────────────────────
CREATE TABLE IF NOT EXISTS producto_imagenes (
    id          SERIAL PRIMARY KEY,
    producto_id integer NOT NULL,
    tenant_id   uuid,
    url         text NOT NULL,
    orden       integer NOT NULL DEFAULT 1,
    created_at  timestamp with time zone DEFAULT now(),
    CONSTRAINT producto_imagenes_producto_id_fkey
        FOREIGN KEY (producto_id) REFERENCES productos(id) ON DELETE CASCADE,
    CONSTRAINT producto_imagenes_orden_check CHECK (orden BETWEEN 1 AND 5),
    CONSTRAINT producto_imagenes_orden_unique UNIQUE (producto_id, orden)
);

-- ── producto_variaciones (mig 018 + foto de 021) ─────────────────────────────
CREATE TABLE IF NOT EXISTS producto_variaciones (
    id          BIGSERIAL PRIMARY KEY,
    producto_id INTEGER NOT NULL REFERENCES productos(id) ON DELETE CASCADE,
    nombre      TEXT    NOT NULL,
    precio      REAL    NOT NULL DEFAULT 0,
    tenant_id   UUID    NOT NULL,
    foto        TEXT,
    UNIQUE(producto_id, nombre)
);

-- ── schema_migrations pre-sembrada: SOLO se aplicará la 065 en el arranque ──
CREATE TABLE IF NOT EXISTS schema_migrations (
    nombre      TEXT PRIMARY KEY,
    aplicada_en TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO schema_migrations (nombre) VALUES
    ('001_rls_unification_and_indexes.sql'),
    ('002_producto_imagenes_plus.sql'),
    ('003_catalogo_templates.sql'),
    ('004_categorias_visibles.sql'),
    ('005_producto_visible_catalogo.sql'),
    ('006_catalogo_hero_anuncios.sql'),
    ('007_catalogo_agrupar_paginar.sql'),
    ('008_catalogo_columnas_movil.sql'),
    ('009_catalogo_permitir_descarga.sql'),
    ('010_catalogo_visibilidad.sql'),
    ('011_catalogo_banner_texto.sql'),
    ('012_catalogo_banner_movil.sql'),
    ('013_catalogo_banner_logo.sql'),
    ('014_lote_etiqueta.sql'),
    ('015_tenant_modo_precio_sugerido.sql'),
    ('016_producto_sufijo_precio.sql'),
    ('017_producto_servicio.sql'),
    ('018_producto_variaciones.sql'),
    ('019_producto_compuesto.sql'),
    ('020_receta_variaciones.sql'),
    ('021_stock_decimal_foto_variacion.sql'),
    ('022_stock_por_variacion.sql'),
    ('023_producto_fraccionable.sql'),
    ('024_catalogo_relacion_imagen.sql'),
    ('025_eliminar_stock_por_variacion.sql'),
    ('026_post_config.sql'),
    ('027_post_config_cta.sql'),
    ('028_post_config_colores.sql'),
    ('029_rls_fix_vulnerables.sql'),
    ('030_dinero_numeric_relaciones_producto.sql'),
    ('031_fechas_timestamptz_negocio.sql'),
    ('032_ordenes_de_venta.sql'),
    ('033_cobro_tickets.sql'),
    ('034_terminales_turnos.sql'),
    ('035_giro_modulos.sql'),
    ('036_venta_libre.sql'),
    ('037_mesas.sql'),
    ('038_clientes.sql'),
    ('039_puntos.sql'),
    ('040_inventario_movimientos.sql'),
    ('041_conteo_inventario.sql'),
    ('042_categorias_orden.sql'),
    ('043_tenant_contacto.sql'),
    ('044_catalogo_fuente.sql'),
    ('045_catalogo_fondo.sql'),
    ('046_producto_tamano_cm.sql'),
    ('047_catalogo_hero.sql'),
    ('048_catalogo_hero_check.sql'),
    ('049_catalogo_hero_layout.sql'),
    ('050_integridad_multitenant.sql'),
    ('051_dias_cerrados.sql'),
    ('052_renombrar_template_cards.sql'),
    ('053_agregar_template_menu.sql'),
    ('054_crm_etiquetas.sql'),
    ('055_crm_interacciones.sql'),
    ('056_conteo_renglones_manuales.sql'),
    ('057_conteo_alcance.sql'),
    ('058_licencias_dispositivos.sql'),
    ('059_catalogo_banner_redes.sql'),
    ('060_venta_libre_categoria.sql'),
    ('061_cobros_servicios.sql'),
    ('062_registro_actividad.sql'),
    ('063_cobros_frecuencia.sql'),
    ('064_cuotas_meta.sql')
ON CONFLICT (nombre) DO NOTHING;

-- ══════════════════════════════════════════════════════════════════════════════
-- Extensión Fase 2 (compuestos): el flujo RESTAURANTE del agente también pasa
-- por el motor del POS (cobrar_carrito), así que el harness necesita las tablas
-- de recetas (019/020), órdenes/tickets (032/033), terminales/turnos (034) y el
-- ledger de inventario (040) — todas fieles a sus migraciones.
-- ══════════════════════════════════════════════════════════════════════════════

-- ── producto_recetas (mig 019 + variaciones de 020) ──────────────────────────
CREATE TABLE IF NOT EXISTS producto_recetas (
    id          BIGSERIAL PRIMARY KEY,
    producto_id INTEGER NOT NULL REFERENCES productos(id) ON DELETE CASCADE,
    material_id INTEGER NOT NULL REFERENCES productos(id) ON DELETE CASCADE,
    cantidad    REAL    NOT NULL DEFAULT 1,
    tenant_id   UUID    NOT NULL,
    UNIQUE(producto_id, material_id)
);
CREATE INDEX IF NOT EXISTS idx_producto_recetas_producto
    ON producto_recetas (producto_id, tenant_id);
ALTER TABLE producto_recetas ADD COLUMN IF NOT EXISTS variacion_id INTEGER
    REFERENCES producto_variaciones(id) ON DELETE CASCADE;
ALTER TABLE producto_recetas DROP CONSTRAINT IF EXISTS producto_recetas_producto_id_material_id_key;
DROP INDEX IF EXISTS uq_producto_recetas_base;
CREATE UNIQUE INDEX uq_producto_recetas_base
    ON producto_recetas (producto_id, material_id) WHERE variacion_id IS NULL;
DROP INDEX IF EXISTS uq_producto_recetas_variacion;
CREATE UNIQUE INDEX uq_producto_recetas_variacion
    ON producto_recetas (producto_id, variacion_id, material_id) WHERE variacion_id IS NOT NULL;

-- ── Columnas de venta que insertar_venta llena (060 y ventas libres) ─────────
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS descripcion TEXT;
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS categoria TEXT;

-- ── Órdenes de venta / tickets (migs 032 + 033) ──────────────────────────────
CREATE TABLE IF NOT EXISTS ordenes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    n_ticket        INTEGER NOT NULL,
    fecha_ts        TIMESTAMPTZ NOT NULL,
    total           NUMERIC(14,2) NOT NULL DEFAULT 0,
    ganancia        NUMERIC(14,2) NOT NULL DEFAULT 0,
    cantidad_items  REAL NOT NULL DEFAULT 0,
    estado          TEXT NOT NULL DEFAULT 'Activa',
    metodo_pago     TEXT,
    pagos           JSONB,
    propina         NUMERIC(14,2) NOT NULL DEFAULT 0,
    monto_recibido  NUMERIC(14,2),
    cambio          NUMERIC(14,2),
    comision_total  NUMERIC(14,2) NOT NULL DEFAULT 0
);
ALTER TABLE ordenes ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON ordenes;
CREATE POLICY tenant_isolation ON ordenes
    FOR ALL USING (tenant_id = auth.uid()) WITH CHECK (tenant_id = auth.uid());
CREATE INDEX IF NOT EXISTS idx_ordenes_tenant_fecha_ts ON ordenes (tenant_id, fecha_ts DESC);
CREATE INDEX IF NOT EXISTS idx_ventas_orden ON ventas (orden_id);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ordenes_tenant_ticket_key') THEN
        ALTER TABLE ordenes ADD CONSTRAINT ordenes_tenant_ticket_key UNIQUE (tenant_id, n_ticket);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ventas_orden_id_fkey') THEN
        ALTER TABLE ventas ADD CONSTRAINT ventas_orden_id_fkey
            FOREIGN KEY (orden_id) REFERENCES ordenes(id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION generar_numero_ticket_orden() RETURNS trigger AS $$
BEGIN
    IF NEW.n_ticket IS NOT NULL THEN
        RETURN NEW;
    END IF;
    PERFORM pg_advisory_xact_lock(770001, hashtext(NEW.tenant_id::text));
    SELECT COALESCE(MAX(n_ticket), 0) + 1
    INTO NEW.n_ticket
    FROM ordenes
    WHERE tenant_id = NEW.tenant_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trigger_folio_ordenes ON ordenes;
CREATE TRIGGER trigger_folio_ordenes
    BEFORE INSERT ON ordenes
    FOR EACH ROW EXECUTE FUNCTION generar_numero_ticket_orden();

-- ── Terminales y turnos (mig 034) ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS terminales (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    nombre               TEXT NOT NULL,
    banco                TEXT,
    comision_debito_pct  REAL NOT NULL DEFAULT 0,
    comision_credito_pct REAL NOT NULL DEFAULT 0,
    comision_fija        REAL NOT NULL DEFAULT 0,
    activo               BOOLEAN NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS turnos (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    estado            TEXT NOT NULL DEFAULT 'Abierto',
    abierta_en        TIMESTAMPTZ NOT NULL,
    cerrada_en        TIMESTAMPTZ,
    monto_apertura    NUMERIC(14,2) NOT NULL DEFAULT 0,
    efectivo_esperado NUMERIC(14,2),
    efectivo_contado  NUMERIC(14,2),
    diferencia        NUMERIC(14,2),
    notas             TEXT
);
ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS turno_id UUID REFERENCES turnos(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_ordenes_turno ON ordenes (turno_id);

-- ── Mesas (mig 037): cobrar_carrito agrega mesa_id/mesa_nombre a la orden ────
CREATE TABLE IF NOT EXISTS mesas (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    nombre     TEXT NOT NULL,
    capacidad  INTEGER,
    orden      INTEGER NOT NULL DEFAULT 0,
    estado     TEXT NOT NULL DEFAULT 'Libre',
    abierta_en TIMESTAMPTZ,
    creada_en  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, nombre)
);
CREATE TABLE IF NOT EXISTS mesa_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    mesa_id         UUID NOT NULL REFERENCES mesas(id) ON DELETE CASCADE,
    producto        TEXT NOT NULL,
    descripcion     TEXT,
    variacion       TEXT,
    cantidad        NUMERIC(12,3) NOT NULL DEFAULT 1,
    precio_unitario NUMERIC(14,2) NOT NULL DEFAULT 0,
    costo           NUMERIC(14,2),
    creado_en       TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS mesa_id UUID REFERENCES mesas(id) ON DELETE SET NULL;
ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS mesa_nombre TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS gasto_comision_automatico BOOLEAN NOT NULL DEFAULT false;

-- ── Ledger de inventario (mig 040) ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS inventario_movimientos (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    producto_id      INTEGER,
    producto         TEXT NOT NULL,
    variacion        TEXT,
    id_lote          TEXT,
    tipo             TEXT NOT NULL CHECK (tipo IN ('entrada', 'salida', 'ajuste')),
    origen           TEXT NOT NULL CHECK (origen IN ('venta', 'restock', 'ajuste_manual', 'edicion_venta', 'anulacion', 'baja_lote')),
    cantidad         NUMERIC(14,3) NOT NULL,
    stock_resultante NUMERIC(14,3),
    referencia_id    TEXT,
    concepto         TEXT,
    fecha            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_movimientos_tenant_fecha
    ON inventario_movimientos (tenant_id, fecha DESC);
CREATE OR REPLACE FUNCTION prohibir_mutar_movimientos() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'inventario_movimientos es append-only: no se permite %', TG_OP;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trigger_movimientos_append_only ON inventario_movimientos;
CREATE TRIGGER trigger_movimientos_append_only
    BEFORE UPDATE OR DELETE ON inventario_movimientos
    FOR EACH ROW EXECUTE FUNCTION prohibir_mutar_movimientos();

-- ── Cartera y planes (migs 038/039/061/063/075/077): las migraciones 075+ las
--    alteran al arrancar el backend, así que el harness las necesita. ──────────
CREATE TABLE IF NOT EXISTS clientes (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    nombre         TEXT NOT NULL,
    email          TEXT,
    telefono       TEXT,
    pin_hash       TEXT,
    notas          TEXT,
    activo         BOOLEAN NOT NULL DEFAULT true,
    fecha_registro TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS cliente_id UUID REFERENCES clientes(id) ON DELETE SET NULL;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS clientes_activos BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS cliente_campos JSONB NOT NULL DEFAULT
    '{"email":{"activo":true,"requerido":true},"telefono":{"activo":true,"requerido":true},"pin":{"activo":false,"requerido":false}}'::jsonb;

CREATE TABLE IF NOT EXISTS puntos_movimientos (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    cliente_id      UUID NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    orden_id        UUID REFERENCES ordenes(id) ON DELETE SET NULL,
    tipo            TEXT NOT NULL CHECK (tipo IN ('ganados', 'canjeados', 'ajuste')),
    puntos          INTEGER NOT NULL,
    valor_monetario NUMERIC(14,2),
    concepto        TEXT,
    fecha           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_puntos_cliente ON puntos_movimientos (tenant_id, cliente_id, fecha);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS puntos_activos BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS puntos_valor_punto NUMERIC(14,4) NOT NULL DEFAULT 1.0;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS puntos_modo TEXT NOT NULL DEFAULT 'por_gasto';
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS puntos_gasto_monto NUMERIC(14,2) NOT NULL DEFAULT 10;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS puntos_gasto_pts INTEGER NOT NULL DEFAULT 1;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS puntos_fijos INTEGER;

CREATE TABLE IF NOT EXISTS cuotas_clientes (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    cliente_id  UUID NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    concepto    TEXT NOT NULL,
    monto       NUMERIC(12,2) NOT NULL CHECK (monto > 0),
    dia_cobro   INT NOT NULL DEFAULT 1 CHECK (dia_cobro BETWEEN 1 AND 28),
    activo      BOOLEAN NOT NULL DEFAULT true,
    notas       TEXT,
    creado_en   TIMESTAMPTZ NOT NULL DEFAULT now(),
    frecuencia  TEXT NOT NULL DEFAULT 'mensual',
    dia_semana  SMALLINT,
    monto_meta  NUMERIC(12,2)
);
ALTER TABLE cuotas_clientes ADD COLUMN IF NOT EXISTS monto_meta NUMERIC(12,2);

CREATE TABLE IF NOT EXISTS cobros (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    cliente_id  UUID NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    cuota_id    UUID REFERENCES cuotas_clientes(id) ON DELETE SET NULL,
    origen      TEXT NOT NULL DEFAULT 'extra' CHECK (origen IN ('cuota', 'extra')),
    concepto    TEXT NOT NULL,
    periodo     TEXT CHECK (periodo IS NULL OR periodo ~ '^\d{4}-\d{2}$'),
    monto       NUMERIC(12,2) NOT NULL CHECK (monto > 0),
    vencimiento DATE,
    programa    JSONB NOT NULL DEFAULT '[]'::jsonb,
    notas       TEXT,
    pagos       JSONB NOT NULL DEFAULT '[]'::jsonb,
    creado_en   TIMESTAMPTZ NOT NULL DEFAULT now()
);
DROP INDEX IF EXISTS uq_cobros_cuota_periodo;
CREATE UNIQUE INDEX IF NOT EXISTS uq_cobros_cuota_vencimiento
    ON cobros (tenant_id, cuota_id, vencimiento) WHERE cuota_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS clientes_planes (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    cliente_id     UUID NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    plan           TEXT NOT NULL,
    estado_cobro   TEXT NOT NULL DEFAULT 'cobrando'
                   CHECK (estado_cobro IN ('cobrando', 'gratis', 'pausado')),
    monto_acordado NUMERIC(12,2),
    otorgado       DATE,
    notas          TEXT,
    creado_en      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ordenes_servicio (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    cliente_id        UUID REFERENCES clientes(id) ON DELETE SET NULL,
    tipo              TEXT NOT NULL DEFAULT 'otro'
                      CHECK (tipo IN ('reparacion', 'instalacion', 'consultoria', 'mantenimiento', 'otro')),
    titulo            TEXT NOT NULL,
    equipo            TEXT,
    detalle_reportado TEXT,
    solucion          TEXT,
    estado            TEXT NOT NULL DEFAULT 'recibido',
    fecha_recibido    DATE NOT NULL DEFAULT CURRENT_DATE,
    fecha_entregado   DATE,
    precio            NUMERIC(12,2) CHECK (precio IS NULL OR precio >= 0),
    cobro_id          UUID REFERENCES cobros(id) ON DELETE SET NULL,
    notas             TEXT,
    creado_en         TIMESTAMPTZ NOT NULL DEFAULT now()
);
