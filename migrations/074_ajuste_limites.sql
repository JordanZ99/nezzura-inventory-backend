-- ==============================================================================
-- 074: Ajuste de límites de almacenamiento a la mitad.
--
-- basico: 1 GB → 500 MB · plus: 10 GB → 5 GB.
--
-- Motivo (cálculo de unit economics con Cloudinary): el costo peligroso es el
-- bandwidth del catálogo (tráfico-driven); reducir el techo de storage no baja
-- ese costo per se, pero sí duplica el número de tenants "llenos" que caben en
-- los 225 créditos del plan Plus de Cloudinary antes de saltar al Advanced, y
-- empuja el upgrade de plan antes. Sigue competitivo: 5 GB por $245 MXN donde
-- la competencia da 5-10 GB por $399-899.
--
-- ON CONFLICT DO UPDATE: instala frescas también nacen con estos límites.
-- El backend lee limite_bytes en vivo (JOIN en cada verificación), así que el
-- cambio surte efecto inmediato en tenants existentes sin deploy.
-- ==============================================================================

INSERT INTO planes_config (plan, nombre, precio_mxn, limite_bytes) VALUES
    ('basico', 'Básico', 99,  536870912),    -- 500 MB
    ('plus',   'Plus',   245, 5368709120)    -- 5 GB
ON CONFLICT (plan) DO UPDATE SET
    limite_bytes   = EXCLUDED.limite_bytes,
    actualizado_en = now();
