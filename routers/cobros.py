# ==============================================================================
# backend/routers/cobros.py
# Cobros del giro 'servicios' (migración 061): cartera por cobrar con
# mensualidades (cuotas) y trabajos extra, con abonos parciales.
# El tenant_id SIEMPRE sale del JWT; nunca del request.
#
# Orden de rutas: las ESTÁTICAS (/resumen, /generar, /cuotas...) se declaran
# ANTES de /{cobro_id} — FastAPI matchea en orden y sin esto "resumen"/"cuotas"
# acabarían tratadas como un id de cobro.
# ==============================================================================

from fastapi import APIRouter, Depends, HTTPException, Query

from dependencies import get_tenant_id
from schemas.cobros import (
    AbonoEditar, AbonoNuevo, CobroActualizar, CobroExtraNuevo, CuotaActualizar, CuotaNueva,
    GenerarPeriodo,
)
from database import cobros as db_cobros

router = APIRouter(prefix="/cobros", tags=["Cobros"])


def _revisar(resultado: dict, mensaje: str) -> dict:
    """404 si el tipo es 'no_encontrado', 422 en validación — patrón clientes.py."""
    if not resultado.get("ok"):
        status = 404 if resultado.get("tipo") == "no_encontrado" else 422
        raise HTTPException(status_code=status, detail=resultado.get("mensaje", mensaje))
    return resultado


# ── Rutas ESTÁTICAS (antes de /{cobro_id}) ────────────────────────────────────

@router.get("/resumen")
def resumen(
    periodo: str = Query(None, pattern=r"^\d{4}-\d{2}$"),
    tenant_id: str = Depends(get_tenant_id),
):
    """KPIs del dashboard financiero: pendiente total, aging (0-30/31-60/61-90/+90
    días de vencido), cobrado del periodo y deudores ordenados por saldo."""
    return db_cobros.resumen(tenant_id, periodo=periodo)


@router.post("/generar")
def generar(data: GenerarPeriodo, tenant_id: str = Depends(get_tenant_id)):
    """Genera los cobros del periodo desde las cuotas activas. IDEMPOTENTE:
    el cobro cuota+mes que ya existe no se duplica."""
    resultado = db_cobros.generar_cobros(tenant_id, data.periodo)
    _revisar(resultado, "Error al generar los cobros")
    return resultado


@router.get("/cuotas")
def listar_cuotas(
    solo_activas: bool = False,
    tenant_id: str = Depends(get_tenant_id),
):
    """Suscripciones mensuales del negocio (con nombre del cliente)."""
    return db_cobros.listar_cuotas(tenant_id, solo_activas=solo_activas)


@router.post("/cuotas")
def crear_cuota(data: CuotaNueva, tenant_id: str = Depends(get_tenant_id)):
    resultado = db_cobros.crear_cuota(
        tenant_id, data.cliente_id, data.concepto, data.monto,
        dia_cobro=data.dia_cobro, notas=data.notas,
        frecuencia=data.frecuencia, dia_semana=data.dia_semana,
        monto_meta=data.monto_meta, tipo=data.tipo, costo=data.costo,
    )
    _revisar(resultado, "Error al crear la cuota")
    return resultado


@router.patch("/cuotas/{cuota_id}")
def actualizar_cuota(cuota_id: str, data: CuotaActualizar, tenant_id: str = Depends(get_tenant_id)):
    resultado = db_cobros.actualizar_cuota(
        tenant_id, cuota_id,
        cliente_id=data.cliente_id, concepto=data.concepto, monto=data.monto,
        dia_cobro=data.dia_cobro, activo=data.activo, notas=data.notas,
        frecuencia=data.frecuencia, dia_semana=data.dia_semana,
        monto_meta=data.monto_meta, tipo=data.tipo, costo=data.costo,
    )
    _revisar(resultado, "Error al actualizar la cuota")
    return resultado


@router.delete("/cuotas/{cuota_id}")
def eliminar_cuota(cuota_id: str, tenant_id: str = Depends(get_tenant_id)):
    """Baja de la cuota: los cobros ya generados se conservan (cuota_id pasa a
    NULL por el ON DELETE SET NULL); el dinero histórico nunca se pierde."""
    resultado = db_cobros.eliminar_cuota(tenant_id, cuota_id)
    _revisar(resultado, "Cuota no encontrada")
    return resultado


