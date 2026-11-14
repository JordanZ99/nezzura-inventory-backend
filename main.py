# ==============================================================================
# backend/main.py
# Punto de entrada de FastAPI.
# Corre con: uvicorn main:app --reload
# Documentación automática en: http://localhost:8000/docs
# ==============================================================================

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg2.errors import UndefinedTable

from database.conexion import inicializar_db
from routers import inventario, ventas, gastos, gastos_programados, catalogo_gestion, publico, exportacion, terminales, turnos, stats, mesas, clientes, analitica, licencias, cobros, actividad, mcp_conexion, catalogo_publicado, sync, almacenamiento, planes_clientes, ordenes
from muse.server import app_mcp, servidor

# ── Lifespan ──────────────────────────────────────────────────────────────────
# Junta el startup que antes vivía en @app.on_event (creación de tablas +
# backfill de pagos) con el lifespan del servidor MCP (necesario para que
# los session managers del transporte streamable HTTP arranquen).
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Crear tablas al arrancar si no existen (cada deploy reinicia el
    # servicio, así que las migraciones corren en el arranque, no por-request)
    inicializar_db()
    # Backfill idempotente: repara los tickets históricos con pagos desfasados
    # de _sincronizar_pagos (que solo corrige al editar de ahora en adelante).
    # Nunca bloquea el arranque.
    try:
        from database.ventas import reparar_pagos_desfasados
        r = reparar_pagos_desfasados()
        if r.get("ok") and (r.get("reparadas") or r.get("mixtos_pendientes")):
            print(f"Backfill de pagos: {r.get('reparadas')} reparadas, "
                  f"{r.get('mixtos_pendientes')} mixtas pendientes manuales")
    except Exception as e:
        print(f"Backfill de pagos desfasados: {e}")
    # Servidor MCP (conector IA): el session manager del transporte streamable
    # HTTP debe correr mientras la app viva (patrón documentado por el SDK
    # para montar el servidor dentro de una app FastAPI existente).
    async with servidor.session_manager.run():
        yield


app = FastAPI(
    title="Nezzura Digital API",
    description="Nezzura Digital — Backend para gestión de inventario, ventas PEPS y gastos.",
    version="1.0.0",
    lifespan=lifespan,
)

import os
import re

# URL por defecto del frontend desplegado en Vercel.
# En producción (Render) se sobreescribe con la variable de entorno FRONTEND_URL
# para apuntar a la URL real del deployment de Nezzura Digital.
URL_PRODUCCION = os.getenv("FRONTEND_URL", "https://nezzura-digital.vercel.app")

origenes_permitidos = [
    "http://localhost:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:3001",
    URL_PRODUCCION
]

# CORS: además de los orígenes fijos, aceptamos cualquier subdominio de Vercel
# para cubrir preview deployments automáticos sin tener que listarlos uno a uno.
app.add_middleware(
    CORSMiddleware,
    allow_origins=origenes_permitidos,
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# NOTA: las imágenes de productos ya NO se sirven desde disco local.
# Render tiene filesystem efímero que borra los archivos en cada deploy.
# Ahora las fotos se suben a Cloudinary y se sirven desde su CDN global.
# El endpoint POST /inventario/foto/{producto} se encarga de la subida.

# SQLSTATE 42P01 (tabla inexistente) → 503 con marcador explícito.
# El frontend SOLO reintenta con /init-db cuando recibe este marcador;
# cualquier otro fallo (timeout, red, pool agotado) falla rápido y
# nunca dispara ejecuciones concurrentes de migraciones DDL.
@app.exception_handler(UndefinedTable)
async def db_no_inicializada_handler(request: Request, exc: UndefinedTable):
    return JSONResponse(
        status_code=503,
        content={
            "codigo": "DB_NO_INICIALIZADA",
            "sqlstate": "42P01",
            "mensaje": "La base de datos aún no tiene las tablas necesarias.",
        },
    )

# Registrar todos los routers
app.include_router(inventario.router)
app.include_router(ventas.router)
app.include_router(gastos.router)
app.include_router(gastos_programados.router)
app.include_router(catalogo_gestion.router)
app.include_router(publico.router)
app.include_router(exportacion.router)
app.include_router(terminales.router)
app.include_router(turnos.router)
app.include_router(stats.router)
app.include_router(mesas.router)
app.include_router(clientes.router)
app.include_router(cobros.router)
app.include_router(planes_clientes.router)
app.include_router(ordenes.router)
app.include_router(actividad.router)
app.include_router(analitica.router)
app.include_router(licencias.router)
app.include_router(catalogo_publicado.router)
app.include_router(sync.router)
app.include_router(almacenamiento.router)
app.include_router(mcp_conexion.router)

@app.get("/")
def root():
    from database.conexion import query
    stats = {}
    try:
        stats["productos"] = query("SELECT COUNT(*) as c FROM productos")[0]["c"]
        stats["lotes"] = query("SELECT COUNT(*) as c FROM lotes")[0]["c"]
        stats["ventas"] = query("SELECT COUNT(*) as c FROM ventas")[0]["c"]
        stats["gastos"] = query("SELECT COUNT(*) as c FROM gastos")[0]["c"]
        return {
            "mensaje": "Nezzura Digital API funcionando ✓",
            "database_stats": stats,
            "nota": "Si los números son 0, el backend está conectado a una DB vacía."
        }
    except Exception as e:
        return {"mensaje": "Error obteniendo stats", "error": str(e)}


@app.get("/init-db")
def forzar_init_db():
    """Re-crea las tablas si fueron borradas accidentalmente."""
    try:
        inicializar_db()
        return {"ok": True, "mensaje": "Tablas verificadas / creadas correctamente ✓"}
    except Exception as e:
        return {"ok": False, "mensaje": str(e)}

# ── Servidor MCP (conector IA / Muse) ─────────────────────────────────────────
# Se monta AL FINAL DEL ARCHIVO y en "/": el Mount captura cualquier path que
# las rutas de arriba no matchearan (Starlette evalúa las rutas en orden de
# registro, así que /, /docs, /init-db y todos los routers ganan primero).
# El servidor MCP responde en /mcp (ruta interna del SDK).
#
#   URL:     https://<backend>/mcp
#   Auth:    header X-API-Key (validación en muse/auth.py)
#   Alcance: SOLO LECTURA del catálogo del tenant dueño de la clave.
app.mount("/", app_mcp)


