# Fases 1–5 — Servicios + Variaciones + Compuestos + Variaciones × recetas + Pulido

> Documentación de la implementación del plan "3 tipos de producto" (stock / servicio / compuesto).
> Estado: ✅ F1 servicios · ✅ F2 variaciones · ✅ F3 compuestos · ✅ F4 variaciones × recetas · ✅ F5 pulido (stock decimal + disponibilidad + fotos por variación) · ⏸️ **SIN commit/push** (pendiente de tu evaluación).
> Fecha: 11/08/2026

---

## 🟢 FASE 1 — Productos de servicio (sin stock)

### 1. Qué es un producto de servicio

Un **servicio** es un producto **sin inventario**: se puede vender infinitas veces sin descontar stock.

| Tipo | Stock propio | Ejemplo | Al vender... |
|---|---|---|---|
| `stock` (actual, default) | ✅ sí | Peluche, planta | Descuenta su lote (PEPS) |
| `servicio` (nuevo) | ❌ no | Corte de cabello, clase, asesoría | Registra la venta directa con su costo/precio; no toca inventario |

### 2. Modelo de datos (Migración 017)

Archivo: `backend/migrations/017_producto_servicio.sql`

```sql
ALTER TABLE productos ADD COLUMN IF NOT EXISTS tipo_producto   TEXT NOT NULL DEFAULT 'stock';   -- 'stock' | 'servicio'
ALTER TABLE productos ADD COLUMN IF NOT EXISTS costo_servicio  NUMERIC DEFAULT 0;
ALTER TABLE productos ADD COLUMN IF NOT EXISTS precio_servicio NUMERIC DEFAULT 0;
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS tipo_producto TEXT NOT NULL DEFAULT 'stock';
```

- **Auto-migración**: `backend/database/conexion.py` ejecuta las migraciones pendientes al arrancar.
- **Compatibilidad**: los productos existentes quedan con `tipo_producto = 'stock'` (default).

### 3. Backend (Fase 1)

#### `database/lotes.py`
- **`agregar_lote`**: acepta `tipo_producto`, `costo_servicio`, `precio_servicio`. Si `tipo_producto='servicio'` **NO crea lote** (guarda costo/precio en el producto).
- **`get_inventario_consolidado`**: parte de `productos` (LEFT JOIN lotes) para que servicios aparezcan; fallbacks a `precio_servicio`/`costo_servicio`.
- **`get_productos_meta`**: expone los campos nuevos.

#### `database/ventas.py`
- **`cobrar_carrito`** (atómico): si el ítem es `servicio`, registra la venta directa **sin** `descontar_stock_peps`.
- **`actualizar_venta`** / **`eliminar_venta`**: no tocan stock para servicios.
- **`get_ventas`**: devuelve `tipo_producto`.

#### Routers
- **`routers/inventario.py`**: modelos con los campos nuevos.
- **`routers/publico.py`**: expone `tipo_producto`; "ocultar agotados" nunca elimina servicios.

### 4. Frontend (Fase 1)

- **`api.ts`**: tipos con `tipo_producto`, `costo_servicio`, `precio_servicio`.
- **Gestor**: selector de tipo (2 opciones en la Fase 1), campos de servicio, exclusión del restock.
- **POS**: sin stock/selector de lote para servicios, sin advertencia de stock.
- **Catálogo**: sin badge "Agotado" ni stock para servicios.

---

## 🟡 FASE 2 — Variaciones de producto

### 1. Qué es una variación

Presentación con su **PROPIO precio** para un mismo producto (ej. Sencilla $60 / Doble $95; S/M/L). Aplica a **CUALQUIER tipo de producto**. En esta fase no consume nada extra: solo cambia el precio.

### 2. Modelo de datos (Migración 018)

```sql
CREATE TABLE IF NOT EXISTS producto_variaciones (
    id          BIGSERIAL PRIMARY KEY,
    producto_id INTEGER NOT NULL REFERENCES productos(id) ON DELETE CASCADE,
    nombre      TEXT    NOT NULL,
    precio      REAL    NOT NULL DEFAULT 0,
    tenant_id   UUID    NOT NULL,
    UNIQUE(producto_id, nombre)
);
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS variacion text DEFAULT '';
```

### 3. Backend (Fase 2)

- **`lotes.py`**: `_adjuntar_variaciones()` (inyecta `variaciones` en consolidado/meta); CRUD `listar_variaciones_producto / crear_variacion / actualizar_variacion / eliminar_variacion` (captura `UniqueViolation` específicamente).
- **`ventas.py`**: `insertar_venta` guarda `variacion`; `cobrar_carrito` la propaga (servicios y PEPS); `get_ventas` la expone.
- **Routers**: `ItemCarrito.variacion` en el cobro; 4 endpoints `/inventario/variaciones*`; `publico.py` expone `variaciones` por producto.

### 4. Frontend (Fase 2)

