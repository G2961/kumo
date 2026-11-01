# Проверка клиента по настоящему opencode 1.19.3

Проверяемый источник: `app/src/main/assets/codex.html` из приватного
`G2961/opencode-android-new`, исходный коммит `2d8df54`.
Архивный прототип и `extras/build.py` не используются.
Схема: `openapi.json`, полученная с работающего сервера на Windows.

## До исправлений

| Область | Настоящий API | Клиент и результат сверки |
| --- | --- | --- |
| Сессии | GET/POST `/session`; GET `/session/{sessionID}/message`; POST `/session/{sessionID}/prompt_async`, `/abort` | Пути существуют. `directory` — необязательный query-параметр. Создание `{}` и промпт `{parts:[{type:"text",text}],agent,model:{providerID,modelID}}` соответствуют схеме; `prompt_async` возвращает 204. |
| Провайдеры | GET `/config/providers` → `{providers: Provider[], default: Record<string,string>}`; `models` — объект | Разбор формы правильный. Выбор первого провайдера по порядку объекта может выбирать неподходящую модель; сохранённая модель не проверяется при смене проекта. |
| SSE | GET `/event?directory=...` → `Event`, `{id,type,properties}` | `message.updated` → `properties.info`; `message.part.updated` → `properties.part`; `message.part.delta` → `sessionID,messageID,partID,field,delta`: разбор совпадает. Схема другого `/api` с полем `data` не является форматом `/event`. |
| Статусы и ошибки | `session.status` → `{sessionID,status:{type:idle/busy/retry,...}}`; `session.idle` → `{sessionID}`; `session.error` → `{sessionID?,error?}` | Основные поля совпадают. При восстановлении соединения клиент читает сообщения, но не GET `/session/status`; старый незавершённый ответ может оставлять «Working…» после abort/ошибки. Ошибка сообщения также не сбрасывает running. |
| Разрешения | `permission.asked` → `PermissionRequest`; `permission.replied` → `{sessionID,requestID,reply}`; GET `/permission`; POST `/permission/{requestID}/reply` → `{reply:once/always/reject}` | Формат карточки и подтверждения совпадает. Старый POST `/session/{sessionID}/permissions/{permissionID}` с `{response}` тоже есть. `permission.updated` в настоящей схеме `/event` отсутствует; обработчик лишний, но не ломает API. Восстановление SSE не загружает ожидающие разрешения. |
| Диффы | GET `/session/{sessionID}/diff` → `SnapshotFileDiff[]`: `file?,patch?,additions,deletions,status?` | **Расхождение:** клиент строит дифф из отсутствующих `before/after`, поэтому реальные изменения не показывает. Нужно отображать `patch`. |
| Авторизация провайдера | PUT `/auth/{providerID}` → `{type:"api",key}`; 200 boolean | В текущем источнике формы Providers вообще нет, запрос не выполняется. `directory` у этого endpoint не объявлен. Ключ хранит opencode; сохранять в localStorage/конфиге клиента нельзя. |
| Конфигурация провайдера | PATCH `/config?directory=...` → Config; `provider[id]={npm,name,options:{baseURL},models:{id:{name}}}` | В текущем источнике формы нет. Для OpenAI-совместимого провайдера применим `@ai-sdk/openai-compatible`; `apiKey` не должен попадать в PATCH. |
| Смена проекта/треда | Все instance endpoints работают в контексте directory | **Ошибка состояния:** асинхронные запросы старого треда/проекта могут записать сообщения в новый; New thread не сбрасывает running/perms; перезагрузка провайдеров при смене проекта отсутствует. |
| Удаление сообщений | `message.removed`, `message.part.removed` доступны в `/event` | Обработчиков нет, поэтому удалённые сообщения/части остаются до перезагрузки. |

Это сверка контракта и кода, а не доказательство ответа модели.
События, провайдер и ответ проверяются отдельно настоящим Playwright-тестом.
