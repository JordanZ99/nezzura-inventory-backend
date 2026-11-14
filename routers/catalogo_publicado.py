# ==============================================================================
# backend/routers/catalogo_publicado.py
# Publish del catálogo desde el MODO LOCAL (plan "local").
#
# Autenticación distinta al resto de la API: NO hay sesión Supabase en el
# dispositivo local — el aparato presenta su JWT de LICENCIA (mismo token
# que /licencias/revalidar) y el tenant_id sale SIEMPRE de ese token
# (tenant_de_licencia), nunca del body.
#
# Endpoints:
#   POST /catalogo-publicado/delta → upsert/borrado de filas del snapshot
#   POST /catalogo-publicado/foto  → foto (bytes) a Cloudinary → URL
#
# El catálogo público se SIRVE por el endpoint de siempre (GET /p/{slug});
# database/catalogo.py detecta el snapshot y lo dibuja desde datos.
# ==============================================================================

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from database.licencias import ErrorLicencia, tenant_de_licencia
from database.catalogo_publicado import aplicar_delta, subir_foto_catalogo

router = APIRouter(prefix="/catalogo-publicado", tags=["Catálogo publicado"])

_bearer = HTTPBearer(auto_error=False)


def _tenant(credentials: HTTPAuthorizationCredentials | None) -> str:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Licencia requerida.")
    try:
        return tenant_de_licencia(credentials.credentials)
    except ErrorLicencia as e:
        raise HTTPException(
            status_code=e.status,
            detail=e.mensaje,
            headers={"x-codigo": e.codigo},
        )


class FilaDelta(BaseModel):
    clave: str                       # '__config__' | 'producto||variacion'
    hash: str
    data: dict | None = None         # None = borrar la fila (delta de borrados)
    orden: int = 0


class SolicitudDelta(BaseModel):
    filas: list[FilaDelta]


@router.post("/delta")
def publicar_delta(data: SolicitudDelta, credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)):
    """Aplica el delta del dispositivo: filas nuevas/cambiadas + borrados.

    Respuesta: {"ok": True, "aplicadas": N, "eliminadas": N, "slug": uuid} —
    el slug lo usa el cliente para armar el link "Ver catálogo" y guardarlo
    junto al snapshot local.
    """
    tenant_id = _tenant(credentials)
    resultado = aplicar_delta(tenant_id, [f.model_dump() for f in data.filas])
    if not resultado.get("ok"):
        raise HTTPException(status_code=resultado.get("status", 422), detail=resultado.get("mensaje"))
    return resultado


@router.post("/foto")
async def publicar_foto(
    foto: UploadFile = File(...),
    nombre: str = Form(""),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
):
    """Sube UNA foto del catálogo local a Cloudinary y devuelve su URL.

    El cliente la llama solo para fotos cuyo hash cambió (delta también en
    fotos): la URL devuelta se reutiliza si la foto no volvió a cambiar.
    """
    tenant_id = _tenant(credentials)
    contents = await foto.read()
    try:
        return subir_foto_catalogo(tenant_id, contents, nombre)
    except ErrorLicencia as e:
        raise HTTPException(
            status_code=e.status,
            detail=e.mensaje,
            headers={"x-codigo": e.codigo},
        )