- **Gestor**: panel "Variaciones" en Editar Prod. (filas editables `VariacionRow` + agregar), guardado al instante.
- **POS**: modal de selección de variación al agregar; selectores VAR. en el carrito; el lote no pisa el precio cuando hay variación; tarjeta "Desde $X".
- **Catálogo**: "desde $X" en Grid/Carta; modal con selector de variación y precio dinámico.

---

## 🟠 FASE 3 — Productos compuestos con receta (BOM)

### 1. Qué es un compuesto

Un **compuesto** (ej. hamburguesa, silla, pulsera) **no tiene stock propio**: al venderse, el gestor descuenta automáticamente el stock de sus **MATERIALES** según una receta. Terminología: "materiales" (neutro — sirve para comida y lo que sea).

| Ej. 1 hamburguesa = | cantidad por unidad |
|---|---|
| pan | 1 |
| carne | 150 |
| queso | 2 |

- Referencias por **id** → sobreviven renombrados de productos.
- Cantidad **REAL** → permite 0.5 pan, 150g, 0.25m.
- El **costo** del compuesto se calcula **EN VIVO** al vender (suma PEPS de materiales); el **precio de venta** es fijo (guardado en `precio_servicio`, reutilizando la columna de "precio sin stock").

### 2. Modelo de datos (Migración 019)

Archivo: `backend/migrations/019_producto_compuesto.sql`

```sql
CREATE TABLE IF NOT EXISTS producto_recetas (
    id          BIGSERIAL PRIMARY KEY,
    producto_id INTEGER NOT NULL REFERENCES productos(id) ON DELETE CASCADE,  -- el compuesto
    material_id INTEGER NOT NULL REFERENCES productos(id) ON DELETE CASCADE,   -- el material (producto de stock)
    cantidad    REAL    NOT NULL DEFAULT 1,
    tenant_id   UUID    NOT NULL,
    UNIQUE(producto_id, material_id)
);
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS consumo jsonb DEFAULT NULL;
-- consumo = [{material, id_lote, cantidad, costo}] — el detalle REAL de qué lotes se usaron
```

### 3. Backend (Fase 3)

#### `database/lotes.py`
- **`agregar_lote`**: acepta `'compuesto'` (no crea lote, devuelve `compuesto_creado`).
- **`get_inventario_consolidado` / `get_productos_meta`**: compuesto = sin stock, precio = `precio_servicio`.
- **`_adjuntar_recetas()`**: inyecta `recetas = [{id, material, cantidad}]` por producto.
- **CRUD**: `listar_recetas_producto / agregar_material_receta` (upsert `ON CONFLICT`, valida `producto != material`) / `actualizar_material_receta / eliminar_material_receta`.

#### `database/ventas.py` — el corazón
- **`cobrar_carrito` rama `compuesto`** (todo en la misma transacción atómica):
  1. Resuelve la receta base del compuesto.
  2. Por cada material → `descontar_stock_peps(material, cantidad × unidades, 0.0, conn)` (descuenta lotes PEPS y captura qué lote/costo salió).
  3. `costo_unitario` = Σ(costo×cantidad) / unidades (costo EN VIVO).
  4. Inserta **UNA sola venta** del compuesto + `consumo` JSON (`psycopg2.extras.Json`).
- **`_revertir_consumo(cur, consumo, tenant_id)`**: devuelve el stock a los **lotes EXACTOS** registrados (o recrea el lote con el costo del consumo si ya no existe; reactiva lotes Inactivos). **Esto corrige el bug conocido de "restaurar al lote equivocado" para compuestos.**
- **`eliminar_venta`** compuesto: revierte lote por lote con `_revertir_consumo` (en vez de `agregar_lote`).
- **`actualizar_venta`** compuesto: revierte el consumo viejo, re-descuenta con la NUEVA cantidad, recalcula costo_unitario en vivo y guarda el nuevo consumo (return temprano, no aplica el ajuste por `dif`).

#### Routers
- **`routers/inventario.py`**: modelos + 4 endpoints `/inventario/recetas*`.
- **`routers/publico.py`**: `CASE stock_total` incluye `'compuesto'`; "ocultar agotados" no los elimina (no se agotan).

### 4. Frontend (Fase 3)

- **`api.ts`**: `MaterialReceta`, `Producto.recetas`, `Venta.consumo`, funciones `getRecetas/agregarMaterial/editarMaterial/eliminarMaterial`.
- **Gestor**:
  - Selector de tipo con **3 opciones**: 📦 Con stock / ✂️ Servicio / 🍔 Compuesto (receta).
  - En el alta, un compuesto solo pide **Precio de venta** (el costo es en vivo).
  - **Editor de "Materiales (receta)"** en Editar Prod. (solo compuestos): buscador que filtra **solo productos de stock** (excluye el propio compuesto) + cantidad decimal + filas editables `MaterialRecetaRow`.
  - Card informativo "Compuesto sin inventario propio" en vez de la tabla de lotes.
  - Campo "Precio de venta" editable para compuestos (persistido vía `precio_servicio`).
