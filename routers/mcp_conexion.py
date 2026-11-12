# ==============================================================================
# backend/routers/mcp_conexion.py
# Router PRIVADO para gestionar las claves del servidor MCP (conector Muse).
#
# Todos los endpoints requieren JWT (Depends(get_tenant_id)): es la parte del
# gestor. El agente IA NUNCA toca este router — solo el endpoint /mcp con su
# X-API-Key (ver muse/auth.py).
#
# El "conector" en Muse no se registra vía API (Meta no la expone): el flujo
# es copiar/pegar un prompt en un chat de Muse, que crea el custom connector
# con la URL del /mcp y la clave. Ese prompt lo arma el frontend con la
# clave recién creada (solo se muestra una vez).
# ==============================================================================

from fastapi import APIRouter, Depends, HTTPException

from database import mcp_claves
from dependencies import get_tenant_id
from schemas.mcp import CrearClaveMcp

router = APIRouter(prefix="/mcp-conexion", tags=["Conector IA (MCP)"])


@router.get("/claves")
def listar_claves(tenant_id: str = Depends(get_tenant_id)):
    """Claves del tenant (sin hash ni clave en claro: solo el prefijo)."""
    return {"claves": mcp_claves.listar_claves(tenant_id)}


@router.post("/claves")
def crear_clave(data: CrearClaveMcp, tenant_id: str = Depends(get_tenant_id)):
    """
    Genera una clave nueva. La respuesta trae la clave en claro UNA sola
    vez: el backend solo guardó su hash. Si el dueño la pierde, revoca y
    genera otra.
    """
    resultado = mcp_claves.crear_clave(tenant_id, data.nombre)
    if resultado.get("ok") is False:
        raise HTTPException(
            status_code=resultado.get("status", 422),
            detail=resultado.get("mensaje", "No se pudo crear la clave"),
        )
    return {"ok": True, "clave": resultado["clave"]}


@router.delete("/claves/{clave_id}")
def revocar_clave(clave_id: int, tenant_id: str = Depends(get_tenant_id)):
    """
    Revoca (soft delete): la clave deja de validar en el próximo request,
    aunque el agente la siga teniendo guardada.
    """
    if not mcp_claves.revocar_clave(clave_id, tenant_id):
        raise HTTPException(status_code=404, detail="Clave no encontrada o ya revocada")
    return {"ok": True}
