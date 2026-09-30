-- ==============================================================================
-- 051: Días de descanso del negocio (tenants.dias_cerrados).
--
-- El Análisis Inteligente sugería "promo en tu día más flojo" sin saber si el
-- negocio abre ese día: con sábado cerrado, "el día flojo es sábado ($65)" era
-- un falso positivo (las $65 eran las ventas de los sábados que sí abrió).
--
-- dias_cerrados: lista JSONB de días de la semana en los que el negocio NO
-- abre. Nombres en minúscula sin acento, mismas claves que usa la analítica:
--   ['lunes', ... 'domingo']  ·  vacío o NULL = abre todos los días
--
-- Los días marcados cerrados salen del cálculo de concentración y jamás se
-- sugieren como día para una promo (las ventas que tengan son de días
-- excepcionales: no cuentan para el patrón de la semana).
--
-- El usuario lo configura en Ajustes via PATCH /inventario/me (mismo flujo que
-- zona_horaria). Idempotente.
-- ==============================================================================

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS dias_cerrados JSONB;