- **POS**: la advertencia de stock excluye servicios Y compuestos; badge 🍔 Compuesto; selectores de lote solo para tipo `stock`.
- **Catálogo**: `esSinStock` (servicio o compuesto) en Grid, Carta y Modal → nunca se "agotan".

---

## 🔴 FASE 4 — Variaciones × recetas

### 1. Qué aporta

Un compuesto puede gastar **MATERIALES DIFERENTES según la variación vendida**:

| Hamburguesa | carne por unidad |
|---|---|
| Sencilla (variación) | 100g |
| Doble (variación) | 200g |
| _Base_ (si una variación no tiene receta propia) | la receta base |

- Si una variación **no tiene receta propia**, cae a la **base** (fallback).
- Cada venta queda con su **costo en vivo** correcto (la suma PEPS de los materiales de ESA variación).

### 2. Modelo de datos (Migración 020)

Archivo: `backend/migrations/020_receta_variaciones.sql`

```sql
ALTER TABLE producto_recetas ADD COLUMN IF NOT EXISTS variacion_id INTEGER
    REFERENCES producto_variaciones(id) ON DELETE CASCADE;   -- NULL = receta base

-- El UNIQUE(producto_id, material_id) original impedía el mismo material en 2 variaciones:
ALTER TABLE producto_recetas DROP CONSTRAINT IF EXISTS producto_recetas_producto_id_material_id_key;

-- Índices únicos parciales:
--  · base:      mismo material solo una vez por compuesto (variacion_id IS NULL)
--  · variación: mismo material solo una vez por (compuesto, variación)
CREATE UNIQUE INDEX IF NOT EXISTS uq_producto_recetas_base
    ON producto_recetas(producto_id, material_id) WHERE variacion_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_producto_recetas_variacion
    ON producto_recetas(producto_id, variacion_id, material_id) WHERE variacion_id IS NOT NULL;
```

- **Auto-migración**: `backend/database/conexion.py` la ejecuta en **bloques try/except independientes** (el DROP del constraint convive con bases donde la 019 se haya aplicado de forma distinta; si un paso falla, los demás siguen).

### 3. Backend (Fase 4)

- **`lotes.py`**:
  - `_adjuntar_recetas` y `listar_recetas_producto`: incluyen `variacion_id` + `nombre` de la variación (LEFT JOIN a `producto_variaciones`).
  - `agregar_material_receta`: acepta `variacion_id` opcional; **valida que la variación pertenezca al compuesto + tenant** (evita contaminar recetas de otros productos); decide UPDATE vs INSERT con `variacion_id IS NOT DISTINCT FROM %s` (ya NO usa `ON CONFLICT` — evita el TOCTOU/ambigüedad con los índices parciales).
- **`ventas.py`**:
  - Nuevo helper **`_resolver_receta_compuesto(cur, producto_id, variacion, tenant_id)`**: busca el id de la variación por nombre → intenta su receta → si está vacía (o la variación no existe / está vacía), cae a la **base**.
  - `cobrar_carrito` y `actualizar_venta` usan el helper → el descuento y el costo en vivo respetan la variación vendida (`ventas.variacion`).
- **`routers/inventario.py`**: `NuevoMaterialReceta.variacion_id: Optional[int] = None` (+ `ActualizarMaterialReceta` similar).

### 4. Frontend (Fase 4)

- **`api.ts`**: `MaterialReceta.variacion_id?` + las 4 funciones de recetas aceptan `variacion_id` opcional.
- **Gestor — editor de materiales con contexto**:
  - Selector **"Base / [Variación]"** encima de la lista de materiales: muestra y edita la receta base o la de una variación concreta.
  - `matVariacionSel` (estado): `null` = base, si no el `id` de la variación; se **resetea al cambiar de producto** en edición.
  - Filtrar `editRecetas` por contexto; `agregarMaterialItem` envía el `variacion_id` correspondiente.
- **POS / Catálogo**: sin cambios — el selector de variación ya existía (Fase 2); solo se conectó con la receta.

---

## ⚪ FASE 5 — Pulido: stock decimal + disponibilidad estimada + fotos por variación

---

## ⭐ MEJORA — Alta de producto completo (visible + variaciones + receta en 1 paso)

### Qué aporta
Antes, al crear un producto solo se creaba el producto (nombre, precio, stock) y las variaciones/receta se configuraban DESPUÉS en "Editar Prod.". Además no había forma de controlar la visibilidad en el catálogo al crearlo (por defecto salía visible, aunque fuera un ingrediente).

Ahora el formulario "Dar de alta" permite:

