# ==============================================================================
# backend/database/conexion.py
# Conexión a PostgreSQL con Connection Pool.
# Sin dependencias de Streamlit — puro Python.
# ==============================================================================

import os
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from psycopg2.errors import UndefinedTable
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DEFAULT_TENANT_ID = "854d3200-9a78-47cb-874a-9f44d9b036d8"

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
            return [dict(row) for row in cur.fetchall()]
    except UndefinedTable:
        conn.rollback()
        inicializar_db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params or ())
            return [dict(row) for row in cur.fetchall()]
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
    except UndefinedTable:
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
    """Crea las tablas si no existen al arrancar y maneja la migración a multitenant."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # 1. Tabla de Tenants (Clientes/Cuentas)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tenants (
                    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    nombre      TEXT NOT NULL,
                    email       TEXT NOT NULL UNIQUE,
                    plan        TEXT DEFAULT 'basico',
                    activo      BOOLEAN DEFAULT true,
                    creado_en   TIMESTAMP DEFAULT now()
                )
            """)

            # 2. Crear Tenant por defecto para datos huérfanos/actuales
            # Usamos un UUID estático para el primer tenant para facilitar la transición
            DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000000"
            cur.execute("""
                INSERT INTO tenants (id, nombre, email)
                VALUES (%s, 'Goyangi Principal', 'admin@goyangi.com')
                ON CONFLICT (id) DO NOTHING
                ON CONFLICT (email) DO NOTHING
            """, (DEFAULT_TENANT_ID,))

            # 3. Tablas Core
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS productos (
                    id          SERIAL PRIMARY KEY,
                    Producto    TEXT NOT NULL UNIQUE,
                    Descripcion TEXT,
                    Imagen      TEXT DEFAULT 'No hay foto',
                    Estado      TEXT DEFAULT 'Activo',
                    Categoria   TEXT DEFAULT 'General',
                    tenant_id   UUID REFERENCES tenants(id) DEFAULT '{DEFAULT_TENANT_ID}'
                )
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS lotes (
                    id            SERIAL PRIMARY KEY,
                    ID_Lote       TEXT NOT NULL UNIQUE,
                    Producto      TEXT NOT NULL,
                    Costo         REAL DEFAULT 0,
                    Precio_Venta  REAL DEFAULT 0,
                    Stock_Lote    INTEGER DEFAULT 0,
                    Fecha_Entrada TEXT,
                    Estado        TEXT DEFAULT 'Activo',
                    tenant_id     UUID REFERENCES tenants(id) DEFAULT '{DEFAULT_TENANT_ID}'
                )
            """)
            cur.execute(f"""
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
                    ID_Lote         TEXT,
                    tenant_id       UUID REFERENCES tenants(id) DEFAULT '{DEFAULT_TENANT_ID}'
                )
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS gastos (
                    id          SERIAL PRIMARY KEY,
                    Fecha       TEXT,
                    Categoria   TEXT,
                    Descripcion TEXT,
                    Monto       REAL,
                    tenant_id   UUID REFERENCES tenants(id) DEFAULT '{DEFAULT_TENANT_ID}'
                )
            """)
            
            # 4. Migración: Asegurar columnas en tablas que ya existen
            tablas = ["productos", "lotes", "ventas", "gastos"]
            for tabla in tablas:
                try:
                    cur.execute(f"ALTER TABLE {tabla} ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) DEFAULT '{DEFAULT_TENANT_ID}'")
                except Exception:
                    pass

            # Columnas adicionales específicas
            try:
                cur.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS Estado TEXT DEFAULT 'Activo'")
                cur.execute("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS ID_Lote TEXT")
                cur.execute("ALTER TABLE productos ADD COLUMN IF NOT EXISTS Categoria TEXT DEFAULT 'General'")
            except Exception:
                pass
                
        conn.commit()
    finally:
        release_conn(conn)