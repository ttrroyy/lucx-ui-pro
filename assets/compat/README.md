# Миграции совместимости

`pro-compat.py` — источник общей логики установщика и восстановления. Его копия встроена в оба shell-скрипта, поэтому для работы миграций не требуется загрузка дополнительного Python-файла с GitHub.

После правки исходника обновите обе встроенные копии:

```bash
python3 assets/compat/sync-embedded.py
bash -n lucx-ui-latest.sh
bash -n assets/backup/lucx-ui-backup.sh
python3 -m unittest discover -s tests
```

`awg-compat.py` — отдельный источник AWG guard и проверки готовности. Его копии обновляются той же командой `sync-embedded.py`. Собственный compile probe и изменения Kbuild/UDP macros удалены: используются штатные функции установщика 279/280. Для старых backup, в которых Pro подменял UDP-функцию, helper восстанавливает точную функцию из релиза 280. Авторские timer_delete/ChaCha/Blake2s фиксы сохраняются. Pro добавляет только DKMS MAKE override для идентификации версии и контроль готовности, а shell guard сохраняет выбранную пользователем политику BBR/FQ.

Связи `client_inbounds` и записи `clients` служат источником для `settings.clients` только у QWDTT, CSQTT, Telegram WEB-proxy и olcRTC. Миграция очищает осиротевшие связи, восстанавливает существующие списки и устанавливает триггеры INSERT/DELETE/UPDATE связей, UPDATE email/enable клиента и удаления клиента/инбаунда. Остальные протоколы сохраняют свои настройки клиентов.

`apply` дополнительно переводит CSQTT в штатный direct-режим, исправляет native Clash provider и схему подключения nginx к панели, удаляет старый Pro override forwarding. UFW policy и runtime `ip_forward` не меняются этим Python helper.

Запускайте обслуживание VPS через `lucx-ui-latest.sh -update y`: этот режим отвечает за предупреждение, backup, остановку панели, миграции, исправление AWG/BBR wrapper, проверки и восстановление файлов при ошибке. Прямой `pro-compat.py apply` не создаёт backup и предназначен для внутреннего использования.