| Antes | Ahora |
|---|---|
| El producto salía visible en el catálogo sin opción de ocultarlo al crear | Select **"Visible en catálogo"** (por defecto **Visible**) — ideal para crear ingredientes/materiales ocultos |
| Las variaciones se agregaban solo en Editar Prod. | Bloque **"Variaciones (opcional)"** en el alta: nombre + precio, se crean en la misma transacción |
| La receta del compuesto se configuraba solo en Editar Prod. | Bloque **"Añadir ingredientes (receta)"** en el alta: buscador de productos de stock + cantidad decimal (0.5, 150...), transacción única |

### Implementación

**Backend (transacción atómica todo-o-nada):**
- `crear_producto_completo()` en `database/lotes.py`: crea producto + categorías + lote inicial + variaciones + receta en **una sola transacción**. Si algo falla (variación duplicada, material inexistente, material = sí mismo) → rollback y error claro.
- `ON CONFLICT` conserva `visible_en_catalogo` y `tipo_producto` actuales (no pisa en restock/conflictos).
- Receta duplicada (mismo material) → se actualiza la cantidad en vez de fallar.
- `routers/inventario.py`: modelo `NuevoProducto` extendido (`visible_en_catalogo`, `variaciones`, `recetas`); `ok:false` → HTTP 422 (validación) / 500 (error interno).

**Frontend (`src/app/inventario/page.tsx`):**
- Toggle/select "Visible en catálogo" justo bajo el selector de tipo.
- `AltaVariaciones`: lista de variaciones (nombre + precio) con botón Añadir/Quitar.
- `AltaMateriales`: buscador que solo sugiere productos de stock + cantidad decimal.
- `guardarNuevo` envía los campos nuevos; si la creación falla tras subir la foto, **borra la foto huérfana de Cloudinary**.

### Archivos tocados
- `backend/database/lotes.py` (`crear_producto_completo`, `_sincronizar_categorias` con `conn` opcional)
- `backend/routers/inventario.py` (modelos + endpoint)
- `frontend/src/lib/api.ts` (tipos `NuevoProducto` con los campos nuevos)
- `frontend/src/app/inventario/page.tsx` (UI del alta)

---


### 1. Qué aporta y CÓMO ESTABA ANTES

| Feature | Cómo estaba antes | Cómo quedó |
|---|---|---|
| **Stock decimal** | `lotes.Stock_Lote` y `ventas.Cantidad` eran **INTEGER** (solo enteros). Vender 0.5 kg era imposible; además las recetas con fracciones (0.5 pan, 150g) provocaban la **deriva fraccionaria** documentada: descontar 1.5 de un lote INTEGER redondeaba a 1 y al revertir se sumaba 1.5 (→ ganancia de +1 unidad por ciclo). El PEPS usaba `int(lote["stock_lote"])` por todos lados. | `Stock_Lote` y `Cantidad` ahora son **REAL**. El PEPS usa `float()` y redondea a 3 decimales (`round(_, 3)`); el cobro y la edición/anulación de ventas aceptan cantidades decimales. **La deriva fraccionaria queda corregida**: descontar 0.5 de 2.0 deja 1.5 exacto, y revertir devuelve 1.5 (no 1). |
| **Disponibilidad estimada** | No existía. Un compuesto mostraba stock 0 en el POS ("🍔 Compuesto" sin más), y el vendedor no sabía cuántas hamburguesas podía preparar hasta abrir el detalle de materiales. | Nuevo campo `disponibilidad_estimada` por compuesto: **min sobre su receta** (base y cada variación por separado, tomando el mínimo entre ellas = lo que garantizas vender sin importar la presentación). Se calcula en `get_inventario_consolidado` con el stock real de los materiales. El POS muestra **"Quedan ~N"** (ámbar; rojo si es 0) junto al badge 🍔 Compuesto. |
| **Fotos por variación** | Las variaciones solo tenían nombre + precio. En el catálogo, al elegir "Doble" seguías viendo la misma foto del producto (no existía foto por presentación). | Nueva columna `producto_variaciones.foto` (URL de Cloudinary). El gestor permite subir/quitar foto por variación (miniatura clicable con compresión automática). En el catálogo, al seleccionar una variación con foto, **esa foto encabeza la galería** del modal (se resetea el carrusel al cambiar de variación). |

### 2. Modelo de datos (Migración 021)

Archivo: `backend/migrations/021_stock_decimal_foto_variacion.sql`

```sql
ALTER TABLE lotes ALTER COLUMN Stock_Lote TYPE REAL;   -- INTEGER → REAL (Postgres convierte solo)
ALTER TABLE ventas ALTER COLUMN Cantidad  TYPE REAL;   -- mismo cambio
ALTER TABLE producto_variaciones ADD COLUMN IF NOT EXISTS foto TEXT DEFAULT '';
```

### 3. Backend (Fase 5)

