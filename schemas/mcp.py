# ==============================================================================
# backend/schemas/mcp.py — Bodies de routers/mcp_conexion.py
# ==============================================================================

from typing import List, Optional
from pydantic import BaseModel, Field


class CrearClaveMcp(BaseModel):
    """Alta de clave para conectar un agente IA (custom connector de Muse)."""
    nombre: Optional[str] = Field(
        default=None,
        max_length=40,
        description="Etiqueta para reconocer la clave en la lista (ej. 'Muse del celular')",
    )
    roles: Optional[List[str]] = Field(
        default=None,
        description=(
            "ROLES funcionales de la clave (081): la UNIÓN de sus tools es lo "
            "que el agente ve. Valores: publicador, analista, disenador, "
            "inventario. Lista vacía u omitida = publicador."
        ),
    )


class EditarClaveMcp(BaseModel):
    """Edición de una clave ACTIVA (sin tocar su hash: el agente no se reconecta)."""
    roles: Optional[List[str]] = Field(
        default=None,
        description=(
            "Nuevos roles de la clave (mínimo 1: una clave sin roles no ve "
            "ninguna tool). Omitir = no cambiar."
        ),
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
    ver_costos: Optional[bool] = Field(
        default=None,
        description=(
            "Permiso de ESTA clave para que el agente lea costos internos y "
            "márgenes (migrations/068). Independiente de los roles. Omitir = no cambiar."
        ),
    )
