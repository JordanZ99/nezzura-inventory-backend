# ==============================================================================
# backend/schemas/mcp.py — Bodies de routers/mcp_conexion.py
# ==============================================================================

from typing import Optional
from pydantic import BaseModel, Field


class CrearClaveMcp(BaseModel):
    """Alta de clave para conectar un agente IA (custom connector de Muse)."""
    nombre: Optional[str] = Field(
        default=None,
        max_length=40,
        description="Etiqueta para reconocer la clave en la lista (ej. 'Muse del celular')",
    )
    nivel: int = Field(
        default=1,
        ge=1,
        le=3,
        description=(
            "Nivel de permiso: 1 lectura básica (catálogo), 2 lectura avanzada "
            "(+ análisis del negocio, plan Plus), 3 escritura básica "
            "(+ crear productos con borrador y confirmación)."
        ),
    )


class EditarClaveMcp(BaseModel):
    """Edición de una clave ACTIVA (sin tocar su hash: el agente no se reconecta)."""
    nivel: Optional[int] = Field(
        default=None,
        ge=1,
        le=3,
        description="Nuevo nivel de permiso. Omitir = no cambiar.",
    )
    limite_llamadas: Optional[int] = Field(
        default=None,
        ge=0,
        le=1000000,
        description="Override de llamadas por día. Omitir = no cambiar. 0 = volver al default del nivel.",
    )
    limite_escrituras: Optional[int] = Field(
        default=None,
        ge=0,
        le=1000000,
        description="Override de escrituras por día. Omitir = no cambiar. 0 = volver al default del nivel.",
    )
