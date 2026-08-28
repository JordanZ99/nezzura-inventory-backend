# ==============================================================================
# backend/main.py
# Punto de entrada de FastAPI.
# Corre con: uvicorn main:app --reload
# Documentación automática en: http://localhost:8000/docs
# ==============================================================================

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database.conexion import inicializar_db
from routers import inventario, ventas, gastos, gastos_programados, catalogo_gestion, publico, exportacion, terminales, turnos

app = FastAPI(
    title="Nezzura Digital API",
    description="Nezzura Digital — Backend para gestión de inventario, ventas PEPS y gastos.",
    version="1.0.0"
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

# Crear tablas al arrancar si no existen
@app.on_event("startup")
def startup():
    inicializar_db()

# NOTA: Las imágenes de productos ya NO se sirven desde disco local.
# Render tiene filesystem efímero que borra los archivos en cada deploy.
# Ahora las fotos se suben a Cloudinary y se sirven desde su CDN global.
# El endpoint POST /inventario/foto/{producto} se encarga de la subida.

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


