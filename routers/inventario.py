# ==============================================================================
# backend/routers/inventario.py
# Endpoints de inventario, lotes y productos.
# ==============================================================================

from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Depends
from pydantic import BaseModel
from typing import Optional
from dependencies import get_tenant_id
import os, uuid, re

# ── Integración con Cloudinary ──────────────────────────────────────────────
# Cloudinary reemplaza el almacenamiento local en disco.
# Motivo: Render tiene filesystem efímero que borra los archivos en cada
# deploy, lo que provocaba que las fotos de productos desaparecieran y
# se mostraran como iconos de imagen rota en el frontend.
# Cloudinary sirve las imágenes desde su CDN global con URL persistente.
import cloudinary
import cloudinary.uploader
import cloudinary.api

# Configuración con credenciales desde variables de entorno.
# En Render estas variables deben estar definidas:
#   CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET")
)

from database.lotes import (
    get_lotes, get_productos_meta, get_inventario_consolidado,
    get_detalle_lotes, agregar_lote, actualizar_producto, actualizar_lote,
    eliminar_categoria_de_productos, listar_categorias, renombrar_categoria,
    crear_categoria, eliminar_lote, toggle_visibilidad_categoria
)
from database.conexion import query, execute
from pydantic import Field
from dependencies import validar_sesion

router = APIRouter(prefix="/inventario", tags=["Inventario"])

# --- Modelos Pydantic (validan los datos que llegan) ---

class NuevoProducto(BaseModel):
    producto       : str
    descripcion    : str  = ""
    costo          : float
    precio_venta   : float
    stock          : int
    imagen         : str  = "No hay foto"
    categoria      : list[str]  = ["General"]
    codigo_interno : Optional[str] = None
    codigo_barras  : Optional[str] = None
    ubicacion      : Optional[str] = None

class Restock(BaseModel):
    producto    : str
    costo       : float
    precio_venta: float
    stock       : int

class ActualizarProducto(BaseModel):
    descripcion         : str
    imagen              : str
    estado              : str
    categoria           : list[str]
    costo               : Optional[float] = None
    precio_venta        : Optional[float] = None
    producto            : Optional[str] = None
    codigo_interno      : Optional[str] = None
    codigo_barras       : Optional[str] = None
    ubicacion           : Optional[str] = None
    visible_en_catalogo : Optional[bool] = None

class ActualizarLote(BaseModel):
    costo       : float
    precio_venta: float
    stock       : int

class CrearCategoria(BaseModel):
    nombre: str = Field(..., min_length=1, description="Nombre de la categoría a crear")

