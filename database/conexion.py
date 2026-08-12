# ==============================================================================
# backend/database/conexion.py
# Conexión a PostgreSQL con Connection Pool.
# Sin dependencias de Streamlit — puro Python.
# ==============================================================================

import os
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from psycopg2.errors import UndefinedTable, UndefinedColumn
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")

try:
    _pool = pool.SimpleConnectionPool(1, 10, DATABASE_URL)
except Exception as e:
    print(f"❌ Error creando el pool: {e}")
    _pool = None


def get_conn():
    return _pool.getconn()


def release_conn(conn):
    _pool.putconn(conn)


def query(sql: str, params: tuple = None) -> list[dict]:
    """Ejecuta un SELECT y devuelve lista de dicts."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params or ())
            result = [dict(row) for row in cur.fetchall()]
        conn.commit()
        return result
    except (UndefinedTable, UndefinedColumn):
        # Si falta una tabla o columna (ej: migración nueva aún no aplicada),
        # re-ejecutamos inicializar_db() para crear/alterar y reintentamos.
        conn.rollback()
        inicializar_db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params or ())
            result = [dict(row) for row in cur.fetchall()]
        conn.commit()
        return result
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        release_conn(conn)


def execute(sql: str, params: tuple = None) -> None:
    """Ejecuta INSERT, UPDATE o DELETE."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
        conn.commit()
    except (UndefinedTable, UndefinedColumn):
        # Igual que en query(): si falta tabla/columna, migrar automáticamente y reintentar.
        conn.rollback()
        inicializar_db()
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        release_conn(conn)


