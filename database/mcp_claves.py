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
# El daño de una clave filtrada depende de sus ROLES (081): una clave
# publicadora solo expone LECTURA del catálogo propio (productos, precios de
# venta, fotos) y los costos internos jamás se seleccionan; una clave con
# roles de escritura además queda limitada por la cuota diaria de escrituras
# y el patrón borrador→confirmar con aprobación humana.
# ==============================================================================

import hashlib
import secrets
from datetime import date

from database.conexion import query, execute
from muse.roles import normalizar_roles, nivel_de_roles, ROLES

# Un negocio no necesita más agentes simultáneos que esto; el tope evita
# crear claves en bucle para evadir el rate limit individual.
MAX_CLAVES_POR_TENANT = 5

# Roles funcionales de una clave (migrations/081 + muse/roles.py): el dueño
# elige QUÉ TRABAJO hace el agente y la clave ve solo las tools de esos roles.
# El NIVEL se deriva de los roles (máximo) y sigue alimentando los rate
# limits y el permiso 'ver costos'.
ROLES_VALIDOS = tuple(ROLES)

# Rate limit diario POR CLAVE, según su nivel. La escalera: más permisos,
# menos margen. El nivel 1 es generoso a propósito (1000): el catálogo es
# barato de leer y el caso de uso principal (auditar el catálogo, iterar
# listados paginados de a 50) no debe topar. El dueño puede apretar cada
# clave con el override por clave (migrations/067).
LIMITE_LLAMADAS_DIARIO = 1000
_LIMITE_POR_NIVEL = {1: 1000, 2: 300, 3: 100}

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


def crear_clave(tenant_id: str, nombre: str | None, roles=None) -> dict:
    """
    Genera una clave nueva para el tenant con sus ROLES funcionales (081).
    Devuelve {ok: True, clave} — la clave en claro viaja UNA sola vez al
    frontend; de aquí en adelante solo existe su hash.
    """
    roles_norm = normalizar_roles(roles)
    if roles_norm is None:
        return {
            "ok": False,
            "status": 422,
            "mensaje": (
                "Roles inválidos. Usa: " + ", ".join(ROLES_VALIDOS) + "."
            ),
        }
    nivel = nivel_de_roles(roles_norm)

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
        "INSERT INTO mcp_api_keys (tenant_id, nombre, key_hash, key_prefix, nivel, roles) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            tenant_id, (nombre or "").strip() or "Mi IA", _hashear(clave),
            clave[:12] + "…", nivel, ",".join(roles_norm),
        ),
    )
    return {"ok": True, "clave": clave}


def listar_claves(tenant_id: str) -> list[dict]:
    """Claves del tenant SIN el hash (la UI muestra solo el prefijo).
    Solo activas: eliminar es ahora un hard delete, ya no hay revocadas.
    `roles` llega ya como lista (el frontend pinta los toggles con ella)."""
    filas = query(
        """SELECT id, nombre, key_prefix, nivel, roles, creada_en, ultimo_uso_en,
                  llamadas_hoy, dia_uso, escrituras_hoy, dia_escrituras,
                  limite_llamadas_diarias, limite_escrituras_diarias, ver_costos
           FROM mcp_api_keys
           WHERE tenant_id = %s
           ORDER BY creada_en DESC""",
        (tenant_id,),
    )
    for fila in filas:
        crudos = (fila.get("roles") or "").strip()
        fila["roles"] = [r for r in crudos.split(",") if r.strip()] or None
    return filas


def revocar_clave(clave_id: int, tenant_id: str) -> bool:
    """Elimina la clave DEFINITIVAMENTE (hard delete).
    - La clave deja de validar al instante: la validación busca el hash y la
      fila ya no existe → 401.
    - Libera el slot de las MAX_CLAVES_POR_TENANT.
    - El Historial (registro_actividad) NO se toca: las altas que hizo el
      agente siguen ahí con su atribución.
    - Borradores huérfanos de la clave: no confirmables (pertenecen a una
      clave muerta) y expiran solos en minutos."""
    filas = query(
        "SELECT id FROM mcp_api_keys WHERE id = %s AND tenant_id = %s",
        (clave_id, tenant_id),
    )
    if not filas:
        return False
    execute("DELETE FROM mcp_api_keys WHERE id = %s AND tenant_id = %s", (clave_id, tenant_id))
    return True


def cambiar_roles(clave_id: int, tenant_id: str, roles) -> bool:
    """
    Cambia los ROLES de una clave ACTIVA sin tocar su hash: el agente no se
    reconecta y tools/list se ajusta en el próximo request. El nivel se
    recalcula de los roles (máximo) porque alimenta los rate limits.
    """
    roles_norm = normalizar_roles(roles)
    if roles_norm is None:
        return False
    filas = query(
        "SELECT id FROM mcp_api_keys WHERE id = %s AND tenant_id = %s",
        (clave_id, tenant_id),
    )
    if not filas:
        return False
    execute(
        "UPDATE mcp_api_keys SET roles = %s, nivel = %s WHERE id = %s",
        (",".join(roles_norm), nivel_de_roles(roles_norm), clave_id),
    )
    return True


