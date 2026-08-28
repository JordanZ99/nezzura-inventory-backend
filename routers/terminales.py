# ==============================================================================
# backend/routers/terminales.py
# CRUD de terminales bancarias con sus comisiones (Fase B).
# El tenant_id SIEMPRE sale del JWT; nunca del cliente.
# ==============================================================================

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from dependencies import get_tenant_id
from database.terminales import (
    listar_terminales, crear_terminal, actualizar_terminal, eliminar_terminal,
)

router = APIRouter(prefix="/terminales", tags=["Terminales"])


class NuevaTerminal(BaseModel):
    nombre: str = Field(..., min_length=1, description="Nombre identificador (ej. 'Banorte móvil')")
    banco: Optional[str] = None
    comision_debito_pct: float = Field(0, ge=0, description="% de comisión sobre débito")
    comision_credito_pct: float = Field(0, ge=0, description="% de comisión sobre crédito")
    comision_fija: float = Field(0, ge=0, description="Cuota fija por transacción")


class ActualizarTerminal(BaseModel):
    nombre: Optional[str] = None
    banco: Optional[str] = None
    comision_debito_pct: Optional[float] = Field(None, ge=0)
    comision_credito_pct: Optional[float] = Field(None, ge=0)
    comision_fija: Optional[float] = Field(None, ge=0)
    activo: Optional[bool] = None


@router.get("/")
def listar(tenant_id: str = Depends(get_tenant_id)):
    return listar_terminales(tenant_id)


@router.post("/")
def crear(data: NuevaTerminal, tenant_id: str = Depends(get_tenant_id)):
    resultado = crear_terminal(
        tenant_id, data.nombre, data.banco,
        data.comision_debito_pct, data.comision_credito_pct, data.comision_fija,
    )
    if not resultado.get("ok"):
        raise HTTPException(status_code=400, detail=resultado.get("mensaje"))
    return resultado


@router.patch("/{terminal_id}")
def actualizar(terminal_id: str, data: ActualizarTerminal, tenant_id: str = Depends(get_tenant_id)):
    resultado = actualizar_terminal(
        terminal_id, tenant_id,
        nombre=data.nombre, banco=data.banco,
        comision_debito_pct=data.comision_debito_pct,
        comision_credito_pct=data.comision_credito_pct,
        comision_fija=data.comision_fija,
        activo=data.activo,
    )
    if not resultado.get("ok"):
        raise HTTPException(status_code=400, detail=resultado.get("mensaje"))
    return resultado


@router.delete("/{terminal_id}")
def borrar(terminal_id: str, tenant_id: str = Depends(get_tenant_id)):
    resultado = eliminar_terminal(terminal_id, tenant_id)
    if not resultado.get("ok"):
        raise HTTPException(status_code=400, detail=resultado.get("mensaje"))
    return resultado
