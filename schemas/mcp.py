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
