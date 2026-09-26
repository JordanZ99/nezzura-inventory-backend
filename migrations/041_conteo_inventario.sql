-- ==============================================================================
-- 041: Conteos de auditoría (conteo físico vs sistema).
--
-- Un conteo es una SESIÓN con ciclo de vida:
--   abrir (snapshot del stock por producto+variación) →
--   capturar (lo que hay físicamente, acumulativo, autosave) →
--   cerrar (resolver cada diferencia).
--
-- Reglas clave:
--   - esperado = stock del sistema congelado AL ABRIR. Una venta hecha
--     DURANTE el conteo no debe parecer merma: por eso el cierre compara
--     contra el stock VIVO (re-leído al cerrar) y no contra el snapshot.
--   - vivo / delta = stock vivo y diferencia REAL al momento del cierre;
--     quedan persistidos en cada renglón para que el historial sea
--     autocontenido (qué se esperaba, qué había vivo, qué se contó).
--   - resolucion = decisión del tenant por renglón. Los movimientos que
--     genera cada resolución caen al ledger (040) con origen='conteo' y
--     referencia_id = id del conteo:
--       * merma / error_sistema (faltante) → salida PEPS sin venta
--       * venta                              → orden + ventas retroactivas
--       * entrada_no_registrada              → lote nuevo con costo capturado
--       * error_sistema (sobrante)           → ajuste +stock
--   - Solo UN conteo abierto por tenant a la vez (índice único parcial).
-- ==============================================================================

CREATE TABLE IF NOT EXISTS conteos (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    estado     TEXT NOT NULL DEFAULT 'abierto' CHECK (estado IN ('abierto', 'cerrado')),
    abierto_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    cerrado_at TIMESTAMPTZ,
    resumen    JSONB
);

CREATE TABLE IF NOT EXISTS conteo_items (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conteo_id      UUID NOT NULL REFERENCES conteos(id) ON DELETE CASCADE,
    tenant_id      UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    producto_id    INTEGER,
    producto       TEXT NOT NULL,
    variacion      TEXT NOT NULL DEFAULT '',
    esperado       NUMERIC(14,3) NOT NULL DEFAULT 0,
    contado        NUMERIC(14,3),
    vivo           NUMERIC(14,3),
    delta          NUMERIC(14,3),
    resolucion     TEXT CHECK (resolucion IS NULL OR resolucion IN
                   ('merma', 'venta', 'error_sistema', 'entrada_no_registrada',
                    'cuadrado', 'sin_contar')),
    actualizado_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- RLS igual que el resto de tablas de negocio
ALTER TABLE conteos ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON conteos;
CREATE POLICY tenant_isolation ON conteos
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

ALTER TABLE conteo_items ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON conteo_items;
CREATE POLICY tenant_isolation ON conteo_items
    FOR ALL
    USING (tenant_id = auth.uid())
    WITH CHECK (tenant_id = auth.uid());

CREATE INDEX IF NOT EXISTS idx_conteos_tenant ON conteos (tenant_id, estado, abierto_at DESC);
CREATE INDEX IF NOT EXISTS idx_conteo_items_conteo ON conteo_items (conteo_id);

-- Un renglón por (producto, variación) dentro del mismo conteo
CREATE UNIQUE INDEX IF NOT EXISTS idx_conteo_items_unicos
    ON conteo_items (conteo_id, producto, variacion);

-- Solo UN conteo abierto por tenant a la vez
CREATE UNIQUE INDEX IF NOT EXISTS idx_conteos_tenant_abierto
    ON conteos (tenant_id) WHERE estado = 'abierto';

-- El ledger (040) aprende el origen 'conteo': toda resolución de diferencias
-- (merma, venta declarada, ingreso hallado, corrección) queda trazable contra
-- el id de la sesión. DROP+ADD es idempotente para el runner de arranque.
ALTER TABLE inventario_movimientos DROP CONSTRAINT IF EXISTS inventario_movimientos_origen_check;
ALTER TABLE inventario_movimientos ADD CONSTRAINT inventario_movimientos_origen_check
    CHECK (origen IN ('venta', 'restock', 'ajuste_manual', 'edicion_venta',
                      'anulacion', 'baja_lote', 'conteo'));
