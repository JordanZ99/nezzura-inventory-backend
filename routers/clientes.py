# ==============================================================================
# backend/routers/clientes.py
# Cartera de clientes por tenant (Fase A del sistema de puntos).
# El tenant_id SIEMPRE sale del JWT; nunca del cliente.
# ==============================================================================

from fastapi import APIRouter, Depends, HTTPException, Query

from dependencies import get_tenant_id
from schemas.clientes import (
    ClienteNuevo, ClienteActualizar, AjustePuntos, VerificarCliente,
    EtiquetaNueva, EtiquetaActualizar, SetEtiquetas, InteraccionNueva,
)
from database import clientes as db_clientes
from database import crm as db_crm
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
        cumpleanos=data.cumpleanos, origen=data.origen,
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


# ── CRM: rutas ESTÁTICAS (etiquetas / segmentos / LTV) ────────────────────────
# Declaradas ANTES de /{cliente_id}: FastAPI matchea en orden de declaración y
# si no, "etiquetas"/"segmentos"/"ltv" acabarían tratándose como cliente_id.

@router.get("/etiquetas")
def listar_etiquetas(tenant_id: str = Depends(get_tenant_id)):
    """Catálogo de etiquetas del negocio, con conteo de clientes por cada una."""
    return db_crm.listar_etiquetas(tenant_id)


@router.post("/etiquetas")
def crear_etiqueta(data: EtiquetaNueva, tenant_id: str = Depends(get_tenant_id)):
    resultado = db_crm.crear_etiqueta(tenant_id, data.nombre, color=data.color)
    if not resultado.get("ok"):
        raise HTTPException(status_code=422, detail=resultado.get("mensaje", "Error al crear la etiqueta"))
    return resultado


@router.patch("/etiquetas/{etiqueta_id}")
def actualizar_etiqueta(etiqueta_id: str, data: EtiquetaActualizar, tenant_id: str = Depends(get_tenant_id)):
    resultado = db_crm.actualizar_etiqueta(
        tenant_id, etiqueta_id, nombre=data.nombre, color=data.color,
    )
    if not resultado.get("ok"):
        status = 404 if resultado.get("tipo") == "no_encontrado" else 422
        raise HTTPException(status_code=status, detail=resultado.get("mensaje", "Error al actualizar"))
    return resultado


@router.delete("/etiquetas/{etiqueta_id}")
def eliminar_etiqueta(etiqueta_id: str, tenant_id: str = Depends(get_tenant_id)):
    """Elimina la etiqueta y la desasigna de todos los clientes (CASCADE)."""
    resultado = db_crm.eliminar_etiqueta(tenant_id, etiqueta_id)
    if not resultado.get("ok"):
        raise HTTPException(status_code=404, detail=resultado.get("mensaje", "Etiqueta no encontrada"))
    return resultado


@router.get("/segmentos")
def segmentos(tenant_id: str = Depends(get_tenant_id)):
    """Segmentos dinámicos de la cartera activa: censados al vuelo contra
    compras (ordenes) y saldo de puntos. ids + conteo por segmento."""
    return db_crm.calcular_segmentos(tenant_id)


@router.get("/ltv")
def ltv(horizonte_meses: int = Query(12, ge=1, le=60), tenant_id: str = Depends(get_tenant_id)):
    """LTV con datos observados: por cliente, resumen global y por segmento."""
    return db_crm.calcular_ltv(tenant_id, horizonte_meses=horizonte_meses)


# ── Ficha y baja (clásicos de la cartera) ─────────────────────────────────────
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
        cumpleanos=data.cumpleanos, origen=data.origen,
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


# ── CRM por cliente: etiquetas asignadas y timeline ───────────────────────────

@router.put("/{cliente_id}/etiquetas")
def set_etiquetas(cliente_id: str, data: SetEtiquetas, tenant_id: str = Depends(get_tenant_id)):
    """Sincroniza el set de etiquetas del cliente (replace-all); devuelve las vigentes."""
    resultado = db_crm.asignar_etiquetas(tenant_id, cliente_id, data.etiqueta_ids)
    if not resultado.get("ok"):
        status = 404 if resultado.get("tipo") == "no_encontrado" else 422
        raise HTTPException(status_code=status, detail=resultado.get("mensaje", "Error al asignar etiquetas"))
    return resultado


@router.post("/{cliente_id}/interacciones")
def registrar_interaccion(cliente_id: str, data: InteraccionNueva, tenant_id: str = Depends(get_tenant_id)):
    """Nueva nota de seguimiento: nota | llamada | visita | promo (mig 055)."""
    resultado = db_crm.crear_interaccion(
        tenant_id, cliente_id, data.tipo, data.texto, creado_por=data.creado_por,
    )
    if not resultado.get("ok"):
        status = 404 if resultado.get("tipo") == "no_encontrado" else 422
        raise HTTPException(status_code=status, detail=resultado.get("mensaje", "Error al registrar"))
    return resultado


@router.get("/{cliente_id}/interacciones")
def listar_interacciones(
    cliente_id: str,
    limite: int = Query(50, ge=1, le=200),
    tenant_id: str = Depends(get_tenant_id),
):
    """Timeline de seguimiento del cliente (la ficha también lo incluye)."""
    return db_crm.listar_interacciones(tenant_id, cliente_id, limite=limite)
