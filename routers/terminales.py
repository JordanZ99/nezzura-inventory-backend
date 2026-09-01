# ==============================================================================
# backend/routers/terminales.py
# CRUD de terminales bancarias con sus comisiones (Fase B).
# El tenant_id SIEMPRE sale del JWT; nunca del cliente.
# ==============================================================================

from fastapi import APIRouter, Depends, HTTPException

from dependencies import get_tenant_id
from schemas.terminales import NuevaTerminal, ActualizarTerminal
from database.terminales import (
    listar_terminales, crear_terminal, actualizar_terminal, eliminar_terminal,
)

router = APIRouter(prefix="/terminales", tags=["Terminales"])


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