#### `database/lotes.py`
- **PEPS decimal**: `descontar_stock_peps(cantidad_total: float)` — todos los `int(stock)` pasan a `float(stock)` y las actualizaciones redondean a 3 decimales (evita ruido flotante). `agregar_lote` / `actualizar_lote` aceptan `stock: float`.
- **`_calcular_disponibilidad_compuestos(filas, tenant_id)`**: calcula la disponibilidad POR RECETA (base + cada variación con receta propia por separado) y toma el **mínimo** entre ellas. Usa `SUM(Stock_Lote)` de los materiales con `ANY(%s)`; si no hay recetas → `None`. Se llama desde `get_inventario_consolidado` (el POS).
- **Variaciones con foto**: `_adjuntar_variaciones`, `listar_variaciones_producto` y `crear_variacion` incluyen `foto`; `actualizar_variacion` recibe `foto: str | None = None` (None = conserva, '' = quita).

#### `database/ventas.py`
- `actualizar_venta(cantidad: float)` y `cobrar_carrito` usan `float()` para cantidades (compuestos y servicios también). El ajuste por diferencia en edición usa `float()` + `round(_, 3)`.

#### `routers/inventario.py`
- Modelos: `stock: float` en `NuevoProducto`, `Restock`, `ActualizarLote`.
- `NuevaVariacion.foto` / `ActualizarVariacion.foto` (opcional).
- **Nuevo endpoint** `POST /inventario/variaciones/{id}/foto`: sube la foto a Cloudinary (carpeta `variaciones`), actualiza la fila y borra la foto anterior. El `DELETE` de variación también borra su foto de Cloudinary.
- El guard de `borrar_imagen` ahora también reconoce fotos de `producto_variaciones` (para poder limpiarlas al quitar foto).

#### `routers/publico.py`
- La query de variaciones del catálogo incluye `v.foto` → el modal puede mostrarla.

### 4. Frontend (Fase 5)

- **`api.ts`**: `Producto.disponibilidad_estimada?`, `Variacion.foto?`, `subirFotoVariacion(variacionId, file)`, y `crearVariacion`/`editarVariacion` con foto opcional.
- **Gestor**: `VariacionRow` gana una **miniatura clicable** (subir/reemplazar foto con `comprimirImagen` + `api.subirFotoVariacion`) y una ✕ para quitarla (`editarVariacion(foto:"")` + `borrarImagen`). Los inputs de Cantidad (nuevo/restock/editar lote) aceptan decimales (`step="0.1"`).
- **POS**: inputs de cantidad con `min=0.1 step=0.1`; los botones **±** avanzan de 1 en 1 si la cantidad ≥ 1 y de 0.1 en 0.1 si ya es fraccionaria (`pasoCantidad`). Tarjeta de compuestos: chip **"Quedan ~N"**.
- **Catálogo**: el modal pone la **foto de la variación seleccionada** al inicio de la galería y resetea el índice al cambiar de variación (`CatalogoModalProducto`). Interfaz `ProductoPublico` en `CatalogoView` con `foto?`.

---

## 🟣 FASE 6 — Stock por variación (inventario separado por presentación)

**Qué resuelve**: el caso de una tenant que vende llaveros por color/tamaño (Corazones Blanco / Rojo / Azul) y quiere que cada variación tenga su propio inventario: si se agotan los blancos, el catálogo marca "Agotado" solo en esa variación y el resto sigue vendible.

**Cómo estaba antes**: los lotes eran del PRODUCTO (por nombre); las variaciones solo cambiaban el precio y compartían el stock total. No se podía saber cuántos "Blanco" quedaban ni agotar una sola variación.

**Cómo quedó**:
- `lotes.variacion_id` (NULL = stock base del producto; {id} = stock EXCLUSIVO de esa variación). ON DELETE CASCADE: al eliminar una variación con "eliminar aún así", sus lotes (y stock) desaparecen con ella.
- `productos.stock_por_variacion` (flag por producto, default false = comportamiento idéntico al anterior).
- **Alta**: el bloque de variaciones (tipo stock) permite llenar **stock inicial** (y costo opcional) por variación → se crea su lote automáticamente y el producto queda con flag ON. Si todas van vacías, nada cambia.
- **Editar Prod.**: toggle "Stock separado por variación" (con advertencia si hay stock sin asignar). La tabla de lotes muestra la variación de cada lote y permite **reasignar/desvincular** (mover stock de una variación a otra). Badge de stock/agotado por variación.
- **Restock**: si el producto tiene flag ON, el formulario pide la variación (obligatoria, HTTP 422 si falta).
- **POS**: el modal de variación muestra "N uds" / "Agotado" y deshabilita las agotadas.
- **Catálogo**: cada variación trae su stock; las agotadas se muestran grisadas con "Agotado" y no se pueden elegir; el "desde $X" se calcula solo con las disponibles. `ocultar_agotados` oculta el producto solo si TODAS sus variaciones están sin stock.
- **Ventas/anulación**: `descontar_stock_peps` y la edición de ventas descuentan/restauran SOLO los lotes de la variación vendida (resuelta por id, solo si el flag está ON). Las ventas siguen apuntando al id_lote exacto → anular una venta de "Blanco" devuelve el stock al lote de "Blanco", no a otro.
- **Eliminar variación con stock**: el backend devuelve `requiere_confirmacion` con unidades/lotes → el gestor muestra advertencia y ofrece "eliminar aún así" (borra sus lotes en cascada). La foto de Cloudinary solo se borra cuando realmente se elimina.

