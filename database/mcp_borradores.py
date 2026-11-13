# ==============================================================================
# backend/database/mcp_borradores.py
# Borradores de productos para claves de nivel 3 (escritura básica).
#
# El transporte MCP es stateless (cada POST es autosuficiente), así que el
# "producto a medio armar" del agente no puede vivir en memoria: vive AQUÍ,
# con token de un solo uso y expiración a los 15 minutos.
#
# El flujo (patrón de escritura segura en 2 llamadas):
#   1. proponer_producto valida los datos y guarda el borrador — NO toca el
#      inventario. Devuelve al agente el resumen legible + el token.
#   2. confirmar_producto(token) crea el producto DESDE LO GUARDADO en el
#      borrador, no desde lo que el agente re-envíe: la IA no puede alterar
#      nada entre mensajes. El token se consume en el mismo paso (un borrador
#      solo puede confirmarse una vez) y un bucle del agente solo genera
#      borradores basura que expiran solos.
# ==============================================================================

import secrets
from datetime import timedelta

from database.conexion import execute, query

# Vida útil del borrador: sobra para una conversación; al expirar se descarta
# (la primera limpieza que pase por aquí lo borra).
TTL_MINUTOS = 15


def limpiar_expirados() -> None:
    """Barato y puntual: se llama al crear cada borrador nuevo."""
    execute("DELETE FROM mcp_borradores WHERE expira_en < now()")


def crear_borrador(tenant_id: str, clave_id: int, datos: dict) -> dict:
    """Guarda el borrador validado. Devuelve {token, expira_en}."""
    limpiar_expirados()
    token = secrets.token_urlsafe(24)
    filas = query(
        "INSERT INTO mcp_borradores (token, tenant_id, clave_id, datos, expira_en) "
        "VALUES (%s, %s, %s, %s::jsonb, now() + %s * interval '1 minute') "
        "RETURNING expira_en",
        (token, tenant_id, clave_id, execute_json(datos), TTL_MINUTOS),
    )
    return {"token": token, "expira_en": filas[0]["expira_en"]}


def obtener_borrador(tenant_id: str, clave_id: int, token: str) -> dict | None:
    """Datos del borrador si existe, no expiró y pertenece a esta clave+tenant."""
    filas = query(
        "SELECT datos FROM mcp_borradores "
        "WHERE token = %s AND tenant_id = %s AND clave_id = %s "
        "AND expira_en >= now()",
        (token, tenant_id, clave_id),
    )
    return filas[0]["datos"] if filas else None


def consumir_borrador(tenant_id: str, clave_id: int, token: str) -> dict | None:
    """
    Toma los datos del borrador y lo ELIMINA en el mismo paso (atómico).
    Devuelve None si ya no existe (expiró, se usó antes o no pertenece).
    """
    filas = query(
        "DELETE FROM mcp_borradores "
        "WHERE token = %s AND tenant_id = %s AND clave_id = %s "
        "AND expira_en >= now() RETURNING datos",
        (token, tenant_id, clave_id),
    )
    return filas[0]["datos"] if filas else None


def execute_json(datos: dict) -> str:
    """Serializa el dict a texto JSONB-safe (jsonb del INSERT lo parsea)."""
    import json
    return json.dumps(datos, ensure_ascii=False, default=str)
