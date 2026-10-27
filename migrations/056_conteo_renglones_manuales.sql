-- ==============================================================================
-- 056: Renglones MANUALES en los conteos de auditoría.
--
-- Problemática: el snapshot del conteo (041) solo incluye productos ya
-- registrados con lote activo. Un producto que nunca se dio de alta era
-- invisible para la auditoría: lo que quedó de él no se podía contar ni
-- estimar lo vendido. Los renglones manuales permiten agregar DENTRO del
-- conteo un producto no registrado con:
--   - nombre (libre, no debe existir en el sistema al agregarlo),
--   - inicial (opcional): "cuánto había al principio" — si se declara y
--     contado < inicial, el cierre declara la venta (inicial − contado) con
--     precio REAL y costo opcional,
--   - contado: lo que físicamente queda — al cerrar se da de alta como
--     producto nuevo + lote etiqueta 'Conteo' con el costo/precio indicados.
--
-- Cambios mínimos sobre 041 (todo idempotente):
--   - origen: 'snapshot' (lo generó abrir_conteo) | 'manual' (lo agregó el
--     usuario). Los renglones existentes quedan 'snapshot' por default.
--   - inicial: solo manual. Reutiliza la semántica de la pantalla de cierre:
--     vivo = inicial y delta = contado − vivo (negativo = vendido).
--
-- El alta del producto ocurre al CERRAR (no al agregar), dentro de la misma
-- transacción atómica del cierre: si algo falla, ni producto ni lote ni
-- venta quedan a medias. La venta manual no descuenta stock (nunca hubo):
-- insertar_venta la liga al producto recién creado (producto_id NOT NULL).
-- ==============================================================================

ALTER TABLE conteo_items ADD COLUMN IF NOT EXISTS origen TEXT NOT NULL DEFAULT 'snapshot';
ALTER TABLE conteo_items ADD COLUMN IF NOT EXISTS inicial NUMERIC(14,3);

ALTER TABLE conteo_items DROP CONSTRAINT IF EXISTS conteo_items_origen_check;
ALTER TABLE conteo_items ADD CONSTRAINT conteo_items_origen_check
    CHECK (origen IN ('snapshot', 'manual'));
