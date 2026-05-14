# ==============================================================================
# backend/routers/inventario.py
# Endpoints de inventario, lotes y productos.
# ==============================================================================

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from typing import Optional
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
from dependencies import get_tenant_id
from fastapi import Depends

router = APIRouter(prefix="/inventario", tags=["Inventario"])

FOTOS_DIR = "fotos_productos"

# --- Modelos Pydantic (validan los datos que llegan) ---

class NuevoProducto(BaseModel):
    producto    : str
    descripcion : str  = ""
    costo       : float
    precio_venta: float
    stock       : int
    imagen      : str  = "No hay foto"
    categoria   : str  = "General"

class Restock(BaseModel):
    producto    : str
    costo       : float
    precio_venta: float
    stock       : int

class ActualizarProducto(BaseModel):
    descripcion: str
    imagen     : str
    estado     : str
    categoria  : str

class ActualizarLote(BaseModel):
    costo       : float
    precio_venta: float
    stock       : int


# --- Endpoints ---

@router.get("/")
def listar_inventario(tenant_id: str = Depends(get_tenant_id)):
    """Vista consolidada: un producto = una fila con stock total."""
    print(f"DEBUG: listar_inventario — Tenant ID: {tenant_id}")
    resultado = get_inventario_consolidado(tenant_id)
    print(f"DEBUG: listar_inventario — Resultados: {len(resultado)}")
    return resultado


@router.get("/lotes")
def listar_lotes(tenant_id: str = Depends(get_tenant_id)):
    """Todos los lotes activos con detalle de costo y stock por lote."""
    print(f"DEBUG: listar_lotes — Tenant ID: {tenant_id}")
    resultado = get_lotes(tenant_id)
    print(f"DEBUG: listar_lotes — Resultados: {len(resultado)}")
    return resultado


@router.get("/lotes/{producto}")
def lotes_por_producto(producto: str, tenant_id: str = Depends(get_tenant_id)):
    """Lotes activos de un producto específico."""
    return get_detalle_lotes(producto, tenant_id)


@router.get("/productos")
def listar_productos(tenant_id: str = Depends(get_tenant_id)):
    """Metadatos de todos los productos."""
    print(f"DEBUG: listar_productos — Tenant ID: {tenant_id}")
    resultado = get_productos_meta(tenant_id)
    print(f"DEBUG: listar_productos — Resultados: {len(resultado)}")
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
        data.producto, "",
        data.costo, data.precio_venta, data.stock,
        tenant_id=tenant_id
    )


@router.post("/foto/{producto}")
async def subir_foto(producto: str, foto: UploadFile = File(...)):
    """Sube la foto de un producto a Cloudinary y devuelve la URL segura."""
    try:
        contents = await foto.read()
        resultado = cloudinary.uploader.upload(
            contents,
            folder="productos",
            public_id=f"{producto}_{uuid.uuid4().hex[:8]}"
        )
        return {"ruta": resultado.get("secure_url")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error subiendo a Cloudinary: {str(e)}")


@router.patch("/{producto}")
def editar_producto(producto: str, data: ActualizarProducto, tenant_id: str = Depends(get_tenant_id)):
    """Actualiza metadatos (descripción, imagen, estado) de un producto."""
    try:
        old_meta = query("SELECT Imagen as imagen FROM productos WHERE Producto=%s AND tenant_id=%s", (producto, tenant_id))
        if old_meta:
            old_url = old_meta[0]["imagen"]
            # Si cambió la imagen y la antigua era de Cloudinary, la borramos para no gastar espacio
            if old_url and old_url != data.imagen and "res.cloudinary.com" in old_url:
                partes = old_url.split("/upload/")
                if len(partes) > 1:
                    ruta = partes[1]
                    # Quitar el posible prefijo de versión, ej: v170966456/productos/hash.jpg -> productos/hash.jpg
                    if re.match(r'^v\d+/', ruta):
                        ruta = ruta.split("/", 1)[1]
                    # Quitar la extensión .jpg/.png
                    public_id = ruta.rsplit(".", 1)[0]
                    # Eliminamos la imagen antigua
                    cloudinary.uploader.destroy(public_id)
    except Exception as e:
        print(f"Error interno borrando foto antigua de Cloudinary: {e}")

    return actualizar_producto(producto, data.descripcion, data.imagen, data.estado, data.categoria, tenant_id)


@router.patch("/lote/{id_lote}")
def editar_lote(id_lote: str, data: ActualizarLote, tenant_id: str = Depends(get_tenant_id)):
    """Actualiza costo, precio de venta y stock de un lote específico."""
    return actualizar_lote(id_lote, data.costo, data.precio_venta, data.stock, tenant_id)