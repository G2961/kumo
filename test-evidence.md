# Проверки на настоящем сервере

Windows, `opencode-ai@1.19.3`, `http://127.0.0.1:58074`.
Playwright 1.64.1, Chromium Headless Shell. Только запрос HTML-ассета
`/_ui/index.html` заменяется содержимым единственного `codex.html`.
Все API-запросы и `/event` обслуживает настоящий opencode.
Провайдер напрямую тестом не вызывается. Заголовки не подменяются.
Ключи и полные ошибки провайдера в отчёты не записываются.

Команда запуска (из корня проекта):

```text
python tests/real_api.py --server http://127.0.0.1:58074 --project <isolated-git-directory> --provider AgentRouter-gpt-responses-gateway --model gpt-5.6-sol --report tests/baseline-agentrouter.json
```

## До изменений клиента, шаг 3

- `tests/baseline-agentrouter.json`: PASS, ответ содержит `CODEX_REAL_OK`,
  `workingVisible=false`, ошибок JavaScript нет. Реальные SSE:
  `message.updated`, `message.part.updated`, `message.part.delta`,
  `session.status` busy/idle, `session.idle`.
- Провайдер уже настроен на ПК самим пользователем: hostname `agentrouter.org`,
  path `/v1`, SDK `@ai-sdk/openai`, дополнительных заголовков нет.
  Тест использовал существующие настройки opencode; ключ не извлекался.
- `tests/baseline.json`: Big Pickle, ответа за 90 секунд нет, `retry`.
- `tests/baseline-big-pickle.json`: повтор за 35 секунд также без ответа,
  `retry`. HTTP `prompt_async` в обоих случаях 204, SSE реально приходят.
  `idle` в неуспешных тестах приходит после отправленного тестом abort.
  Установленная причина недоступности Big Pickle отсутствует.

Зависимости: `pip install -r tests/requirements.txt`,
`python -m playwright install chromium --only-shell`.
