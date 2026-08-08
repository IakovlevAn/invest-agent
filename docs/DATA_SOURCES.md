# Источники данных

Последняя проверка ссылок: 2026-08-08.

## БКС

Официальная документация подтверждает два типа refresh-токенов: read-only и
trade-and-read. Access-токен выпускается из refresh-токена и живёт ограниченное
время. Это позволяет физически отделить аналитический контур от исполнителя.

- Быстрый старт: https://trade-api.bcs.ru/
- Авторизация: https://trade-api.bcs.ru/http/authorization/
- Портфель и лимиты: https://trade-api.bcs.ru/http/limits/
- Рыночные данные: https://trade-api.bcs.ru/http/market-data/
- Заявки: https://trade-api.bcs.ru/http/operations/
- Ограничения API: https://trade-api.bcs.ru/restrictions/

Перед реализацией клиента необходимо зафиксировать актуальные JSON-схемы из
официальной документации и записать контрактные ответы в обезличенные fixtures.

Текущий read-only клиент использует только два официальных адреса:

- `POST https://be.broker.ru/trade-api-keycloak/realms/tradeapi/protocol/openid-connect/token`
  с обязательным `client_id=trade-api-read`;
- `GET https://be.broker.ru/trade-api-bff-portfolio/api/v1/portfolio`.

Клиент не содержит URL создания заявок и не умеет использовать
`client_id=trade-api-write`.

Нормализатор реализован по официальной HTTP-схеме `positions`: `agreementId`,
`ticker`, `instrumentType`, `quantity`, `locked`, `currentPrice`,
`currentValueRub`, `board`, `isBlockedTradeAccount` и `isBlocked`. Номер
брокерского соглашения не попадает в отчёт: используется короткий SHA-256 ref.

Ответ авторизации содержит новую пару access/refresh. Новый refresh-токен
атомарно записывается в локальный mode-600 файл до запроса портфеля, чтобы сбой
после ротации не
оставил приложение со старым секретом.

## Московская биржа

Read-only клиент использует публичный ISS и разделяет три официальных объекта:

- `/iss/securities/{secid}.json` — карточка выпуска и основной режим торгов;
- `/iss/engines/stock/markets/bonds/boards/{board}/securities/{secid}.json` —
  котировки, эффективная доходность, дюрация, спреды и показатели торгов;
- `/iss/emitters/{id}.json` — юридическая карточка эмитента.

Доходность и дюрация в первую очередь берутся из таблицы
`marketdata_yields` (`EFFECTIVEYIELDWAPRICE`, `DURATIONWAPRICE`). Купон не
используется как замена доходности. Для концентрации выпуски агрегируются по
`EMITTER_ID`. Публичные данные MOEX могут быть задержаны, поэтому отчёт хранит
`TRADEMOMENT`, `SYSTIME`, время получения и прямые URL запросов.

Документация ISS: https://www.moex.com/a2193 и
https://iss.moex.com/iss/reference/.

## Следующая карта источников

Для фундаментальной модели будут отдельно проверены первичные источники:

- Банк России — ставка, кривая и показатели вкладов;
- центры раскрытия и сайты эмитентов — отчётность и корпоративные события;
- российские рейтинговые агентства — рейтинги и пресс-релизы;
- ФНС и официальные разъяснения — налоги и режим ИИС.

Новости и аналитические публикации могут быть только дополнительным, а не
единственным основанием решения.
