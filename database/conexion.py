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
                    Stock_Lote    INTEGER DEFAULT 0,
                    Fecha_Entrada TEXT,
                    Estado        TEXT DEFAULT 'Activo'
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ventas (
                    id              SERIAL PRIMARY KEY,
                    Fecha           TEXT,
                    Producto        TEXT,
                    Cantidad        INTEGER,
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

        conn.commit()
    finally:
        release_conn(conn)