# ── Cobros ────────────────────────────────────────────────────────────────────

@router.get("/")
def listar(
    periodo: str = Query(None, pattern=r"^\d{4}-\d{2}$"),
    cliente_id: str = None,
    solo_pendientes: bool = False,
    tenant_id: str = Depends(get_tenant_id),
):
    """Lista de cobros (incluye pagados). Con periodo: solo ese mes; con
    solo_pendientes: solo saldos abiertos (la cartera por cobrar)."""
    return db_cobros.listar_cobros(
        tenant_id, periodo=periodo, cliente_id=cliente_id,
        solo_pendientes=solo_pendientes,
    )


@router.post("/")
def crear_extra(data: CobroExtraNuevo, tenant_id: str = Depends(get_tenant_id)):
    """Alta de un trabajo puntual (dev/consultoría/extra) con plan de pagos
    opcional. Los cobros mensuales se generan con POST /cobros/generar."""
    resultado = db_cobros.crear_cobro_extra(
        tenant_id, data.cliente_id, data.concepto, data.monto,
        vencimiento=data.vencimiento, programa=data.programa, notas=data.notas,
    )
    _revisar(resultado, "Error al crear el cobro")
    return resultado


@router.get("/{cobro_id}")
def obtener(cobro_id: str, tenant_id: str = Depends(get_tenant_id)):
    """Detalle de un cobro (datos, abonos y estado derivado)."""
    filas = db_cobros.listar_cobros(tenant_id)
    cobro = next((c for c in filas if c["id"] == cobro_id), None)
    if not cobro:
        raise HTTPException(status_code=404, detail="Cobro no encontrado")
    return cobro


@router.patch("/{cobro_id}")
def actualizar(cobro_id: str, data: CobroActualizar, tenant_id: str = Depends(get_tenant_id)):
    """Edita concepto/monto/vencimiento/programa/notas. El dinero (`pagos`)
    entra SOLO por POST /{id}/pagos y se corrige con DELETE /{id}/pagos/{indice}."""
    resultado = db_cobros.actualizar_cobro(
        tenant_id, cobro_id,
        concepto=data.concepto, monto=data.monto, vencimiento=data.vencimiento,
        programa=data.programa, notas=data.notas,
    )
    _revisar(resultado, "Error al actualizar el cobro")
    return resultado


@router.delete("/{cobro_id}")
def eliminar(cobro_id: str, tenant_id: str = Depends(get_tenant_id)):
    resultado = db_cobros.eliminar_cobro(tenant_id, cobro_id)
    _revisar(resultado, "Cobro no encontrado")
    return resultado


@router.post("/{cobro_id}/pagos")
def abonar(cobro_id: str, data: AbonoNuevo, tenant_id: str = Depends(get_tenant_id)):
    """Registra un abono (parcial o total) y recalcula saldo/estado al vuelo.
    Con cascada=true y cobro de cuenta: el excedente se reparte a los
    siguientes cobros pendientes de la misma cuota (mig 081)."""
    resultado = db_cobros.agregar_pago(
        tenant_id, cobro_id, data.monto, metodo=data.metodo, fecha=data.fecha,
        cascada=data.cascada,
    )
    _revisar(resultado, "Error al registrar el abono")
    return resultado


@router.patch("/{cobro_id}/pagos/{indice}")
def editar_abono(cobro_id: str, indice: int, data: AbonoEditar, tenant_id: str = Depends(get_tenant_id)):
    """Corrige un abono EN SU POSICIÓN del ledger (monto/metodo/fecha);
    None = conservar el valor actual del abono."""
    resultado = db_cobros.editar_pago(
        tenant_id, cobro_id, indice, data.monto, metodo=data.metodo, fecha=data.fecha,
    )
    _revisar(resultado, "Error al editar el abono")
    return resultado


@router.delete("/{cobro_id}/pagos/{indice}")
def quitar_abono(cobro_id: str, indice: int, tenant_id: str = Depends(get_tenant_id)):
    """Corrección de errores: quita un abono por posición (0-indexed)."""
    resultado = db_cobros.quitar_pago(tenant_id, cobro_id, indice)
    _revisar(resultado, "Error al quitar el abono")
    return resultado
