# STATE.md — ui round 3 (brief: permissions, subtractive UI, context menu)

## Выполнено (доказательство: tests/ui_round3.py → ALL PASS, 64 чека, tests/evidence/ui_round3/*.png)

### Block B — Права (клиент)
- Шторка Permissions: Default permissions / Auto-approve / Read-only, единый стиль opt-строк.
- Auto-approve: предупреждение при первом включении (Enable/Cancel), чип с классом .danger (акцент), выключен по умолчанию, сохраняется (oc_agent).
- В submit(): auto → write/edit/bash/SSH-инструменты получают action:'allow' (без запросов); plan → все SSH-записи/команды выключены (deny через tools=false); build (Default) → SSH exec/write 'ask', локальные allow по правилам restoreLocalRules (запись — ask).
- Правила реально доходят до сервера: PATCH /session/{id} permission → GET возвращает те же правила (permission_ruleset_roundtrip PASS, настоящий сервер 1.19.3).
- SSH-инструменты уже в системе прав opencode (mobile_ssh_ssh_exec/ssh_write_file с 'ask' в LocalRuntime.kt + правила сессии от клиента). Карточки подтверждения для SSH — см. fixed-client.json (прошлое доказательство Approve/Deny).
- Чип на главном экране показывает выбранный режим.

### Block A — Интерфейс
- Чип усилия показывает значение: 'Default' когда не выбрано, 'High' и т.д. после выбора (effort_chip_value PASS).
- Шторка Environment: заголовок «Environment», Build/Plan строками opt-стиля (без синей заливки), Local, пустое SSH — строка «No SSH connections» + кнопка «Add SSH connection», Web search убран (остался в Settings).
- Фон шторок = --sheet-bg: тёмная #181818, светлая #FFFFFF (sheet_color PASS).
- Низ экрана: footer сплошной var(--base), градиент только над композером (footer::before).
- Плейсхолдер тёмной темы #AFAFAF (--ph) (placeholder_contrast PASS).
- «Добавить проект» снова с «+»; Commit без шеврона (меню commit/push/PR нет — честно, без пустышки).

### Block C — Тред и карточки
- Карточка ошибки: модель + код (429 виден в тесте) + текст + кнопки Retry / Switch model; спиннер гаснет (error_no_working_spinner PASS). Дублирование с S.lastErr устранено (hadMsgError).
- Призрачная карточка: причина — changeBubble с unknown/пустым патчем рендерил пустую раскрываемую карточку «Reading history…»; теперь компактная строка без пустого тела. changeHints чистятся в resetThread.
- Markdown/HTML-экранирование/дифф-двойники не трогались (были проверены ранее: feed-evidence.md).

### Block F — Контекстное меню
- Долгое нажатие 420 мс (vibrate 10, подавление выделения классом lp-wait, contextmenu preventDefault). Скролл после долгого нажатия работает (scroll_after_lp PASS).
- Сообщение пользователя: Copy (буфер проверен), Edit & resend (баннер «Editing · will fork from here», отмена крестиком, fork по отправке), Resend, Select text.
- Сообщение ассистента: Copy, Retry, Retry with another model…, Fork from here, Select text.
- Edit & resend: POST /session/{id}/fork {messageID} → промпт уходит в форк (edit_resend_fork_body, edit_resend_prompt_to_fork PASS). Fork endpoint подтверждён в /doc (fork_endpoint_in_doc PASS).
- Карточка команды: Copy command/Copy output; карточка правки: Copy path/Copy diff (рендерится по типу карточки).
- Подсказка «Long-press a message for actions» один раз (oc_lp_hint).
- Revert/Rewind в меню не добавлены (по брифу).

### Block D / E
- Панель: папки open/close иконки, синяя точка активного треда, без «+»/направляющей — сделано в прошлом раунде, не переделывалось.
- Overscroll: OVER_SCROLL_NEVER уже в configureWebview (MainActivity.kt) — не трогали.

## Не сделано / причины
- Живой тест Block B п.1 (Approve-диалоги на запись/команду, локально и по SSH) на этом прогоне не повторялся: требуется вызов модели; прошлое доказательство — fixed-client.json (реальные permission.asked/replied, Deny, patch из metadata.files). Правила-пламбинг проверен roundtrip-тестом.
- Auto-approve «предупреждение при первом включении» показывается один раз за выбор в шторке (не per-project постоянный флаг) — совпадает с брифом «по умолчанию выключен, сохраняется».
- «Retry with another model…» переиспользует выбор без возврата в шторку прав — осознанно.
