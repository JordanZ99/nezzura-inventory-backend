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


def _dividir_sql(sql: str) -> list[str]:
    """Divide un script SQL en statements individuales, respetando bloques
    $$...$$ (las migraciones 003/006 usan DO blocks con `;` internos), así como
    comentarios de línea (--), comentarios de bloque (/* ... */) y cadenas literales ('...')."""
    statements = []
    actual = []
    i = 0
    en_dolar = False
    en_comentario_linea = False
    en_comentario_bloque = False
    en_cadena = False
    
    n = len(sql)
    while i < n:
        # Detectar delimitadores de comentario, cadena o bloque de forma segura
        # sólo si no estamos ya dentro de otro tipo de delimitador.
        if not en_comentario_linea and not en_comentario_bloque and not en_cadena:
            if sql.startswith("$$", i):
                en_dolar = not en_dolar
                actual.append("$$")
                i += 2
                continue
            if sql.startswith("--", i):
                en_comentario_linea = True
                actual.append("--")
                i += 2
                continue
            if sql.startswith("/*", i):
                en_comentario_bloque = True
                actual.append("/*")
                i += 2
                continue
        
        c = sql[i]
        
        if en_comentario_linea:
            if c in ("\n", "\r"):
                en_comentario_linea = False
        elif en_comentario_bloque:
            if sql.startswith("*/", i):
                en_comentario_bloque = False
                actual.append("*/")
                i += 2
                continue
        elif en_cadena:
            # Manejar el escape estándar de comilla simple en SQL: ''
            if sql.startswith("''", i):
                actual.append("''")
                i += 2
                continue
            if c == "'":
                en_cadena = False
        else: # Código SQL normal
            if c == "'":
                en_cadena = True
            elif c == ";" and not en_dolar:
                stmt = "".join(actual).strip()
                if stmt:
                    statements.append(stmt)
                actual = []
                i += 1
                continue
                
        actual.append(c)
        i += 1
        
    stmt = "".join(actual).strip()
    if stmt:
        statements.append(stmt)
    return statements



def _ejecutar_migraciones(cur) -> None:
    """Ejecuta los archivos de migrations/*.sql en orden numérico.

    Es la FUENTE ÚNICA del esquema: antes estos statements estaban duplicados
    inline en inicializar_db() (con comentarios tipo "misma columna que 005..."),
    lo que generaba drift silencioso si solo se cambiaba uno de los dos lugares.

    Cada statement va en su propio try/except (mismo comportamiento tolerante
    que el código anterior): un fallo puntual —tabla/columna que aún no existe,
    migración ya aplicada— no detiene el resto ni rompe el arranque.
    """
    directorio = Path(__file__).resolve().parent.parent / "migrations"
    if not directorio.exists():
        return
    for archivo in sorted(directorio.glob("*.sql")):
        try:
            sql = archivo.read_text(encoding="utf-8")
        except Exception as e:
            print(f"Error leyendo {archivo.name}: {e}")
            continue
        for stmt in _dividir_sql(sql):
            try:
                cur.execute(stmt)
            except Exception as e:
                print(f"Migración {archivo.name}: {e}")


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

            # ── Migraciones del esquema: fuente única en migrations/*.sql ──
            # Antes, cada migración (005-025) estaba DUPLICADA inline aquí con
            # comentarios tipo "misma columna que 005..." — dos fuentes de verdad
            # que podían divergir en silencio. El runner ejecuta los archivos en
            # orden; cada statement en su propio try/except (tolerante a fallos).
            _ejecutar_migraciones(cur)

        conn.commit()
    finally:
        release_conn(conn)