def inicializar_db():
    """Crea las tablas si no existen al arrancar."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # 1. Tablas Core
            cur.execute("""
                CREATE TABLE IF NOT EXISTS productos (
                    id          SERIAL PRIMARY KEY,
                    Producto    TEXT NOT NULL,
                    Descripcion TEXT,
                    Imagen      TEXT DEFAULT 'No hay foto',
                    Estado      TEXT DEFAULT 'Activo'
                )
            """)
            # Tabla de categorías (Many-to-Many con productos)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS categorias (
                    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    tenant_id   UUID NOT NULL,
                    nombre      TEXT NOT NULL,
                    slug        TEXT NOT NULL,
                    UNIQUE(tenant_id, nombre)
                )
            """)
            # Tabla pivote: rompe la relación Muchos a Muchos entre productos y categorías
            # producto_id es INTEGER porque productos.id es SERIAL
            # categoria_id es UUID porque categorias.id es UUID
            cur.execute("""
                CREATE TABLE IF NOT EXISTS producto_categorias (
                    producto_id   INTEGER NOT NULL REFERENCES productos(id) ON DELETE CASCADE,
                    categoria_id  UUID    NOT NULL REFERENCES categorias(id) ON DELETE CASCADE,
                    PRIMARY KEY (producto_id, categoria_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS lotes (
                    id            SERIAL PRIMARY KEY,
                    ID_Lote       TEXT NOT NULL UNIQUE,
                    Producto      TEXT NOT NULL,
                    Costo         REAL DEFAULT 0,
                    Precio_Venta  REAL DEFAULT 0,
                    Stock_Lote    REAL DEFAULT 0,   -- REAL desde la Fase 5 (vender 0.5 kg)
                    Fecha_Entrada TEXT,
                    Estado        TEXT DEFAULT 'Activo'
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ventas (
                    id              SERIAL PRIMARY KEY,
                    n_ticket        INTEGER,
                    Fecha           TEXT,
                    Producto        TEXT,
                    Cantidad        REAL,   -- REAL desde la Fase 5 (0.5 kg, 150g...)
                    Precio_Lista    REAL,
                    Precio_Real     REAL,
                    Costo_Unitario  REAL,
                    Total_Venta     REAL,
                    Ganancia_Bruta  REAL,
                    Estado          TEXT DEFAULT 'Activo',
                    ID_Lote         TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS gastos (
                    id          SERIAL PRIMARY KEY,
                    Fecha       TEXT,
                    Categoria   TEXT,
                    Descripcion TEXT,
                    Monto       REAL
                )
            """)
            
            # 2. Asegurar columnas específicas si las tablas ya existían
            try:
                cur.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS Estado TEXT DEFAULT 'Activo'")
                cur.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS ID_Lote TEXT")
                # n_ticket lo lee get_ventas (historial); asegurarlo por si la
                # tabla se creó antes de que existiera la columna.
                cur.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS n_ticket integer")
            except Exception:
                pass

            # Tabla de categorías de gasto (editables por el usuario)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS gastos_categorias (
                    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    tenant_id   UUID NOT NULL,
                    nombre      TEXT NOT NULL,
                    UNIQUE(tenant_id, nombre)
                )
            """)

            # Migración: asegurar tenant_id en tablas que lo necesitan
            for tbl in ['productos', 'lotes', 'ventas', 'gastos']:
                try:
                    cur.execute(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS tenant_id UUID")
                except Exception:
                    pass

            # Migración: visibilidad de producto en catálogo público
            # (misma columna que 005_producto_visible_catalogo.sql — idempotente)
            try:
                cur.execute("ALTER TABLE productos ADD COLUMN IF NOT EXISTS visible_en_catalogo boolean DEFAULT true")
            except Exception:
                pass

            # Migración: hero + anuncios del catálogo público
            # (mismas columnas que 006_catalogo_hero_anuncios.sql — idempotente)
            try:
                cur.execute("ALTER TABLE catalogo_config ADD COLUMN IF NOT EXISTS banner_url text DEFAULT ''")
                cur.execute("ALTER TABLE catalogo_config ADD COLUMN IF NOT EXISTS hero_estilo text DEFAULT 'gradiente'")
                cur.execute("ALTER TABLE catalogo_config ADD COLUMN IF NOT EXISTS anuncio_texto text DEFAULT ''")
            except Exception:
                pass

            # Migración: agrupación por categoría configurable en el catálogo público
            # (misma columna que 007_catalogo_agrupar_paginar.sql — idempotente)
            try:
                cur.execute("ALTER TABLE catalogo_config ADD COLUMN IF NOT EXISTS agrupar_por_categoria boolean")
                cur.execute("ALTER TABLE catalogo_config ADD COLUMN IF NOT EXISTS columnas_movil integer DEFAULT 2")
            except Exception:
                pass

            # Migración: permitir descargar fotos del catálogo (009 — idempotente)
            try:
                cur.execute("ALTER TABLE catalogo_config ADD COLUMN IF NOT EXISTS permitir_descarga boolean DEFAULT false")
            except Exception:
                pass

            # Migración: visibilidad del catálogo (010 — idempotente)
            try:
                cur.execute("ALTER TABLE catalogo_config ADD COLUMN IF NOT EXISTS ocultar_agotados boolean DEFAULT false")
                # El stock se muestra por defecto: normalizar NULLs y fijar default true
                cur.execute("UPDATE catalogo_config SET mostrar_stock = true WHERE mostrar_stock IS NULL")
                cur.execute("ALTER TABLE catalogo_config ALTER COLUMN mostrar_stock SET DEFAULT true")
            except Exception:
                pass

            # Migración: texto del banner en modo imagen (011 — idempotente)
            try:
                cur.execute("ALTER TABLE catalogo_config ADD COLUMN IF NOT EXISTS banner_texto_color text DEFAULT '#ffffff'")
                cur.execute("ALTER TABLE catalogo_config ADD COLUMN IF NOT EXISTS banner_mostrar_texto boolean DEFAULT true")
            except Exception:
                pass

            # Migración: banner específico para móvil (012 — idempotente)
            try:
                cur.execute("ALTER TABLE catalogo_config ADD COLUMN IF NOT EXISTS banner_url_movil text DEFAULT ''")
            except Exception:
                pass

            # Migración: mostrar el logo sobre el banner (013 — idempotente)
            try:
                cur.execute("ALTER TABLE catalogo_config ADD COLUMN IF NOT EXISTS banner_mostrar_logo boolean DEFAULT true")
            except Exception:
                pass

            # Migración: etiqueta/presentación por lote (014 — idempotente)
            try:
                cur.execute("ALTER TABLE lotes ADD COLUMN IF NOT EXISTS etiqueta text DEFAULT ''")
            except Exception:
                pass

            # Migración: modo de precio sugerido del POS (015 — idempotente)
            try:
                cur.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS modo_precio_sugerido text DEFAULT 'antiguo'")
            except Exception:
                pass

            # Migración: sufijo del precio en el catálogo (016 — idempotente)
            # '' = sin sufijo; 'c/u', 'por kilo' o texto libre (ej. 'por litro')
            try:
                cur.execute("ALTER TABLE productos ADD COLUMN IF NOT EXISTS sufijo_precio text DEFAULT ''")
            except Exception:
                pass

            # Migración: productos de servicio sin stock (017 — idempotente)
            # tipo_producto: 'stock' (normal) | 'servicio' (sin inventario)
            try:
                cur.execute("ALTER TABLE productos ADD COLUMN IF NOT EXISTS tipo_producto text DEFAULT 'stock'")
                cur.execute("ALTER TABLE productos ADD COLUMN IF NOT EXISTS costo_servicio real DEFAULT 0")
                cur.execute("ALTER TABLE productos ADD COLUMN IF NOT EXISTS precio_servicio real DEFAULT 0")
                # Las ventas guardan el tipo del producto vendido, para saber al
                # editar/anular si hay que tocar inventario.
                cur.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS tipo_producto text DEFAULT 'stock'")
            except Exception:
                pass

            # Migración: variaciones de producto (018 — idempotente)
            # Una variación es una presentación con su PROPIO precio para un mismo
            # producto (ej. Sencilla/Doble, S/M/L). Aplica a cualquier tipo de producto.
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS producto_variaciones (
                        id          BIGSERIAL PRIMARY KEY,
                        producto_id INTEGER NOT NULL REFERENCES productos(id) ON DELETE CASCADE,
                        nombre      TEXT    NOT NULL,
                        precio      REAL    NOT NULL DEFAULT 0,
                        tenant_id   UUID    NOT NULL,
                        UNIQUE(producto_id, nombre)
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_producto_variaciones_producto ON producto_variaciones (producto_id, tenant_id)")
                # Las ventas guardan QUÉ variación se vendió (historial)
                cur.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS variacion text DEFAULT ''")
            except Exception:
                pass

            # Migración: productos compuestos con receta (019 — idempotente)
            # Un compuesto (ej. hamburguesa) NO tiene stock propio: al venderlo se
            # descuenta el stock de sus MATERIALES (producto_recetas = BOM).
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS producto_recetas (
                        id          BIGSERIAL PRIMARY KEY,
                        producto_id INTEGER NOT NULL REFERENCES productos(id) ON DELETE CASCADE,  -- el compuesto
                        material_id INTEGER NOT NULL REFERENCES productos(id) ON DELETE CASCADE,   -- el material (producto de stock)
                        cantidad    REAL    NOT NULL DEFAULT 1,
                        tenant_id   UUID    NOT NULL,
                        UNIQUE(producto_id, material_id)
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_producto_recetas_producto ON producto_recetas (producto_id, tenant_id)")
                # Las ventas de compuestos guardan el CONSUMO real: [{material, id_lote, cantidad, costo}]
                # — esto permite anular/editar revirtiendo EXACTAMENTE los lotes usados.
                cur.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS consumo jsonb DEFAULT NULL")
            except Exception:
                pass

            # Migración: recetas POR VARIACIÓN (020 — idempotente)
            # variacion_id NULL = receta base (se usa si la variación vendida no
            # tiene receta propia); si no, receta específica de esa variación.
            try:
                cur.execute("ALTER TABLE producto_recetas ADD COLUMN IF NOT EXISTS variacion_id INTEGER REFERENCES producto_variaciones(id) ON DELETE CASCADE")
            except Exception:
                pass
            # El UNIQUE(producto_id, material_id) bloqueaba dos variaciones con
            # el mismo material → se reemplaza por índices únicos parciales.
            # (Bloque separado: si el nombre del constraint difiriera en algún
            #  entorno, no se pierden los índices por el fallo del DROP.)
            try:
                cur.execute("ALTER TABLE producto_recetas DROP CONSTRAINT IF EXISTS producto_recetas_producto_id_material_id_key")
            except Exception:
                pass
            try:
                cur.execute("DROP INDEX IF EXISTS uq_producto_recetas_base")
                cur.execute("""
                    CREATE UNIQUE INDEX uq_producto_recetas_base
                        ON producto_recetas (producto_id, material_id)
                        WHERE variacion_id IS NULL
                """)
            except Exception:
                pass
            try:
                cur.execute("DROP INDEX IF EXISTS uq_producto_recetas_variacion")
                cur.execute("""
                    CREATE UNIQUE INDEX uq_producto_recetas_variacion
                        ON producto_recetas (producto_id, variacion_id, material_id)
                        WHERE variacion_id IS NOT NULL
                """)
            except Exception:
                pass

            # Migración: stock decimal + foto por variación (021 — idempotente)
            # Stock_Lote y ventas.Cantidad pasan de INTEGER a REAL (vender 0.5 kg,
            # 150g...). PostgreSQL convierte INTEGER → REAL automáticamente.
            try:
                cur.execute("ALTER TABLE lotes ALTER COLUMN Stock_Lote TYPE REAL")
            except Exception:
                pass
            try:
                cur.execute("ALTER TABLE ventas ALTER COLUMN Cantidad TYPE REAL")
            except Exception:
                pass
            # Foto propia por variación (el catálogo muestra la foto de la
            # presentación seleccionada, ej. Hamburguesa Doble vs Sencilla).
            try:
                cur.execute("ALTER TABLE producto_variaciones ADD COLUMN IF NOT EXISTS foto text DEFAULT ''")
            except Exception:
                pass

            # Migración: stock por variación (022 — idempotente)
            # lotes.variacion_id: NULL = stock del producto/base; {id} = stock
            # EXCLUSIVO de esa variación. productos.stock_por_variacion: flag
            # por producto (default false = variaciones comparten stock).
            try:
                cur.execute("ALTER TABLE lotes ADD COLUMN IF NOT EXISTS variacion_id INTEGER REFERENCES producto_variaciones(id) ON DELETE CASCADE")
            except Exception:
                pass
            try:
                cur.execute("ALTER TABLE productos ADD COLUMN IF NOT EXISTS stock_por_variacion boolean DEFAULT false")
            except Exception:
                pass
            try:
                cur.execute("CREATE INDEX IF NOT EXISTS idx_lotes_variacion ON lotes (Producto, variacion_id, tenant_id)")
            except Exception:
                pass

            # Migración: producto fraccionable (023 — idempotente)
            # true = acepta decimales al vender (kg, lt, mt, ...);
            # false (default) = solo unidades enteras (c/u, sin sufijo, ...).
            try:
                cur.execute("ALTER TABLE productos ADD COLUMN IF NOT EXISTS fraccionable boolean DEFAULT false")
            except Exception:
                pass
            # Derivación inicial para productos existentes (misma lógica que la
            # migración SQL): los sufijos de peso/volumen/longitud pasan a true.
            try:
                cur.execute("""
                    UPDATE productos SET fraccionable = true
                    WHERE COALESCE(sufijo_precio, '') <> ''
                      AND (
                            lower(sufijo_precio) IN ('kg', 'lt', 'mt', 'g', 'ml', 'm',
                                                     'por kilo', 'por litro', 'por metro',
                                                     'kilo', 'litro', 'metro')
                         OR lower(sufijo_precio) LIKE '%kilo%'
                         OR lower(sufijo_precio) LIKE '%litro%'
                         OR lower(sufijo_precio) LIKE '%metro%'
                         OR lower(sufijo_precio) LIKE '%gramo%'
                         OR lower(sufijo_precio) LIKE '%mililitro%'
                      )
                """)
            except Exception:
                pass

        conn.commit()
    finally:
        release_conn(conn)