**Archivos**: migración `022_stock_por_variacion.sql` + bloque inline en `database/conexion.py`; `database/lotes.py`, `database/ventas.py`; `routers/inventario.py`, `routers/publico.py`; frontend `src/lib/api.ts`, `src/app/inventario/page.tsx`, `src/app/page.tsx` (POS), `CatalogoView.tsx`, `CatalogoGridClasico.tsx`, `CatalogoMenuCarta.tsx`, `CatalogoModalProducto.tsx`.

---

## 📄 Archivos modificados (Fases 1 + 2 + 3 + 4 + 5)

### Backend (7 + 5 nuevos)
| Archivo | F1 | F2 | F3 | F4 | F5 |
|---|---|---|---|---|---|
| `database/conexion.py` | auto-mig 017 | auto-mig 018 | auto-mig 019 | auto-mig 020 | auto-mig 021 |
| `database/lotes.py` | servicios, JOIN, fallbacks | variaciones + CRUD | compuesto + recetas | recetas por variación | PEPS decimal, `_calcular_disponibilidad_compuestos`, foto en variaciones |
| `database/ventas.py` | rama servicio | `variacion` | compuesto + `consumo` | receta por variación | cantidades float en cobro/editar |
| `routers/inventario.py` | modelos | variaciones | recetas | `variacion_id` | stock float, `POST variaciones/{id}/foto`, borrado foto al eliminar |
| `routers/ventas.py` | — | `ItemCarrito.variacion` | — | — | — |
| `routers/publico.py` | JOIN, tipo, filtro | variaciones | CASE compuesto | — | foto en variaciones del catálogo |
| `migrations/017→021` | 017 | 018 | 019 | 020 | 021 nuevo |

### Frontend (7)
| Archivo | Cambios |
|---|---|
| `src/lib/api.ts` | tipos servicio + variaciones + recetas + `disponibilidad_estimada` + `Variacion.foto` + `subirFotoVariacion` |
| `src/app/inventario/page.tsx` | selector 3 tipos, editor de materiales, **foto por variación (miniatura clicable)**, inputs de stock decimal |
| `src/app/page.tsx` | POS: sin stock servicios/compuestos, modal variación, **cantidad decimal (min 0.1, ± inteligente)**, chip **"Quedan ~N"** |
| `src/app/catalogo/[slug]/CatalogoView.tsx` | interfaz con `variaciones.foto?` |
| `src/components/CatalogoGridClasico.tsx` | `esSinStock`, "desde $X" |
| `src/components/CatalogoMenuCarta.tsx` | `esSinStock`, "desde $X" |
| `src/components/CatalogoModalProducto.tsx` | `esSinStock`, selector de variación, **foto de la variación al inicio de la galería + reset de carrusel** |

---

## ✅ Validaciones realizadas

- ✅ `python -m py_compile` sobre los archivos backend de las 5 fases.
- ✅ `npx tsc --noEmit` en el frontend.
- ✅ `npm run build` (next build) en el frontend (5 fases).
- ✅ Code-review automático en las 5 fases (bugs corregidos, ver sección siguiente).

---

## 🐛 Bugs encontrados en code-review (ya corregidos)

### Fase 1
- **Regresión JOIN**: `get_inventario_consolidado` sin filtrar `p.Estado = 'Activo'` → productos desactivados aparecían. ✅ Filtro añadido.

### Fase 2
1. **"desde" invertido en el modal** del catálogo (aparecía en productos sin variaciones). ✅
2. **`manejarToggleDescuento` pisaba el precio de la variación** en el POS. ✅
3. **`crear_variacion` capturaba cualquier excepción** como "duplicado" (ahora solo `UniqueViolation`). ✅

### Fase 3
1. 🔴 **Editar el precio de un compuesto no se guardaba**: el payload de `guardarProducto` solo enviaba `precio_servicio` si `tipo_producto === "servicio"`. ✅ Corregido: se envía para `tipo_producto !== "stock"`.

### Fase 4
1. ⚠️ **TOCTOU en `agregar_material_receta`**: el SELECT previo + INSERT no era atómico bajo concurrencia (dos requests simultáneos podían insertar el mismo material duplicado). ✅ Corregido: la migración 020 crea índices únicos parciales que ahora SÍ respaldan el upsert (la fila duplicada viola el índice → se captura `UniqueViolation` como "ya existe").
2. ⚠️ **Migración 020 frágil**: un solo bloque `try/except` podía abortar el DROP del constraint si algo fallaba antes. ✅ Corregido: pasos en bloques try/except **independientes**.

