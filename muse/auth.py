# ==============================================================================
# backend/muse/auth.py
# Autenticación del servidor MCP: header X-API-Key → tenant_id.
#
# Es un middleware ASGI PURO (sin depender de internos de Starlette/FastMCP):
# intercepta los requests http, valida la clave contra mcp_api_keys y guarda
# el tenant_id en un ContextVar que los tools leen. Los scopes que no son
# http (lifespan, websocket) pasan sin tocar la base.
#
# El tenant_id NUNCA lo envía el agente: se deriva de la clave, y todas las
# queries de database/mcp_catalogo.py lo inyectan en el WHERE.
# ==============================================================================

from contextvars import ContextVar

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from database.mcp_claves import validar_clave

# Contexto del request actual. Los tools son síncronos y corren en threadpool:
# anyio copia los contextvars al saltar de async → thread, así que los valores
# fijados acá son visibles dentro de cada tool.
tenant_actual: ContextVar[str] = ContextVar("muse_tenant", default="")
nivel_actual: ContextVar[int] = ContextVar("muse_nivel", default=0)
clave_actual: ContextVar[int] = ContextVar("muse_clave", default=0)
ver_costos_actual: ContextVar[bool] = ContextVar("muse_ver_costos", default=False)


def tenant_de_request() -> str:
    """Tenant validado por el middleware. Vacío = request sin autenticar."""
    return tenant_actual.get()


def nivel_de_request() -> int:
    """Nivel de permiso de la clave (1, 2 o 3). 0 = request sin autenticar."""
    return nivel_actual.get()


def clave_de_request() -> int:
    """Id de la clave usada (para borradores y auditoría). 0 = sin clave."""
    return clave_actual.get()


def ver_costos_de_request() -> bool:
    """True si la clave tiene el permiso de ver costos internos (068).
    False = el costo ni siquiera se selecciona en las queries (defensa a
    nivel SQL, no un filtro de presentación)."""
    return ver_costos_actual.get()


class AutenticacionAPIKey:
    """Middleware ASGI: valida X-API-Key en cada request al servidor MCP."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            clave.decode("latin-1").lower(): valor.decode("latin-1")
            for clave, valor in scope.get("headers", [])
        }
        clave_api = headers.get("x-api-key", "")

        # Solo los POST (llamadas reales) gastan cuota del rate limit;
        # GET (stream SSE) y DELETE (cerrar sesión) se validan sin contar.
        contar = scope.get("method") == "POST"
        tenant_id, nivel, clave_id, error, ver_costos = await run_in_threadpool(
            validar_clave, clave_api, contar
        )

        if error is not None:
            mensajes = {
                401: ("Clave API inválida o revocada. Genera una nueva en "
                      "Ajustes → Negocio → Conectar mi IA."),
                429: "Límite diario de llamadas alcanzado para esta clave.",
            }
            respuesta = JSONResponse(
                {"error": mensajes.get(error, "No autorizado")}, status_code=error
            )
            await respuesta(scope, receive, send)
            return

        tenant_actual.set(tenant_id)
        nivel_actual.set(int(nivel or 1))
        clave_actual.set(int(clave_id or 0))
        ver_costos_actual.set(bool(ver_costos))
        await self.app(scope, receive, send)
