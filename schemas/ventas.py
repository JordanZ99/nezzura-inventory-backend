from typing import Optional, List
from pydantic import BaseModel


class ItemCarrito(BaseModel):
    producto   : str
    cantidad   : float
    precio_real: float
    id_lote    : Optional[str] = None
    variacion  : Optional[str] = None
    # Venta libre (migración 036): renglón del producto genérico 'Venta libre'.
    # descripcion = texto libre para el ticket ("Cereal", "Silla usada");
    # costo = costo opcional capturado en el POS (default 0 → ganancia = precio).
    descripcion: Optional[str] = None
    costo      : Optional[float] = None


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
    # Cobro de mesa (Fase 2, migración 037): si viene, el backend convierte
    # estos items en el ticket de ESA mesa y la libera en la misma transacción.
    mesa_id: Optional[str] = None


class ActualizarVenta(BaseModel):
    fecha         : Optional[str]   = None
    cantidad      : Optional[float] = None
    precio_real   : Optional[float] = None
    costo_unitario: Optional[float] = None
    total_venta   : Optional[float] = None
    ganancia_bruta: Optional[float] = None


class ActualizarOrden(BaseModel):
    fecha: str
