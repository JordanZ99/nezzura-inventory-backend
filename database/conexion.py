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
    """Crea las tablas si no existen al arrancar."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS productos (
                    id          SERIAL PRIMARY KEY,
                    Producto    TEXT NOT NULL UNIQUE,
                    Descripcion TEXT,
                    Imagen      TEXT DEFAULT 'No hay foto',
                    Estado      TEXT DEFAULT 'Activo'
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
                    Ganancia_Bruta  REAL
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
        conn.commit()
    finally:
        release_conn(conn)