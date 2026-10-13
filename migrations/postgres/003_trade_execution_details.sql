-- FlashHunter 003: детали боевого исполнения сделок.
--
-- Зачем: в боевом режиме важно хранить не только «успех/реверт», но и то, что
-- именно вернула сеть: результат pre-flight (eth_call) до отправки, ответ релея,
-- номер блока и газ. На эти данные опирается разбор инцидентов и /analytics.
--
-- Миграция идемпотентна: повторный запуск не меняет схему.

ALTER TABLE trades ADD COLUMN IF NOT EXISTS revert_reason TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS details       JSONB;

COMMENT ON COLUMN trades.revert_reason IS 'Причина реверта/отказа (боевой режим)';
COMMENT ON COLUMN trades.details       IS 'JSON: pre-flight, ответ релея, факты из блока';

-- Индекс для разбора «почему сделки падают»: чаще всего ищут именно реверты
CREATE INDEX IF NOT EXISTS ix_trades_status_created ON trades (status, created_at DESC);