def actualizar_limites(clave_id: int, tenant_id: str,
                       limite_llamadas: int | None = None,
                       limite_escrituras: int | None = None) -> bool:
    """
    Override de límites por clave. 0 = vuelve al default del nivel (limpia la
    columna); >0 = fija el override. Los cambios no son retroactivos: los
    contadores diarios ya gastados siguen contando.
    """
    def _limpiar(valor):
        if valor is None:
            return None
        try:
            n = int(valor)
        except (TypeError, ValueError):
            return False
        if n == 0:
            return None  # 0 = volver al default del nivel
        return n if n > 0 else False

    llamadas = _limpiar(limite_llamadas)
    escrituras = _limpiar(limite_escrituras)
    if llamadas is False or escrituras is False:
        return False

    filas = query(
        "SELECT id FROM mcp_api_keys WHERE id = %s AND tenant_id = %s",
        (clave_id, tenant_id),
    )
    if not filas:
        return False
    execute(
        "UPDATE mcp_api_keys SET limite_llamadas_diarias = %s, "
        "limite_escrituras_diarias = %s WHERE id = %s",
        (llamadas, escrituras, clave_id),
    )
    return True


def cambiar_ver_costos(clave_id: int, tenant_id: str, ver_costos: bool) -> bool:
    """
    Permite o prohíbe que el agente de esta clave vea costos internos
    (costo promedio de los lotes y margen en fichas/listados/análisis).
    Independiente del nivel: el nivel controla QUÉ ACCIONES puede hacer el
    agente; este switch controla QUÉ DATOS sensibles puede leer.
    La red de seguridad no cambia con esto: datos_para_publicar JAMÁS
    devuelve costos (es el canal público, Marketplace).
    """
    filas = query(
        "SELECT id FROM mcp_api_keys WHERE id = %s AND tenant_id = %s",
        (clave_id, tenant_id),
    )
    if not filas:
        return False
    execute("UPDATE mcp_api_keys SET ver_costos = %s WHERE id = %s", (bool(ver_costos), clave_id))
    return True


def validar_clave(clave: str, contar_uso: bool = True):
    """
    Valida una clave para el middleware del servidor MCP.
    Devuelve (tenant_id, nivel, clave_id, None, ver_costos, roles) si es
    válida, o (None, None, None, status_http, False, None) si no:
      401 = no existe o está revocada · 429 = límite diario agotado.
    roles: tupla de roles activos de la clave (081). None/'' = todas las
    tools (defensa para claves pre-081 sin migrar).

    contar_uso: solo los POST (llamadas reales) gastan cuota; GET (SSE) y
    DELETE (cerrar sesión) se validan sin contar.
    """
    if not clave:
        return None, None, None, 401, False, None

    filas = query(
        "SELECT id, tenant_id, nivel, roles, revocada_en, llamadas_hoy, dia_uso, "
        "limite_llamadas_diarias, ver_costos FROM mcp_api_keys WHERE key_hash = %s",
        (_hashear(clave),),
    )
    if not filas or filas[0]["revocada_en"]:
        return None, None, None, 401, False, None

    fila = filas[0]
    nivel = int(fila["nivel"] or 1)
    hoy = date.today()
    usadas = fila["llamadas_hoy"] if fila["dia_uso"] == hoy else 0
    # Override por clave (NULL = default del nivel, migrations/067)
    limite = fila["limite_llamadas_diarias"] or _LIMITE_POR_NIVEL.get(nivel, LIMITE_LLAMADAS_DIARIO)
    if contar_uso and usadas >= limite:
        return None, None, None, 429, False, None

    if contar_uso:
        execute(
            "UPDATE mcp_api_keys SET llamadas_hoy = %s, dia_uso = %s, "
            "ultimo_uso_en = now() WHERE id = %s",
            (usadas + 1, hoy, fila["id"]),
        )
    # 5ª posición: el agente de esta clave puede ver costos internos (068).
    # 6ª: roles activos (081); vacío = todas (compatibilidad pre-migración).
    crudos_roles = (fila.get("roles") or "").strip()
    roles = tuple(r.strip() for r in crudos_roles.split(",") if r.strip()) or None
    return str(fila["tenant_id"]), nivel, int(fila["id"]), None, bool(fila["ver_costos"]), roles


def contar_escritura(clave_id: int) -> bool:
    """
    Reserva una cuota de escritura para una clave nivel 3.
    Devuelve False si ya alcanzó el límite diario. La escritura SE cuenta
    aunque luego falle la creación: el costo de intentarlo ya se pagó.
    """
    filas = query(
        "SELECT escrituras_hoy, dia_escrituras, limite_escrituras_diarias "
        "FROM mcp_api_keys WHERE id = %s",
        (clave_id,),
    )
    if not filas:
        return False
    fila = filas[0]
    hoy = date.today()
    usadas = fila["escrituras_hoy"] if fila["dia_escrituras"] == hoy else 0
    # Override por clave (NULL = default global, migrations/067)
    limite = fila["limite_escrituras_diarias"] or LIMITE_ESCRITURAS_DIARIO
    if usadas >= limite:
        return False
    execute(
        "UPDATE mcp_api_keys SET escrituras_hoy = %s, dia_escrituras = %s "
        "WHERE id = %s",
        (usadas + 1, hoy, clave_id),
    )
    return True
