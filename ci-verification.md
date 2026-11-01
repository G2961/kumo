# APK через GitHub Releases

Workflow уже переделан: `.github/workflows/build.yml` собирает
`assembleDebug assembleRelease` в GitHub Actions и публикует release APK
через `gh release upload` с `contents: write`. `upload-artifact` отсутствует.
Теги публикации: `apk-latest`, `build-${github.run_number}`.
Workflow менять не потребовалось; релиз `ci` в этом репозитории отсутствует
и команды его изменения не выполнялись.

Проверенный build 5:
[Actions run](https://github.com/G2961/opencode-android-new/actions/runs/41290341283),
commit `d34f57f2d0c2c78b78a1e86eeda05f4a67c078d7`, conclusion `success`.
APK появился в [build-5](https://github.com/G2961/opencode-android-new/releases/tag/build-5)
и `apk-latest`: `opencode-android-5.apk`, 75 309 971 байт.

Это доказывает сборку исправлений клиента и Kotlin-диагностики.
После последнего коммита отдельно проверяется финальная сборка с формой
Providers; ссылка и результат приводятся в итоговом ответе.

Репозиторий остаётся PRIVATE. `codex.html` отслеживается Git.
`app/src/main/jniLibs/` и `app/src/main/assets/rootfs.tgz` не отслеживаются.
Локальная сборка Android не запускалась. Работа APK на телефоне требует
проверок из `phone-diagnostics.md` и `provider-setup.md`.