### Fase 5
1. 🔴 **TS: `agregar(variacionActual?.foto)`** no aceptaba `string | undefined` → error de tipos en build. ✅ `?? ""`.
2. 🔴 **JSX: dos elementos hermanos en el ternario del POS** (chip Compuesto + chip Quedan) sin fragment → `TS1005`. ✅ Envuelto en `<>...</>`.
3. ⚠️ **Disponibilidad estimada mezclaba recetas de variaciones** (last-wins por material) cuando no había receta base → estimación arbitraria. ✅ Ahora se calcula **por receta** (base + cada variación) y se toma el **mínimo** entre ellas.

### 🔍 Análisis externo (revisión independiente) — bugs corregidos

> Una revisión externa del código real detectó bugs adicionales. Se corrigieron todos los críticos:

1. 🔴 **`actualizar_venta` compuesto con receta vacía → inventario inflado en silencio**: si el compuesto ya no tiene materiales, al editar la venta se revertía el stock (2a) sin re-descontar nada (receta vacía). ✅ Ahora, si la receta está vacía → `rollback` + error claro ("Agrega materiales antes de editar la venta"). También se agregó el guard en `cobrar_carrito`: un compuesto **sin receta no se puede cobrar** (frena todo el carrito con rollback).
2. 🔴 **`eliminar_venta` compuesto en dos transacciones**: el stock se revertía con `conn2.commit()` y la venta se marcaba Inactivo con otro commit → si el proceso moría a mitad, stock restaurado + venta Activo (doble restauración posible). ✅ Reescrita: **una sola transacción** — revertir consumo, restaurar stock y marcar Inactivo, todo con un único commit.
3. 🔴 **Anular venta de STOCK restauraba al lote equivocado**: usaba `agregar_lote` (busca por costo+precio). ✅ Ahora restaura al **lote EXACTO** (`ventas.id_lote`), recreándolo con (costo, precio_lista) si fue eliminado; el fallback para ventas legacy sin `id_lote` quedó **dentro de la transacción** (inline con el cursor, no vía `agregar_lote` que usa el pool y rompería la atomicidad).
4. 🔴 **Pydantic rechazaba cantidades decimales**: `ItemCarrito.cantidad: int` → cualquier 0.5 kg daba 422 y el cobro fallaba (la Fase 5 no funcionaría end-to-end). ✅ `float` en `ItemCarrito` y `ActualizarVenta`; `corregir_venta` usa `float(v['cantidad'])`.
5. ⚠️ **`n_ticket` leído en `get_ventas` pero la columna no existía** en el CREATE ni en migraciones → fallo de runtime latente. ✅ Añadida al `CREATE TABLE ventas` y con `ALTER TABLE ... ADD COLUMN IF NOT EXISTS n_ticket`.
6. ⚠️ **Instalaciones nuevas inconsistentes**: el `CREATE TABLE` seguía con `Stock_Lote INTEGER` (la migración 021 lo convertía después). ✅ Ahora crea directo con `REAL` (y `Cantidad REAL`).
7. ⚠️ **Lote recreado con precio = costo** (en `_revertir_consumo` y anulaciones): ✅ ahora usa `MAX(Precio_Venta)` del material como best-effort; en anulación de stock usa el `precio_lista` original de la venta.
8. 🟡 Menores: imports movidos al top (`uuid`, `json`, `get_conn`/`release_conn`, `descontar_stock_peps`), docstring de `agregar_lote` antes del código, `borrar_venta` ahora responde HTTP 400 con el mensaje (el frontend ya lo muestra).
9. ✅ **Bug #9 (renombrar compuesto rompía ventas antiguas) — VERIFICADO: ya estaba cubierto.** `actualizar_producto` sincroniza el renombrado en `productos`, `lotes` y `ventas`, así que `_resolver_receta_compuesto` por nombre sigue resolviendo las ventas antiguas. Edge residual documentado: si un material fue renombrado Y su lote eliminado, el lote recreado usa el nombre viejo del `consumo` (caso menor).
10. 🟡 **Cosmético (2ª revisión) — cobro de compuesto sin receta devolvía HTTP 500**: semánticamente es una regla de negocio incumplida → ahora `cobrar_carrito` distingue `ValueError` (devuelve `tipo: "validacion"`) y el router responde **422** (500 solo para errores internos/DB).

> **2ª revisión externa: ✅ LISTO para producción** (verificado en código real, no solo en el `.md`).

---

## ⚠️ Bugs pendientes conocidos

> No bloquean las Fases 1-4, pero están documentados para resolverlos en fases futuras.

