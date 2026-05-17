# ==============================================================================
# backend/main.py
# Punto de entrada de FastAPI.
# Corre con: uvicorn main:app --reload
# Documentación automática en: http://localhost:8000/docs
# ==============================================================================

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database.conexion import inicializar_db
from routers import inventario, ventas, gastos

app = FastAPI(
    title="Goyangi Store API",
    description="Backend para gestión de inventario, ventas PEPS y gastos.",
    version="1.0.0"
)

import os

URL_PRODUCCION = os.getenv("FRONTEND_URL", "https://goyangi-frontend.vercel.app")

origenes_permitidos = [
    "http://localhost:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:3001",
    "https://goyangi-frontend-git-multitenant-jordanz99s-projects.vercel.app",
    URL_PRODUCCION
]

# CORS seguro con orígenes explícitos
app.add_middleware(
    CORSMiddleware,
    allow_origins=origenes_permitidos,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Crear tablas al arrancar si no existen
@app.on_event("startup")
def startup():
    inicializar_db()

# Registrar todos los routers
app.include_router(inventario.router)
app.include_router(ventas.router)
app.include_router(gastos.router)

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
            "mensaje": "Goyangi Store API funcionando ✓",
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


