# Миграции совместимости

`pro-compat.py` — источник общей логики установщика и восстановления. Его копия встроена в оба shell-скрипта, поэтому для работы миграций не требуется загрузка дополнительного Python-файла с GitHub.

После правки исходника обновите обе встроенные копии:

```bash
python3 assets/compat/sync-embedded.py
bash -n lucx-ui-latest.sh
bash -n assets/backup/lucx-ui-backup.sh
python3 -m unittest discover -s tests
```

`awg-bbr.py` удаляет только BBR/FQ из авторского AWG installer и его performance-файла. Модуль, ABI, DKMS и параметры установки остаются авторскими. При обслуживании старых установок и восстановлении backup удаляются прежние Pro wrappers и служба готовности; при необходимости исходный installer восстанавливается из точного тега установленной панели. Для этого нужен доступ к GitHub.

Связи `client_inbounds` и записи `clients` служат источником для `settings.clients` только у QWDTT, CSQTT, Telegram WEB-proxy, olcRTC и OpenFlux. Миграция очищает осиротевшие связи, восстанавливает существующие списки и устанавливает триггеры INSERT/DELETE/UPDATE связей, UPDATE полей клиента, сохранения settings инбаунда и удаления клиента/инбаунда. Остальные протоколы сохраняют свои настройки клиентов.

`apply` дополнительно переводит CSQTT в штатный direct-режим, исправляет native Clash provider и схему подключения nginx к панели, удаляет старый Pro override forwarding. UFW policy и runtime `ip_forward` не меняются этим Python helper.

Запускайте обслуживание VPS через `lucx-ui-latest.sh -update y`: с версии панели 281 этот режим выполняет только обновление: предупреждение, backup, остановку панели, миграции, сохранение BBR/FQ и проверки нашей схемы с файловым откатом при ошибке. Прямой `pro-compat.py apply` не создаёт backup и предназначен для внутреннего использования.
