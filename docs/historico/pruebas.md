# Checklist de pruebas — Fases 1-5 (3 tipos de producto + pulido)

> Checklist para verificar en producción que todo salió bien tras el deploy
> de las Fases 1-5 (servicios, variaciones, compuestos, variaciones x recetas
> y pulido) + las correcciones de la revision externa.
>
> Como usar: marca cada casilla con `[x]` cuando la prueba pase. Si algo falla,
> anota el detalle al final en la seccion "Observaciones".

## 0. Smoke test (hacer primero)

- [ ] El gestor (POS e Inventario) carga sin errores en consola tras el deploy
- [ ] Inventario > Editar Prod. de cualquier producto con lotes: se ven los lotes con stock (la migracion a REAL no perdio datos)
- [ ] Los logs del backend (Render) no muestran tracebacks; la auto-migracion 021 paso en silencio
- [ ] El Historial de ventas (Estadisticas) carga sin error (valida el fix de n_ticket)

> Nota: probar en un tenant de prueba, no en el real. Hacer snapshot/backup de la BD antes.

## 1. Pruebas criticas (bugs corregidos — las mas importantes)

- [ ] **1.1 Stock decimal al cobrar**: vender 0.5 kg de un producto -> NO da error 422 (fix `cantidad: float`); el lote baja 0.5 exacto
- [ ] **1.2 Anular venta decimal**: anular esa venta -> el lote vuelve a +0.5 exacto (2.0 -> 1.5 -> 2.0, sin deriva)
- [ ] **1.3 Lote exacto al anular**: crear 2 lotes con el mismo costo y precio; vender 3 uds (PEPS usa el lote viejo); anular -> el stock vuelve al lote viejo exacto (no al nuevo). Verificar el stock de cada lote en Editar Prod.
- [ ] **1.4 Receta vacia al editar**: crear compuesto con receta, venderlo, borrar un material de la receta, editar la cantidad de esa venta -> error claro "Este compuesto ya no tiene materiales en su receta" y el stock de los materiales NO queda inflado
- [ ] **1.5 Cobrar compuesto sin receta**: el carrito se frena con error 422 (no 500)
- [ ] **1.6 Doble anulacion**: anular una venta dos veces -> la segunda da "Ya anulada" y el stock se restaura una sola vez
- [ ] **1.7 Anulaciones seguidas**: anular una venta compuesta y una de stock seguidas -> cada una restaura exactamente lo suyo, sin duplicar

## 2. Fase 1 — Servicios

- [ ] Crear "Corte de cabello" como tipo Servicio (costo y precio propios) -> no pide cantidad/stock; no aparece en Restock
- [ ] Venderlo en el POS -> se agrega sin advertencia de stock; la venta registra tipo servicio
- [ ] Anular esa venta -> no toca inventario
- [ ] En el catalogo publico -> aparece, nunca muestra "Agotado" ni stock

## 3. Fase 2 — Variaciones

- [ ] Crear "T-shirt" con variaciones S $60 / M $70 / L $80 -> guarda al instante
- [ ] En el POS, tocar el producto -> abre el modal de variacion; elegir "L" agrega con $80
- [ ] Cambiar la variacion en el carrito (selector VAR.) -> el precio cambia al de la variacion; el lote se descuenta bien
- [ ] Activar/desactivar el descuento en un item con variacion -> el precio no se pisa
- [ ] En el catalogo -> la tarjeta muestra "desde $60"; en el modal, elegir variacion cambia el precio

## 4. Fases 3 y 4 — Compuestos y variaciones x recetas (el corazon)

Preparacion: productos "Pan" (stock 10), "Carne" (stock 1000g), "Queso" (stock 20). Compuesto "Hamburguesa" = 1 pan + 150 carne + 2 queso.

- [ ] Vender 2 hamburguesas -> Pan 10->8, Carne 1000->700, Queso 20->16 (descuento exacto)
- [ ] Revisar la venta en el historial -> costo unitario = costo en vivo (suma de lotes / 2), no 0
- [ ] Anular esa venta -> Pan 8->10, Carne 700->1000, Queso 16->20 (vuelve a los mismos lotes)
- [ ] Agregar variacion "Doble" a la hamburguesa con receta propia (200g carne, sin pan)
- [ ] Vender 1 Sencilla (base) y 1 Doble -> Carne baja 100 + 200 = 300g; cada venta con su costo distinto
- [ ] Vender mas de lo que hay (ej. 3 hamburguesas con solo 2 panes) -> vende igual con aviso (stock negativo en pan, comportamiento por diseño)

## 5. Fase 5 — Pulido

- [ ] Restock de 2.5 kg (el input acepta decimales) -> el lote muestra 2.5
- [ ] En el POS, la tarjeta de la Hamburguesa -> muestra el chip "Quedan ~N" y baja al vender
- [ ] En el gestor, Editar Prod. > Variaciones > tocar la miniatura de "Doble" y subir una foto -> guarda y aparece la miniatura
- [ ] En el catalogo publico, abrir el producto y seleccionar "Doble" -> la foto de la variacion aparece primero en el carrusel
- [ ] Quitar la foto de la variacion -> desaparece la miniatura; el catalogo vuelve a la foto normal

## 6. Catalogo publico + multitenant

- [ ] Abrir el catalogo en celular y en computadora -> grid/menu correcto, "desde $X", modal de producto con galeria y variaciones
- [ ] Con "ocultar agotados" activo -> servicios y compuestos siguen visibles (no se agotan)
- [ ] Si se activo "descargar fotos" -> el boton de descarga funciona (sin marca de agua, pendiente)
- [ ] Compartir el link por WhatsApp -> preview con logo/titulo correctos
- [ ] Loguearse con otro tenant -> no ve los productos ni ventas del primero

## 7. Verificacion directa en la BD (la mas confiable)

```sql
-- 1. Migracion 021 aplicada (tipos REAL + columna foto)
SELECT column_name, data_type FROM information_schema.columns
WHERE table_name IN ('lotes','ventas','producto_variaciones')
  AND column_name IN ('Stock_Lote','Cantidad','foto');

-- 2. Consumo registrado de una venta compuesta
SELECT id, producto, cantidad, consumo FROM ventas
WHERE tipo_producto = 'compuesto' AND estado = 'Activo' ORDER BY id DESC LIMIT 5;

-- 3. Tras anular: el stock quedo exacto (comparar antes/despues)
SELECT producto, id_lote, stock_lote FROM lotes WHERE producto IN ('Pan','Carne','Queso');
```

## Orden sugerido

0 (smoke) -> 1 (criticos) -> 4 (compuestos) -> 5 (pulido) -> 3 (variaciones) -> 2 (servicios) -> 6 (catalogo).

Si 0 y 1 pasan, el resto es casi seguro.

## Observaciones

- (anotar aqui cualquier fallo, detalle o comportamiento inesperado)
