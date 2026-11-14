# ==============================================================================
# backend/database/licencias.py
# Licencias de dispositivo para el Modo Local (plan "local").
#
# Firma de tokens: JWT HS256 con LICENSE_SECRET. Si la variable no está
# configurada se usa un secreto aleatorio EN MEMORIA (válida, pero cada
# restart del backend obliga a re-activar dispositivos: sirve para dev).
# OPERACIÓN SERIA: definir LICENSE_SECRET en Render/producción (32+ bytes).
# ==============================================================================

import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt

from database.conexion import execute, query

# ---------------------------------------------------------------------------
# Parámetros de la licencia (env-configurables; defaults del modelo acordado)
# ---------------------------------------------------------------------------
DIAS_VIGENCIA = int(os.getenv("LICENCIA_DIAS", "14"))          # gracia offline
MAX_DISPOSITIVOS = int(os.getenv("LICENCIA_MAX_DISPOSITIVOS", "3"))

# Planes con derecho a Modo Local (hoy solo 'local'; la estructura admite más)
PLANES_CON_LOCAL = {"local"}

# El secreto: variable de entorno LICENSE_SECRET; fallback en memoria (dev).
_CARGA = None  # placeholder del secreto efectivo


def _secreto() -> bytes:
    global _CARGA
    if _CARGA is None:
        env = os.getenv("LICENSE_SECRET", "").strip()
        if env:
            _CARGA = env.encode()
        else:
            print(
                "AVISO LICENSE_SECRET: no está configurada. Se usa un secreto "
                "aleatorio en memoria — los dispositivos deberán re-activarse "
                "en cada restart del backend."
            )
            _CARGA = secrets.token_bytes(32)
    return _CARGA


def _plan_tenant(tenant_id: str) -> str | None:
    """Plan del tenant desde tenants. Devuelve None si el tenant no existe."""
    try:
        filas = query("SELECT plan FROM tenants WHERE id = %s", (tenant_id,))
        if not filas:
            return None
        return filas[0].get("plan") or "basico"
    except Exception as e:
        print(f"Error leyendo plan del tenant {tenant_id}: {e}")
        return "basico"


def _empresa_tenant(tenant_id: str) -> dict:
    """Nombre/logo del tenant para mostrarlos en la activación sin otra llamada."""
    try:
        filas = query(
            "SELECT empresa, logo FROM tenants WHERE id = %s", (tenant_id,)
        )
        if filas:
            return {
                "empresa": filas[0].get("empresa") or "",
                "logo": filas[0].get("logo") or "",
            }
    except Exception:
        pass
    return {"empresa": "", "logo": ""}


# ---------------------------------------------------------------------------
# Firma / verificación del token de licencia
# ---------------------------------------------------------------------------
def firmar_licencia(tenant_id: str, device_id: str, plan: str, nombre: str) -> dict:
    """Firma un JWT de licencia de dispositivo y devuelve el token + exp."""
    ahora = datetime.now(timezone.utc)
    exp = ahora + timedelta(days=DIAS_VIGENCIA)
    payload = {
        "sub": device_id,
        "tenant_id": tenant_id,
        "plan": plan,
        "device_nombre": nombre,
        "typ": "licencia",
        "iat": int(ahora.timestamp()),
        "exp": int(exp.timestamp()),
    }
    token = jwt.encode(payload, _secreto(), algorithm="HS256")
    return {"token": token, "exp": int(exp.timestamp())}


def verificar_licencia(token: str) -> dict:
    """Verifica firma y vigencia de una licencia. Levanta ValueError con mensaje.

    La exp vencida NO es error aquí: revalidar es justamente el remedio.
    Devuelve el payload. Un token con exp vencida devuelve el payload con
    'exp_vencida': True.
    """
    try:
        payload = jwt.decode(
            token,
            _secreto(),
            algorithms=["HS256"],
            options={"verify_exp": False},  # la exp vencida se maneja aparte
        )
    except jwt.PyJWTError as e:
        raise ValueError("Licencia inválida.") from e

    if payload.get("typ") != "licencia":
        raise ValueError("Token no es una licencia de dispositivo.")

    ahora = datetime.now(timezone.utc).timestamp()
    return {**payload, "exp_vencida": payload.get("exp", 0) <= ahora}


# ---------------------------------------------------------------------------
# Dispositivos (CRUD sobre dispositivos_licencia)
# ---------------------------------------------------------------------------
def _activos_tenant(tenant_id: str) -> list[dict]:
    return query(
        "SELECT * FROM dispositivos_licencia WHERE tenant_id = %s ORDER BY creado_en",
        (tenant_id,),
    )


def activar_dispositivo(tenant_id: str, device_id: str, nombre: str) -> dict:
    """Registra/actualiza el dispositivo y EMITE su licencia.

    - Re-activación del mismo device_id (borró localStorage, reinstaló la
      PWA): upsert. NO cuenta como dispositivo nuevo.
    - Límite MAX_DISPOSITIVOS por tenant (equipos activos), contando el nuevo.
    - El plan se lee AHORA: si el tenant ya no es 'local', no se emite.
    """
    plan = _plan_tenant(tenant_id)
    if plan is None:
        return {"ok": False, "mensaje": "Tenant inexistente."}
    if plan not in PLANES_CON_LOCAL:
        return {
            "ok": False,
            "codigo": "PLAN_SIN_LOCAL",
            "mensaje": f"El plan '{plan}' no incluye el Modo Local.",
        }

    existentes = _activos_tenant(tenant_id)
    lo_tiene = any(d.get("device_id") == device_id for d in existentes)
    if not lo_tiene and len(existentes) >= MAX_DISPOSITIVOS:
        return {
            "ok": False,
            "codigo": "LIMITE_DISPOSITIVOS",
            "mensaje": f"Ya tienes {MAX_DISPOSITIVOS} dispositivos activos. "
                       "Revoca uno desde la configuración para activar este.",
        }

    execute(
        """
        INSERT INTO dispositivos_licencia (tenant_id, device_id, nombre, ultimo_uso)
        VALUES (%s, %s, %s, now())
        ON CONFLICT (tenant_id, device_id)
        DO UPDATE SET nombre = EXCLUDED.nombre, activo = TRUE, ultimo_uso = now()
        """,
        (tenant_id, device_id, nombre),
    )

    licencia = firmar_licencia(tenant_id, device_id, plan, nombre)
    return {
        "ok": True,
        **licencia,
        "plan": plan,
        **_empresa_tenant(tenant_id),
        "expira_en_dias": DIAS_VIGENCIA,
    }


