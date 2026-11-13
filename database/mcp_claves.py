# ==============================================================================
# backend/database/mcp_claves.py
# Gestión de claves API del servidor MCP (conector para agentes IA como Muse).
#
# Una clave = un agente IA conectado. El flujo:
#   1. El dueño genera la clave desde Ajustes → Negocio (routers/mcp_conexion.py)
#   2. Se guarda SOLO el hash SHA-256 — la clave en claro se muestra una vez
#   3. El agente envía la clave en cada request (header X-API-Key) y el
#      middleware muse/auth.py la valida aquí: resuelve el tenant_id,
#      chequea revocación y aplica el rate limit diario.
#
# El daño máximo de una clave filtrada es LECTURA del catálogo propio
# (productos, precios de venta, fotos): no hay tools de escritura y los
# costos internos jamás se seleccionan (database/mcp_catalogo.py).
# ==============================================================================

import hashlib
import secrets
from datetime import date

from database.conexion import query, execute

# Un negocio no necesita más agentes simultáneos que esto; el tope evita
# crear claves en bucle para evadir el rate limit individual.
MAX_CLAVES_POR_TENANT = 5

# Niveles de permiso de una clave (migrations/066):
#   1 Lectura básica    → catálogo para publicar.
#   2 Lectura avanzada  → + análisis del negocio (requiere plan Plus).
#   3 Escritura básica  → + crear productos (borrador → confirmar).
NIVELES_VALIDOS = (1, 2, 3)

# Rate limit diario POR CLAVE, según su nivel. Los tools de análisis corren
# queries más pesadas que el catálogo, así que una clave nivel 2 gasta más
# por llamada y su cuota es menor.
LIMITE_LLAMADAS_DIARIO = 300
_LIMITE_POR_NIVEL = {1: 300, 2: 100, 3: 50}

# Escrituras por día (solo nivel 3, contadas desde el día 1). Valor inicial
# generoso para probar el agente sin topar; bajarlo después es cambiar ESTA
# constante, sin código nuevo. Además de este límite, confirmar_producto es
# destructiveHint=True: Muse pide aprobación humana en cada llamada.
LIMITE_ESCRITURAS_DIARIO = 1000


def _hashear(clave: str) -> str:
    return hashlib.sha256(clave.encode("utf-8")).hexdigest()


def _generar_clave() -> str:
    """Clave en claro con prefijo reconocible en logs: nezzura_mcp_<48 hex>."""
    return f"nezzura_mcp_{secrets.token_hex(24)}"


def crear_clave(tenant_id: str, nombre: str | None, nivel: int = 1) -> dict:
    """
    Genera una clave nueva para el tenant.
    Devuelve {ok: True, clave} — la clave en claro viaja UNA sola vez al
    frontend; de aquí en adelante solo existe su hash.
    """
    try:
        nivel = int(nivel)
    except (TypeError, ValueError):
        nivel = 1
    if nivel not in NIVELES_VALIDOS:
        return {
            "ok": False,
            "status": 422,
            "mensaje": "Nivel inválido: usa 1 (lectura básica), 2 (lectura avanzada) o 3 (escritura básica).",
        }

    activas = query(
        "SELECT COUNT(*) AS c FROM mcp_api_keys "
        "WHERE tenant_id = %s AND revocada_en IS NULL",
        (tenant_id,),
    )[0]["c"]
    if activas >= MAX_CLAVES_POR_TENANT:
        return {
            "ok": False,
            "status": 409,
            "mensaje": (
                f"Ya tienes {MAX_CLAVES_POR_TENANT} claves activas. "
                "Revoca alguna antes de crear otra."
            ),
        }

    clave = _generar_clave()
    execute(
        "INSERT INTO mcp_api_keys (tenant_id, nombre, key_hash, key_prefix, nivel) "
        "VALUES (%s, %s, %s, %s, %s)",
        (tenant_id, (nombre or "").strip() or "Mi IA", _hashear(clave), clave[:12] + "…", nivel),
    )
    return {"ok": True, "clave": clave}


def listar_claves(tenant_id: str) -> list[dict]:
    """Claves del tenant SIN el hash (la UI muestra solo el prefijo)."""
    return query(
        """SELECT id, nombre, key_prefix, nivel, creada_en, ultimo_uso_en,
                  llamadas_hoy, dia_uso, escrituras_hoy, dia_escrituras,
                  revocada_en
           FROM mcp_api_keys
           WHERE tenant_id = %s
           ORDER BY revocada_en IS NOT NULL, creada_en DESC""",
        (tenant_id,),
    )


def revocar_clave(clave_id: int, tenant_id: str) -> bool:
    """Soft-delete: la clave deja de validar en el próximo request."""
    filas = query(
        "SELECT id FROM mcp_api_keys "
        "WHERE id = %s AND tenant_id = %s AND revocada_en IS NULL",
        (clave_id, tenant_id),
    )
    if not filas:
        return False
    execute("UPDATE mcp_api_keys SET revocada_en = now() WHERE id = %s", (clave_id,))
    return True


def validar_clave(clave: str, contar_uso: bool = True):
    """
    Valida una clave para el middleware del servidor MCP.
    Devuelve (tenant_id, nivel, clave_id, None) si es válida, o
    (None, None, None, status_http) si no:
      401 = no existe o está revocada · 429 = límite diario agotado.

    contar_uso: solo los POST (llamadas reales) gastan cuota; GET (SSE) y
    DELETE (cerrar sesión) se validan sin contar.
    """
    if not clave:
        return None, None, None, 401

    filas = query(
        "SELECT id, tenant_id, nivel, revocada_en, llamadas_hoy, dia_uso "
        "FROM mcp_api_keys WHERE key_hash = %s",
        (_hashear(clave),),
    )
    if not filas or filas[0]["revocada_en"]:
        return None, None, None, 401

    fila = filas[0]
    nivel = int(fila["nivel"] or 1)
    hoy = date.today()
    usadas = fila["llamadas_hoy"] if fila["dia_uso"] == hoy else 0
    limite = _LIMITE_POR_NIVEL.get(nivel, LIMITE_LLAMADAS_DIARIO)
    if contar_uso and usadas >= limite:
        return None, None, None, 429

    if contar_uso:
        execute(
            "UPDATE mcp_api_keys SET llamadas_hoy = %s, dia_uso = %s, "
            "ultimo_uso_en = now() WHERE id = %s",
            (usadas + 1, hoy, fila["id"]),
        )
    return str(fila["tenant_id"]), nivel, int(fila["id"]), None


def contar_escritura(clave_id: int) -> bool:
    """
    Reserva una cuota de escritura para una clave nivel 3.
    Devuelve False si ya alcanzó el límite diario. La escritura SE cuenta
    aunque luego falle la creación: el costo de intentarlo ya se pagó.
    """
    filas = query(
        "SELECT escrituras_hoy, dia_escrituras FROM mcp_api_keys WHERE id = %s",
        (clave_id,),
    )
    if not filas:
        return False
    fila = filas[0]
    hoy = date.today()
    usadas = fila["escrituras_hoy"] if fila["dia_escrituras"] == hoy else 0
    if usadas >= LIMITE_ESCRITURAS_DIARIO:
        return False
    execute(
        "UPDATE mcp_api_keys SET escrituras_hoy = %s, dia_escrituras = %s "
        "WHERE id = %s",
        (usadas + 1, hoy, clave_id),
    )
    return True
