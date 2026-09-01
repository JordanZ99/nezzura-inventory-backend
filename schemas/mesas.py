# ==============================================================================
# backend/schemas/mesas.py
# Contratos de la API de mesas (Fase 2, preset restaurante).
# ==============================================================================

from typing import Optional, List
from pydantic import BaseModel


class NuevaMesa(BaseModel):
    nombre   : str
    capacidad: Optional[int] = None


class ActualizarMesa(BaseModel):
    nombre   : Optional[str] = None
    capacidad: Optional[int] = None


class ReordenMesa(BaseModel):
    id   : str
    orden: int


class ReordenarMesas(BaseModel):
    mesas: List[ReordenMesa]


class ItemMesa(BaseModel):
    producto       : str
    cantidad       : float
    precio_unitario: float
    descripcion    : Optional[str] = None   # texto libre de la venta libre
    variacion      : Optional[str] = None
    costo          : Optional[float] = None  # solo venta libre
    notas          : Optional[str] = None


class AgregarItemsMesa(BaseModel):
    items: List[ItemMesa]


class ActualizarItemMesa(BaseModel):
    cantidad       : Optional[float] = None
    precio_unitario: Optional[float] = None
    notas          : Optional[str] = None
