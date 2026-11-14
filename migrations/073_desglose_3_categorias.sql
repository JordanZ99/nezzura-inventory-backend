-- ==============================================================================
-- 073: Desglose en 3 categorías para el gestor de espacio (migración 071).
--
-- El tenant no distingue foto principal de galería ni logo de banner: el
-- desglose pasa a 3 categorías que SÍ entiende:
--
--   imagenes   — todo lo de Cloudinary (es lo ÚNICO que cuenta en el límite)
--   datos      — estado actual del negocio (productos, lotes, clientes,
--                categorías, configuración, snapshot del catálogo publicado)
--   historial  — transacciones acumuladas (ventas, gastos, movimientos,
--                interacciones, conteos, activity log) — promesa: nunca se
--                borra y no le cuesta espacio
--
-- Los pesos de datos/historial son ESTIMACIONES nocturnas (cron): se prorratea
-- pg_total_relation_size de cada tabla por la proporción de filas del tenant.
-- Se cachean aquí para que /almacenamiento/uso (poll cada 60 s) sea una lectura
-- barata, no un cálculo pesado por request. `sin_uso_bytes` (huérfanas del
-- ledger sin ninguna referencia) también se refresca en el cron.
-- ==============================================================================

ALTER TABLE tenant_storage ADD COLUMN IF NOT EXISTS datos_bytes    BIGINT NOT NULL DEFAULT 0;
ALTER TABLE tenant_storage ADD COLUMN IF NOT EXISTS historial_bytes BIGINT NOT NULL DEFAULT 0;
ALTER TABLE tenant_storage ADD COLUMN IF NOT EXISTS sin_uso_bytes  BIGINT NOT NULL DEFAULT 0;