1. **Anular venta de un producto de STOCK restaura al lote equivocado** ⚠️
   `eliminar_venta` usa `agregar_lote` (restaura buscando lote con mismo costo+precio). Si hay varios lotes con el mismo costo/precio, puede sumar al lote incorrecto. **Los COMPUESTOS ya NO tienen este bug** (usan `_revertir_consumo` con el id_lote exacto). El fix general para productos de stock se puede hacer con el mismo mecanismo (guardar `id_lote` en la venta de stock, que ya existe en `ventas.id_lote`).
2. ✅ ~~**Consumo fraccionario vs stock INTEGER — deriva acumulativa**~~ **CORREGIDO en Fase 5**: `Stock_Lote` y `Cantidad` ahora son REAL y el PEPS redondea a 3 decimales.
3. **Carrito viejo en localStorage sin `variacion`** (menor): mitigado con placeholder "Elegir variación".
4. **`precio_lista` en ventas de stock con variación**: queda con el precio del lote; `precio_real` es el de la variación — correcto por diseño, solo un matiz del historial.
5. **Precio sugerido tras agotarse el lote más antiguo** (reporte beta tester, preexistente): mitigado con el modo configurable de precio sugerido.
6. **Código casado a Cloudinary** (preexistente): si se migra a Supabase Storage hay que actualizar subida, URLs de optimización y limpieza.
7. **`agregar_material_receta` no valida que el producto padre sea compuesto** (menor): el UI solo lo muestra para compuestos; el backend lo aceptaría para un stock. Aceptable.
8. **Sin probar en producción**: las 5 fases están validadas en compilación pero **no desplegadas ni probadas con datos reales**. Pruebas sugeridas:
   - Crear un compuesto (ej. "Hamburguesa" = 1 pan + 150 carne + 2 queso), registrar stock de los materiales, vender 2 hamburguesas → verificar que los materiales bajen según la receta, que el costo unitario se calcule en vivo y que `ventas.consumo` quede registrado.
   - **Fase 5**: vender **0.5 kg** de un producto con stock decimal → verificar que el lote baje 0.5 exacto, que la venta registre Cantidad 0.5 y que al anular vuelva exacto (sin deriva). Verificar el chip **"Quedan ~N"** en un compuesto (y que baje al vender).
   - **Fase 5**: subir una **foto a una variación** en el gestor → en el catálogo, abrir el producto y seleccionar esa variación → debe mostrar la foto de la variación primero.
   - **Fase 4**: dar a la Hamburguesa una variación "Doble" (200g carne) → vender 1 "Sencilla" y 1 "Doble" → verificar que cada venta descuente 100g vs 200g y tenga su propio costo.
   - Anular esas ventas → verificar que el stock vuelva a los **mismos lotes** (revertir por consumo).
   - Editar la cantidad de una venta → verificar re-descuento correcto.
   - Servicio + variaciones + compuesto con variaciones conviviendo.
9. **Meta/Facebook avisa `fb:app_id` faltante** (catálogo, no estas fases).

---

## 🗺️ Roadmap

| Fase | Qué incluye | Estado |
|---|---|---|
| **1. Servicios** | tipo_producto sin stock | ✅ implementada |
| **2. Variaciones** | `producto_variaciones` + `ventas.variacion`; selectores; "desde $X" | ✅ implementada |
| **3. Compuestos + recetas** | `producto_recetas`; cobro descuenta materiales PEPS; costo en vivo; `ventas.consumo`; anulación/edición revierten lote por lote | ✅ implementada |
| **4. Variaciones × recetas** | receta por variación (ej. hamburguesa sencilla vs doble): `producto_recetas.variacion_id` con fallback a base | ✅ implementada |
| **5. Pulido** | **stock decimal** (REAL — corrige la deriva fraccionaria), disponibilidad estimada ("quedan ~3"), fotos por variación | ✅ implementada |

**Las 5 fases del plan "3 tipos de producto" están completas.** 🎉

**Decisiones del usuario ya tomadas**:
- Costo del compuesto: **calculado en vivo** al cobrar (suma PEPS de materiales) ✅ aplicado.
- Falta de materiales: **vender igual con aviso** (stock negativo en el material) ✅ aplicado.
- Variaciones: aplican a **cualquier producto** (stock, servicio o compuesto) ✅ aplicado en Fase 2.
- Receta por variación: si la variación no tiene receta propia → **cae a la base** ✅ aplicado en Fase 4.

Otros pendientes diferidos:
- Marca de agua del logo en fotos del catálogo.
- Rediseño del menú del catálogo.

---

## 📦 Estado de git

- **Backend**: 12 archivos modificados/nuevos **sin commit** (Fases 1 + 2 + 3 + 4 + 5 juntas).
- **Frontend**: 7 archivos modificados **sin commit** (Fases 1 + 2 + 3 + 4 + 5 juntas).
- **Pendiente de tu evaluación**: cuando decidas, se hará commit + push de todas las fases juntas (backend y frontend).