def revalidar_licencia(token: str) -> dict:
    """Revalida una licencia: firma válida + dispositivo activo + plan vigente.

    Es el ÚNICO control server-side del modo offline: si el dueño revocó el
    dispositivo o el plan cambió, la respuesta lo bloquea. Si todo va bien
    devuelve un token NUEVO (+DIAS_VIGENCIA) que el cliente guarda.
    """
    try:
        payload = verificar_licencia(token)
    except ValueError as e:
        return {"ok": False, "codigo": "LICENCIA_INVALIDA", "mensaje": str(e)}

    tenant_id = payload.get("tenant_id")
    device_id = payload.get("sub")
    if not tenant_id or not device_id:
        return {"ok": False, "codigo": "LICENCIA_INVALIDA", "mensaje": "Licencia incompleta."}

    plan_actual = _plan_tenant(tenant_id)
    if plan_actual not in PLANES_CON_LOCAL:
        return {
            "ok": False,
            "codigo": "PLAN_SIN_LOCAL",
            "mensaje": "Tu plan ya no incluye el Modo Local.",
        }

    filas = query(
        "SELECT activo FROM dispositivos_licencia WHERE tenant_id = %s AND device_id = %s",
        (tenant_id, device_id),
    )
    if not filas or not filas[0].get("activo"):
        return {
            "ok": False,
            "codigo": "DISPOSITIVO_REVOCADO",
            "mensaje": "Este dispositivo fue revocado desde la configuración.",
        }

    execute(
        "UPDATE dispositivos_licencia SET ultimo_uso = now() "
        "WHERE tenant_id = %s AND device_id = %s",
        (tenant_id, device_id),
    )

    licencia = firmar_licencia(tenant_id, device_id, plan_actual, payload.get("device_nombre", ""))
    return {
        "ok": True,
        **licencia,
        "plan": plan_actual,
        **_empresa_tenant(tenant_id),
        "expira_en_dias": DIAS_VIGENCIA,
    }


class ErrorLicencia(Exception):
    """Guard de licencia para endpoints del Modo Local: trae el status HTTP
    y el código listos para mapear a HTTPException en el router."""

    def __init__(self, status: int, mensaje: str, codigo: str = "LICENCIA_INVALIDA"):
        self.status = status
        self.mensaje = mensaje
        self.codigo = codigo
        super().__init__(mensaje)


def tenant_de_licencia(token: str) -> str:
    """Valida un JWT de LICENCIA (HS256 del dispositivo) y devuelve tenant_id.

    Es el guard de los endpoints que el Modo Local llama con su licencia
    (publish de catálogo): mismos checks que `revalidar_licencia` (firma +
    plan + dispositivo activo) pero SIN renovar el token. Lanza
    ErrorLicencia con el status HTTP listo.
    """
    try:
        payload = verificar_licencia(token)
    except ValueError as e:
        raise ErrorLicencia(401, str(e))

    tenant_id = payload.get("tenant_id")
    device_id = payload.get("sub")
    if not tenant_id or not device_id:
        raise ErrorLicencia(401, "Licencia incompleta.")

    plan_actual = _plan_tenant(tenant_id)
    if plan_actual not in PLANES_CON_LOCAL:
        raise ErrorLicencia(403, "Tu plan ya no incluye el Modo Local.", "PLAN_SIN_LOCAL")

    filas = query(
        "SELECT activo FROM dispositivos_licencia WHERE tenant_id = %s AND device_id = %s",
        (tenant_id, device_id),
    )
    if not filas or not filas[0].get("activo"):
        raise ErrorLicencia(
            403, "Este dispositivo fue revocado desde la configuración.", "DISPOSITIVO_REVOCADO"
        )

    execute(
        "UPDATE dispositivos_licencia SET ultimo_uso = now() "
        "WHERE tenant_id = %s AND device_id = %s",
        (tenant_id, device_id),
    )
    return tenant_id


def listar_dispositivos(tenant_id: str) -> dict:
    """Lista del dueño: qué aparatos tienen licencia (para revocar uno)."""
    filas = _activos_tenant(tenant_id)
    dispositivos = [
        {
            "device_id": f["device_id"],
            "nombre": f["nombre"],
            "activo": f["activo"],
            "creado_en": f["creado_en"].isoformat() if f.get("creado_en") else None,
            "ultimo_uso": f["ultimo_uso"].isoformat() if f.get("ultimo_uso") else None,
        }
        for f in filas
    ]
    return {"ok": True, "dispositivos": dispositivos, "max_dispositivos": MAX_DISPOSITIVOS}


def revocar_dispositivo(tenant_id: str, device_id: str) -> dict:
    """Revoca (soft-delete) un dispositivo del mismo tenant."""
    execute(
        "UPDATE dispositivos_licencia SET activo = FALSE "
        "WHERE tenant_id = %s AND device_id = %s",
        (tenant_id, device_id),
    )
    return {"ok": True, "device_id": device_id}
