from typing import Optional, List
from pydantic import BaseModel


class ItemCarrito(BaseModel):
    producto   : str
    cantidad   : float
    precio_real: float
    id_lote    : Optional[str] = None
    variacion  : Optional[str] = None


class PagoItem(BaseModel):
    metodo    : str
    monto     : float
    referencia: Optional[str] = None


class PagoCarrito(BaseModel):
    metodo        : str
    propina       : float = 0.0
    pagos         : Optional[list[PagoItem]] = None
    monto_recibido: Optional[float] = None


class Carrito(BaseModel):
    items: list[ItemCarrito]
    pago : Optional[PagoCarrito] = None


class ActualizarVenta(BaseModel):
    fecha         : Optional[str]   = None
    cantidad      : Optional[float] = None
    precio_real   : Optional[float] = None
    costo_unitario: Optional[float] = None
    total_venta   : Optional[float] = None
    ganancia_bruta: Optional[float] = None


class ActualizarOrden(BaseModel):
    fecha: str
