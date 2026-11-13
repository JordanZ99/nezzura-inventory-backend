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
from schemas.mcp import CrearClaveMcp, EditarClaveMcp

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
    resultado = mcp_claves.crear_clave(tenant_id, data.nombre, data.nivel)
    if resultado.get("ok") is False:
        raise HTTPException(
            status_code=resultado.get("status", 422),
            detail=resultado.get("mensaje", "No se pudo crear la clave"),
        )
    return {"ok": True, "clave": resultado["clave"]}


@router.patch("/claves/{clave_id}")
def editar_clave(clave_id: int, data: EditarClaveMcp, tenant_id: str = Depends(get_tenant_id)):
    """
    Edita una clave ACTIVA sin tocar su hash: el agente no se reconecta.
    - nivel: cambia los permisos; tools/list se ajusta en el próximo request
      (de 6 a 8 a 10 herramientas).
    - limite_llamadas / limite_escrituras: override por clave; null = vuelve
      al default del nivel.
    """
    filas = mcp_claves.listar_claves(tenant_id)
    if not any(c["id"] == clave_id for c in filas):
        raise HTTPException(status_code=404, detail="Clave no encontrada")

    cambios = []
    if data.nivel is not None:
        if not mcp_claves.cambiar_nivel(clave_id, tenant_id, data.nivel):
            raise HTTPException(status_code=422, detail="Nivel inválido: usa 1, 2 o 3")
        cambios.append("nivel")
    if data.limite_llamadas is not None or data.limite_escrituras is not None:
        if not mcp_claves.actualizar_limites(
            clave_id, tenant_id, data.limite_llamadas, data.limite_escrituras
        ):
            raise HTTPException(
                status_code=422,
                detail="Límites inválidos: deben ser enteros positivos (o null para el default)",
            )
        cambios.append("limites")
    if data.ver_costos is not None:
        if not mcp_claves.cambiar_ver_costos(clave_id, tenant_id, data.ver_costos):
            raise HTTPException(status_code=404, detail="Clave no encontrada")
        cambios.append("ver_costos")
    if not cambios:
        raise HTTPException(
            status_code=422, detail="Nada que editar: envía nivel, límites y/o ver_costos"
        )
    return {"ok": True, "cambiado": cambios}


@router.delete("/claves/{clave_id}")
def eliminar_clave(clave_id: int, tenant_id: str = Depends(get_tenant_id)):
    """
    ELIMINA la clave definitivamente (hard delete): la fila desaparece y el
    agente queda desconectado al instante (401 en el próximo request).
    El Historial del gestor no se toca: lo que el agente ya creó queda
    registrado con su atribución.
    """
    if not mcp_claves.revocar_clave(clave_id, tenant_id):
        raise HTTPException(status_code=404, detail="Clave no encontrada")
    return {"ok": True}
