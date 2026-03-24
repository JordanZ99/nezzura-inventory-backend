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

URL_PRODUCCION = os.getenv("FRONTEND_URL", "https://goyangi.vercel.app")

origenes_permitidos = [
    "http://localhost:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:3001",
    URL_PRODUCCION
]

# CORS cerrado: protege el backend de peticiones hechas desde otras páginas piratas
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
    return {"mensaje": "Goyangi Store API funcionando ✓"}
