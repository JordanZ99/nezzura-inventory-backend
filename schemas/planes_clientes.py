# ==============================================================================
# backend/schemas/planes_clientes.py
# Modelos Pydantic de "Mis cuentas" (giro 'servicios', migración 075): el
# registro MANUAL de qué plan del gestor tiene cada cliente y si se le cobra.
# Es un cuaderno personal: nunca genera cobros ni persiste dinero.
# ==============================================================================

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field

# Estado del COBRO del plan: cobrando (hay cuota activa), gratis (de momento
# no se le cobra nada) o pausado (acuerdo congelado).
EstadoCobroPlan = Literal["cobrando", "gratis", "pausado"]


class PlanNuevo(BaseModel):
    """Alta de una anotación 'cliente tiene plan X'."""
    cliente_id     : str = Field(..., min_length=1)
    plan           : str = Field(..., min_length=1, description="Nombre libre: 'local', 'plus', 'básico'...")
    estado_cobro   : EstadoCobroPlan = "cobrando"
    monto_acordado : Optional[float] = Field(None, ge=0, description="Informativo; NULL = sin monto")
    otorgado       : Optional[date] = None
    notas          : Optional[str] = None


class PlanActualizar(BaseModel):
    """PATCH parcial. monto_acordado: 0 = quitar el monto (NULL); >0 = fijar."""
    cliente_id     : Optional[str] = None
    plan           : Optional[str] = Field(None, min_length=1)
    estado_cobro   : Optional[EstadoCobroPlan] = None
    monto_acordado : Optional[float] = Field(None, ge=0)
    otorgado       : Optional[date] = None
    notas          : Optional[str] = None
