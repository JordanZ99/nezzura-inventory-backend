# ==============================================================================
# backend/routers/clientes.py
# Cartera de clientes por tenant (Fase A del sistema de puntos).
# El tenant_id SIEMPRE sale del JWT; nunca del cliente.
# ==============================================================================

from fastapi import APIRouter, Depends, HTTPException

from dependencies import get_tenant_id
from schemas.clientes import ClienteNuevo, ClienteActualizar, AjustePuntos, VerificarCliente
from database import clientes as db_clientes
from database import puntos as db_puntos

router = APIRouter(prefix="/clientes", tags=["Clientes"])


@router.get("/")
def listar(
    q: str = None,
    incluir_inactivos: bool = False,
    tenant_id: str = Depends(get_tenant_id),
):
    """Lista de clientes con saldo de puntos y métricas de compra agregadas."""
    return db_clientes.listar_clientes(tenant_id, q=q, incluir_inactivos=incluir_inactivos)


@router.post("/")
def crear(data: ClienteNuevo, tenant_id: str = Depends(get_tenant_id)):
    resultado = db_clientes.crear_cliente(
        tenant_id, data.nombre,
        email=data.email, telefono=data.telefono, pin=data.pin, notas=data.notas,
    )
    if not resultado.get("ok"):
        raise HTTPException(status_code=422, detail=resultado.get("mensaje", "Error al crear el cliente"))
    return resultado


@router.post("/verificar")
def verificar(data: VerificarCliente, tenant_id: str = Depends(get_tenant_id)):
    """Identificación en el POS: busca por número/correo y valida la contraseña
    si el cliente la tiene. 401 con el motivo si falla (el POS muestra el mensaje)."""
    resultado = db_clientes.verificar_cliente(tenant_id, data.identificador, pin=data.pin)
    if not resultado.get("ok"):
        raise HTTPException(status_code=401, detail=resultado.get("mensaje", "Cliente no identificado"))
    return resultado


@router.get("/{cliente_id}")
def obtener(cliente_id: str, tenant_id: str = Depends(get_tenant_id)):
    cliente = db_clientes.obtener_cliente(tenant_id, cliente_id)
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    return cliente


@router.patch("/{cliente_id}")
def actualizar(cliente_id: str, data: ClienteActualizar, tenant_id: str = Depends(get_tenant_id)):
    resultado = db_clientes.actualizar_cliente(
        tenant_id, cliente_id,
        nombre=data.nombre, email=data.email, telefono=data.telefono,
        pin=data.pin, notas=data.notas, activo=data.activo,
    )
    if not resultado.get("ok"):
        status = 404 if resultado.get("tipo") == "no_encontrado" else 422
        raise HTTPException(status_code=status, detail=resultado.get("mensaje", "Error al actualizar"))
    return resultado


@router.delete("/{cliente_id}")
def eliminar(cliente_id: str, tenant_id: str = Depends(get_tenant_id)):
    """Baja lógica: el historial de compras y movimientos se conserva."""
    resultado = db_clientes.eliminar_cliente(tenant_id, cliente_id)
    if not resultado.get("ok"):
        raise HTTPException(status_code=404, detail=resultado.get("mensaje", "Cliente no encontrado"))
    return resultado


@router.post("/{cliente_id}/puntos")
def ajustar_puntos(cliente_id: str, data: AjustePuntos, tenant_id: str = Depends(get_tenant_id)):
    """Dar (+) o quitar (−) puntos manualmente, siempre con motivo (ledger tipo 'ajuste')."""
    resultado = db_puntos.ajustar_puntos(tenant_id, cliente_id, data.puntos, data.concepto)
    if not resultado.get("ok"):
        status = 404 if resultado.get("tipo") == "no_encontrado" else 422
        raise HTTPException(status_code=status, detail=resultado.get("mensaje", "Error en el ajuste"))
    return resultado
