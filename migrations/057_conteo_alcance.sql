-- ==============================================================================
-- 057: Conteo PARCIAL de auditoría (alcance por categoría o productos).
--
-- Un conteo completo obliga a contar TODO el inventario de golpe — inviable
-- para hacerlo seguido. El alcance permite congelar el snapshot de solo una
-- parte: por categorías o por productos elegidos. El resto del inventario ni
-- se toca: el cierre solo resuelve los renglones del conteo y lo "sin contar"
-- queda como está, así que un parcial nunca puede romper el resto.
--
-- alcance JSONB (NULL = conteo completo):
--   {"categorias": ["Anillos"], "producto_ids": [3, 8]}
-- Guarda lo que pidió el tenant (para el banner e historial); el snapshot
-- real (renglones) es la fuente de verdad de qué se incluyó.
-- ==============================================================================

ALTER TABLE conteos ADD COLUMN IF NOT EXISTS alcance JSONB;