class RenombrarCategoria(BaseModel):
    nuevo_nombre: str = Field(..., min_length=1, description="Nuevo nombre para la categoría")


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
        tenant_id,
        codigo_interno=data.codigo_interno,
        codigo_barras=data.codigo_barras,
        ubicacion=data.ubicacion
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
      ya que el frontend ya comprime las imágenes a ~80 KB como máximo.
    - Se pasa la opción quality=auto a Cloudinary para que optimice aún más
      el peso del archivo sin pérdida de calidad visible.
    """
    try:
        # Leemos el contenido del archivo subido
        contents = await foto.read()

        # Validar que el contenido no esté vacío
        if not contents or len(contents) == 0:
            raise HTTPException(status_code=400, detail="La imagen recibida está vacía (0 bytes)")

        # Obtenemos el tamaño en kilobytes para validación
        tamano_kb = len(contents) / 1024

        # Límite de seguridad: rechazamos archivos mayores a 1 MB.
        # El frontend comprime a ~80 KB, pero validamos en backend por si
        # alguien llama la API directamente sin pasar por el frontend.
        MAX_TAMANO_KB = 1024  # 1 MB
        if tamano_kb > MAX_TAMANO_KB:
            raise HTTPException(
                status_code=413,
                detail=f"La imagen es demasiado grande ({tamano_kb:.0f} KB). "
                       f"El máximo permitido es {MAX_TAMANO_KB} KB. "
                       "El frontend comprime automáticamente las imágenes "
                       "antes de subirlas para evitar este error."
            )

        # Subimos a Cloudinary con optimización automática de calidad.
        # folder="productos" agrupa todas las fotos en una carpeta en Cloudinary.
        # public_id usa el nombre del producto + un hex aleatorio para unicidad.
        # quality="auto:best" permite que Cloudinary optimice el peso del
        # archivo automáticamente sin pérdida de calidad visible.
        # fetch_format="auto" convierte automáticamente al formato más
        # eficiente (WebP en navegadores modernos, JPEG en el resto).
        resultado = cloudinary.uploader.upload(
            contents,
            folder="productos",
            public_id=f"{producto}_{uuid.uuid4().hex[:8]}",
            quality="auto:best",
            fetch_format="auto"
        )
        # Cloudinary devuelve una URL HTTPS persistente que se guarda en la DB.
        # Esta URL sobrevive a deploys de Render porque vive en el CDN de Cloudinary.
        return {"ruta": resultado.get("secure_url")}

    except HTTPException:
        # Re-lanzamos excepciones HTTP para que FastAPI las maneje correctamente
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error subiendo a Cloudinary: {str(e)}")


@router.patch("/{producto}")
def editar_producto(producto: str, data: ActualizarProducto, tenant_id: str = Depends(get_tenant_id)):
    """Actualiza metadatos (descripción, imagen, estado) de un producto."""
    # Intentar borrar la imagen anterior de Cloudinary si se reemplazó.
    # Esto evita acumular fotos huérfanas en el CDN (que cuestan almacenamiento).
    try:
        old_meta = query("SELECT Imagen as imagen FROM productos WHERE Producto=%s", (producto,))
        if old_meta:
            old_url = old_meta[0]["imagen"]
            # Solo intentamos borrar si la URL vieja era de Cloudinary y cambió
            if old_url and old_url != data.imagen and "res.cloudinary.com" in old_url:
                # Extraer el public_id de la URL de Cloudinary.
                # Formato URL: https://res.cloudinary.com/{cloud}/image/upload/v{version}/{folder}/{public_id}.{ext}
                # Necesitamos quedarnos solo con: {folder}/{public_id} (sin la extensión)
                partes = old_url.split("/upload/")
                if len(partes) > 1:
                    ruta = partes[1]
                    # Eliminar el prefijo de versión (v1234567890/)
                    if re.match(r'^v\d+/', ruta):
                        ruta = ruta.split("/", 1)[1]
                    # Eliminar la extensión del archivo (.jpg, .png, etc.)
                    public_id = ruta.rsplit(".", 1)[0]
                    cloudinary.uploader.destroy(public_id)
    except Exception as e:
        print(f"Error interno borrando foto antigua de Cloudinary: {e}")

    return actualizar_producto(
        producto, data.descripcion, data.imagen, data.estado, data.categoria,
        data.costo, data.precio_venta, tenant_id,
        nuevo_producto=data.producto,
        codigo_interno=data.codigo_interno,
        codigo_barras=data.codigo_barras,
        ubicacion=data.ubicacion,
        visible_en_catalogo=data.visible_en_catalogo
    )


@router.patch("/lote/{id_lote}")
def editar_lote(id_lote: str, data: ActualizarLote, tenant_id: str = Depends(get_tenant_id)):
    """Actualiza costo, precio de venta y stock de un lote específico."""
    return actualizar_lote(id_lote, data.costo, data.precio_venta, data.stock, tenant_id)


@router.delete("/lote/{id_lote}")
def borrar_lote(id_lote: str, tenant_id: str = Depends(get_tenant_id)):
    """Da de baja un lote. Si es el último activo, también desactiva el producto."""
    return eliminar_lote(id_lote, tenant_id)


@router.get("/categorias")
def obtener_categorias(tenant_id: str = Depends(get_tenant_id)):
    """Devuelve la lista de categorías del tenant con conteo de productos."""
    return listar_categorias(tenant_id)


@router.post("/categoria/crear")
def crear_categoria_endpoint(data: CrearCategoria, tenant_id: str = Depends(get_tenant_id)):
    """Crea una categoría nueva o devuelve la existente si ya existe."""
    return crear_categoria(data.nombre, tenant_id)


@router.patch("/categoria/{categoria}")
def editar_categoria(categoria: str, data: RenombrarCategoria, tenant_id: str = Depends(get_tenant_id)):
    """
    Renombra una categoría existente.
    Actualiza todas las relaciones automáticamente (el slug se recalcula).
    """
    return renombrar_categoria(categoria, data.nuevo_nombre, tenant_id)


@router.delete("/categoria/{categoria}")
def borrar_categoria(categoria: str, tenant_id: str = Depends(get_tenant_id)):
    """Elimina una categoría de todos los productos del tenant."""
    return eliminar_categoria_de_productos(categoria, tenant_id)


@router.patch("/categoria/{categoria}/visibilidad")
def alternar_visibilidad_catalogo(categoria: str, tenant_id: str = Depends(get_tenant_id)):
    """
    Alterna la visibilidad de una categoría en el catálogo público.
    Si estaba visible, se oculta; si estaba oculta, se muestra.
    """
    return toggle_visibilidad_categoria(categoria, tenant_id)


# =============================================================================
# ── GALERÍA DE IMÁGENES (Plan Plus) ──────────────────────────────────────────
# Los tenants con plan "plus" pueden subir hasta 5 imágenes adicionales
# por producto. La imagen principal sigue en productos.imagen (Cloudinary).
# Las imágenes extra viven en la tabla producto_imagenes.
# =============================================================================

def _get_tenant_plan(tenant_id: str) -> str:
    """
    Helper interno: obtiene el plan del tenant desde la tabla tenants.
    Devuelve 'basico' por defecto si el tenant no tiene plan asignado
    o si ocurre algún error consultando la base de datos.
    Esto nunca debe bloquear la app — si falla, se asume plan básico
    (menos privilegios) por seguridad.
    """
    try:
        resultado = query("SELECT plan FROM tenants WHERE id = %s", (tenant_id,))
        if resultado and resultado[0].get("plan"):
            return resultado[0]["plan"]
    except Exception as e:
        print(f"Error obteniendo plan del tenant: {e}")
    return "basico"


def _get_producto_id(producto_nombre: str, tenant_id: str) -> int:
    """
    Helper interno: obtiene el ID numérico de un producto por su nombre.
    Las tablas lotes y ventas referencian productos por nombre (text),
    pero producto_imagenes usa FK a productos.id (integer).
    Este helper hace el puente entre ambos sistemas.
    """
    resultado = query(
        "SELECT id FROM productos WHERE Producto = %s AND tenant_id = %s",
        (producto_nombre, tenant_id)
    )
    if not resultado:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    return resultado[0]["id"]


@router.get("/imagenes/{producto}")
def listar_imagenes_producto(producto: str, tenant_id: str = Depends(get_tenant_id)):
    """
    Devuelve la galería completa de imágenes de un producto.
    Para Plan Plus: hasta 5 imágenes unificadas (la orden 1 es la principal).
    Para Plan básico: devuelve una lista vacía (el frontend usa ImagePicker simple).
    """
    producto_id = _get_producto_id(producto, tenant_id)
    resultado = query(
        "SELECT id, url, orden FROM producto_imagenes "
        "WHERE producto_id = %s AND tenant_id = %s "
        "ORDER BY orden ASC",
        (producto_id, tenant_id)
    )
    return resultado


@router.post("/imagenes/{producto}")
async def subir_imagen_extra(
    producto: str,
    foto: UploadFile = File(...),
    orden_target: Optional[int] = Form(None),
    tenant_id: str = Depends(get_tenant_id)
):
    """
    Sube una imagen a la galería de un producto (Plan Plus).
    - Si orden_target es None: añade una imagen nueva (siguiente slot disponible).
    - Si orden_target es 1-5: reemplaza la imagen en esa posición (borra la anterior de Cloudinary).
    Máximo 5 imágenes por producto.
    La orden 1 se sincroniza automáticamente con productos.imagen.
    """
    # 1. Validar plan plus
    plan = _get_tenant_plan(tenant_id)
    if plan != "plus":
        raise HTTPException(
            status_code=403,
            detail="La galería de imágenes es exclusiva del plan Plus."
        )

    # 2. Obtener el ID del producto
    producto_id = _get_producto_id(producto, tenant_id)

    # 3. Leer y validar el archivo
    contents = await foto.read()
    if not contents or len(contents) == 0:
        raise HTTPException(status_code=400, detail="La imagen recibida está vacía (0 bytes)")

    tamano_kb = len(contents) / 1024
    MAX_TAMANO_KB = 1024  # 1 MB
    if tamano_kb > MAX_TAMANO_KB:
        raise HTTPException(
            status_code=413,
            detail=f"La imagen es demasiado grande ({tamano_kb:.0f} KB). Máximo {MAX_TAMANO_KB} KB."
        )

    # 4. Determinar el orden y si es reemplazo o inserción nueva
    if orden_target is not None:
        # Reemplazo: validar que el orden esté en rango
        if orden_target < 1 or orden_target > 5:
            raise HTTPException(status_code=400, detail="orden_target debe estar entre 1 y 5")
        nueva_orden = orden_target

        # Borrar la imagen existente en ese orden de Cloudinary + DB
        existente = query(
            "SELECT id, url FROM producto_imagenes "
            "WHERE producto_id = %s AND tenant_id = %s AND orden = %s",
            (producto_id, tenant_id, nueva_orden)
        )
        if existente:
            old_url = existente[0]["url"]
            if "res.cloudinary.com" in old_url:
                try:
                    partes = old_url.split("/upload/")
                    if len(partes) > 1:
                        ruta = partes[1]
                        if re.match(r'^v\d+/', ruta):
                            ruta = ruta.split("/", 1)[1]
                        public_id = ruta.rsplit(".", 1)[0]
                        cloudinary.uploader.destroy(public_id)
                except Exception as e:
                    print(f"Error borrando imagen anterior de Cloudinary: {e}")
            execute(
                "DELETE FROM producto_imagenes WHERE id = %s",
                (existente[0]["id"],)
            )
    else:
        # Inserción nueva: calcular siguiente slot disponible
        conteo = query(
            "SELECT COUNT(*) as total FROM producto_imagenes "
            "WHERE producto_id = %s AND tenant_id = %s",
            (producto_id, tenant_id)
        )
        total_actual = conteo[0]["total"] if conteo else 0
        if total_actual >= 5:
            raise HTTPException(
                status_code=403,
                detail="Este producto ya tiene 5 imágenes. Elimina una antes de subir otra."
            )

        ordenes = query(
            "SELECT orden FROM producto_imagenes "
            "WHERE producto_id = %s AND tenant_id = %s ORDER BY orden",
            (producto_id, tenant_id)
        )
        ordenes_usadas = {r["orden"] for r in ordenes}
        nueva_orden = 1
        for i in range(1, 6):
            if i not in ordenes_usadas:
                nueva_orden = i
                break

    # 5. Subir a Cloudinary en la carpeta "productos/galeria"
    resultado = cloudinary.uploader.upload(
        contents,
        folder="productos/galeria",
        public_id=f"{producto}_{uuid.uuid4().hex[:8]}_img{nueva_orden}",
        quality="auto:best",
        fetch_format="auto"
    )
    url = resultado.get("secure_url")

    # 6. Insertar en la base de datos
    execute(
        "INSERT INTO producto_imagenes (producto_id, tenant_id, url, orden) "
        "VALUES (%s, %s, %s, %s)",
        (producto_id, tenant_id, url, nueva_orden)
    )

    # 7. Sincronizar productos.imagen si la orden es 1 (la principal)
    if nueva_orden == 1:
        execute(
            "UPDATE productos SET Imagen = %s WHERE id = %s AND tenant_id = %s",
            (url, producto_id, tenant_id)
        )

    return {"ok": True, "url": url, "orden": nueva_orden}


@router.delete("/imagenes/{imagen_id}")
def eliminar_imagen_extra(imagen_id: int, tenant_id: str = Depends(get_tenant_id)):
    """
    Elimina una imagen de la galería de un producto (Plan Plus).
    - Borra la imagen de Cloudinary.
    - Si era la orden 1 (principal), promueve la siguiente imagen a principal
      y sincroniza productos.imagen.
    - Reordena las imágenes restantes para que no quien huecos.
    """
    # 1. Validar plan plus
    plan = _get_tenant_plan(tenant_id)
    if plan != "plus":
        raise HTTPException(
            status_code=403,
            detail="La gestión de galería es exclusiva del plan Plus."
        )

    # 2. Obtener la imagen antes de borrar
    resultado = query(
        "SELECT id, producto_id, url, orden FROM producto_imagenes "
        "WHERE id = %s AND tenant_id = %s",
        (imagen_id, tenant_id)
    )
    if not resultado:
        raise HTTPException(status_code=404, detail="Imagen no encontrada")

    img = resultado[0]
    url = img["url"]
    orden_eliminada = img["orden"]
    producto_id = img["producto_id"]

    # 3. Borrar de Cloudinary
    if "res.cloudinary.com" in url:
        try:
            partes = url.split("/upload/")
            if len(partes) > 1:
                ruta = partes[1]
                if re.match(r'^v\d+/', ruta):
                    ruta = ruta.split("/", 1)[1]
                public_id = ruta.rsplit(".", 1)[0]
                cloudinary.uploader.destroy(public_id)
        except Exception as e:
            print(f"Error borrando imagen extra de Cloudinary: {e}")

    # 4. Borrar de la base de datos
    execute(
        "DELETE FROM producto_imagenes WHERE id = %s AND tenant_id = %s",
        (imagen_id, tenant_id)
    )

    # 5. Si era la principal (orden 1), promover la siguiente y reordenar
    if orden_eliminada == 1:
        # Buscar la siguiente imagen disponible (menor orden)
        siguiente = query(
            "SELECT id, url FROM producto_imagenes "
            "WHERE producto_id = %s AND tenant_id = %s "
            "ORDER BY orden ASC LIMIT 1",
            (producto_id, tenant_id)
        )
        if siguiente:
            # La siguiente asciende a orden 1
            execute(
                "UPDATE producto_imagenes SET orden = 1 WHERE id = %s",
                (siguiente[0]["id"],)
            )
            # Sincronizar productos.imagen con la nueva principal
            execute(
                "UPDATE productos SET Imagen = %s WHERE id = %s AND tenant_id = %s",
                (siguiente[0]["url"], producto_id, tenant_id)
            )
        else:
            # No quedan imágenes: resetear productos.imagen
            execute(
                "UPDATE productos SET Imagen = 'No hay foto' WHERE id = %s AND tenant_id = %s",
                (producto_id, tenant_id)
            )
    else:
        # No era la principal, pero reordenamos para compactar slots
        # Mover las imágenes con orden > orden_eliminada un paso atrás
        posteriores = query(
            "SELECT id, orden FROM producto_imagenes "
            "WHERE producto_id = %s AND tenant_id = %s AND orden > %s "
            "ORDER BY orden ASC",
            (producto_id, tenant_id, orden_eliminada)
        )
        for p in posteriores:
            nuevo_orden = p["orden"] - 1
            execute(
                "UPDATE producto_imagenes SET orden = %s WHERE id = %s",
                (nuevo_orden, p["id"])
            )

    return {"ok": True, "id": imagen_id}


class ReordenarImagenes(BaseModel):
    """Modelo para reordenar imágenes de la galería de un producto.
    Recibe un array de IDs en el nuevo orden deseado (primero = orden 1 = principal)."""
    ids: list[int] = Field(..., description="Array de IDs de imágenes en el nuevo orden")


@router.patch("/imagenes/{producto}/reordenar")
def reordenar_imagenes(producto: str, data: ReordenarImagenes, tenant_id: str = Depends(get_tenant_id)):
    """
    Reordena las imágenes de la galería de un producto (Plan Plus).
    Recibe un array de IDs en el nuevo orden (posición 0 = orden 1 = principal).
    El backend asigna orden 1, 2, 3... secuencialmente según el orden del array.
    Si el ID en orden 1 es diferente al anterior, actualiza productos.imagen
    automáticamente con la URL de la nueva imagen principal.
    """
    # 1. Validar plan plus
    plan = _get_tenant_plan(tenant_id)
    if plan != "plus":
        raise HTTPException(
            status_code=403,
            detail="La gestión de galería es exclusiva del plan Plus."
        )

    if not data.ids:
        raise HTTPException(status_code=400, detail="El array de IDs no puede estar vacío")

    producto_id = _get_producto_id(producto, tenant_id)

    # 2. Verificar que todos los IDs pertenecen a este producto y tenant
    existentes = query(
        "SELECT id, url, orden FROM producto_imagenes "
        "WHERE producto_id = %s AND tenant_id = %s AND id = ANY(%s)",
        (producto_id, tenant_id, data.ids)
    )
    ids_validos = {r["id"] for r in existentes}
    ids_enviados = set(data.ids)
    if ids_enviados - ids_validos:
        raise HTTPException(
            status_code=400,
            detail="Algunos IDs no pertenecen a este producto o no existen."
        )

    # 3. Actualizar orden secuencialmente según la posición en el array
    for idx, img_id in enumerate(data.ids):
        nuevo_orden = idx + 1
        execute(
            "UPDATE producto_imagenes SET orden = %s WHERE id = %s AND tenant_id = %s",
            (nuevo_orden, img_id, tenant_id)
        )

    # 4. Sincronizar productos.imagen con la nueva orden 1
    primera = query(
        "SELECT url FROM producto_imagenes WHERE id = %s AND tenant_id = %s",
        (data.ids[0], tenant_id)
    )
    if primera:
        execute(
            "UPDATE productos SET Imagen = %s WHERE id = %s AND tenant_id = %s",
            (primera[0]["url"], producto_id, tenant_id)
        )

    return {"ok": True, "mensaje": "Imágenes reordenadas correctamente"}
