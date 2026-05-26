# ==============================================================================
# backend/routers/inventario.py
# Endpoints de inventario, lotes y productos.
# ==============================================================================

from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Depends
from pydantic import BaseModel
from typing import Optional
from dependencies import get_tenant_id
import os, uuid, re
import cloudinary
import cloudinary.uploader
import cloudinary.api


cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET")
)

from database.lotes import (
    get_lotes, get_productos_meta, get_inventario_consolidado,
    get_detalle_lotes, agregar_lote, actualizar_producto, actualizar_lote
)
from database.conexion import query
from dependencies import validar_sesion

router = APIRouter(prefix="/inventario", tags=["Inventario"])

# --- Modelos Pydantic (validan los datos que llegan) ---

class NuevoProducto(BaseModel):
    producto    : str
    descripcion : str  = ""
    costo       : float
    precio_venta: float
    stock       : int
    imagen      : str  = "No hay foto"
    categoria   : list[str]  = ["General"]

class Restock(BaseModel):
    producto    : str
    costo       : float
    precio_venta: float
    stock       : int

class ActualizarProducto(BaseModel):
    descripcion: str
    imagen     : str
    estado     : str
    categoria  : list[str]

class ActualizarLote(BaseModel):
    costo       : float
    precio_venta: float
    stock       : int


# --- Endpoints ---

@router.get("/me")
def obtener_mi_perfil(tenant_id: str = Depends(get_tenant_id)):
    """
    Este endpoint está protegido. 
    Lee el JWT del Header, lo decodifica y devuelve el ID del usuario.
    """
    return {"tenant_id": tenant_id}


@router.get("/")
def listar_inventario(tenant_id: str = Depends(get_tenant_id)):
    """Vista consolidada: un producto = una fila con stock total."""
    resultado = get_inventario_consolidado(tenant_id)
    return resultado


@router.get("/lotes")
def listar_lotes(tenant_id: str = Depends(get_tenant_id)):
    """Todos los lotes activos con detalle de costo y stock por lote."""
    resultado = get_lotes(tenant_id)
    return resultado


@router.get("/lotes/{producto}")
def lotes_por_producto(producto: str, tenant_id: str = Depends(get_tenant_id)):
    """Lotes activos de un producto específico."""
    return get_detalle_lotes(producto, tenant_id)


@router.get("/productos")
def listar_productos(tenant_id: str = Depends(get_tenant_id)):
    """Metadatos de todos los productos."""
    resultado = get_productos_meta(tenant_id)
    return resultado


@router.post("/")
def crear_producto(data: NuevoProducto, tenant_id: str = Depends(get_tenant_id)):
    """Registra un producto nuevo con su primer lote."""
    return agregar_lote(
        data.producto, data.descripcion,
        data.costo, data.precio_venta,
        data.stock, data.imagen, data.categoria,
        tenant_id
    )


@router.post("/restock")
def restockear(data: Restock, tenant_id: str = Depends(get_tenant_id)):
    """Añade stock a un producto existente (nuevo lote o suma al existente)."""
    return agregar_lote(
        producto=data.producto,
        descripcion="",
        costo=data.costo,
        precio_venta=data.precio_venta,
        stock=data.stock,
        tenant_id=tenant_id
    )


@router.post("/foto/{producto}")
async def subir_foto(producto: str, foto: UploadFile = File(...), tenant_id: str = Depends(get_tenant_id)):
    """
    Sube la foto de un producto a Cloudinary y devuelve la URL segura.

    Validaciones de seguridad:
    - El archivo no debe exceder 1 MB (1024 KB). Esto es una red de seguridad,
      ya que el frontend ya comprime las imágenes a ~300 KB como máximo.
    - Se pasa la opción quality=auto a Cloudinary para que optimice aún más
      el peso del archivo sin pérdida de calidad visible.
    """
    try:
        # Leemos el contenido del archivo subido
        contents = await foto.read()

        # Obtenemos el tamaño en kilobytes para validación
        tamano_kb = len(contents) / 1024

        # Límite de seguridad: rechazamos archivos mayores a 1 MB
        # (el frontend ya comprime a ~300 KB, pero validamos en backend
        #  por si alguien llama la API directamente sin pasar por el frontend)
        MAX_TAMANO_KB = 1024  # 1 MB
        if tamano_kb > MAX_TAMANO_KB:
            raise HTTPException(
                status_code=413,
                detail=f"La imagen es demasiado grande ({tamano_kb:.0f} KB). "
                       f"El máximo permitido es {MAX_TAMANO_KB} KB. "
                       "El frontend comprime automáticamente las imágenes "
                       "antes de subirlas para evitar este error."
            )

        # Subimos a Cloudinary con optimización automática de calidad
        resultado = cloudinary.uploader.upload(
            contents,
            folder="productos",
            public_id=f"{producto}_{uuid.uuid4().hex[:8]}",
            # quality="auto" permite que Cloudinary optimice el peso
            # del archivo automáticamente, reduciendo el espacio en disco
            quality="auto:best",
            # fetch_format convierte automáticamente al formato más eficiente
            fetch_format="auto"
        )
        return {"ruta": resultado.get("secure_url")}
    except HTTPException:
        # Re-lanzamos excepciones HTTP para que FastAPI las maneje
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error subiendo a Cloudinary: {str(e)}")


@router.patch("/{producto}")
def editar_producto(producto: str, data: ActualizarProducto, tenant_id: str = Depends(get_tenant_id)):
    """Actualiza metadatos (descripción, imagen, estado) de un producto."""
    try:
        old_meta = query("SELECT Imagen as imagen FROM productos WHERE Producto=%s", (producto,))
        if old_meta:
            old_url = old_meta[0]["imagen"]
            if old_url and old_url != data.imagen and "res.cloudinary.com" in old_url:
                partes = old_url.split("/upload/")
                if len(partes) > 1:
                    ruta = partes[1]
                    if re.match(r'^v\d+/', ruta):
                        ruta = ruta.split("/", 1)[1]
                    public_id = ruta.rsplit(".", 1)[0]
                    cloudinary.uploader.destroy(public_id)
    except Exception as e:
        print(f"Error interno borrando foto antigua de Cloudinary: {e}")

    return actualizar_producto(producto, data.descripcion, data.imagen, data.estado, data.categoria, tenant_id)


@router.patch("/lote/{id_lote}")
def editar_lote(id_lote: str, data: ActualizarLote, tenant_id: str = Depends(get_tenant_id)):
    """Actualiza costo, precio de venta y stock de un lote específico."""
    return actualizar_lote(id_lote, data.costo, data.precio_venta, data.stock, tenant_id)

    # En backend/routers/inventario.py (o donde prefieras)

