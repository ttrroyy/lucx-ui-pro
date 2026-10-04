# lucx-ui-pro

Автоматическая установка панели [lucx-ui](https://github.com/AlexeyLCP/lucx-ui) с nginx, SSL, автосозданием инбаундов, автогенерацией доменов (только для теста, повышенный риск бана ТСПУ), сайтом заглушкой, self-hosted DoH, fail2ban, защитой от сканеров, настройкой DNS в xray, а также поддержкой Clash/Mihomo подписки.

- Debian 12,13 / Ubuntu 24,26
- Два домена или поддомена (для панели/DNS и для REALITY), третий для Telegram WEB-proxy (опционально)

---

## Что устанавливается

Поддержка VLESS TCP REALITY, VLESS XHTTP TLS, Telegram WEB-proxy — через порт 443, а так же Hysteria 2, qWDTT и CSQTT

| Компонент | Описание |
|-----------|----------|
| lucx-ui | VPN-панель с веб-интерфейсом |
| nginx | Обратный прокси, SNI-роутинг |
| certbot | Let's Encrypt SSL |
| Фейковый сайт | Случайный HTML-сайт-прикрытие |
| Бэкап | Скрипт резервного копирования |
| AdGuard Home | Опционально: self-hosted DNS с блокировкой рекламы (DoH) |
| rkn-guard | Опционально: защита от сканеров РКН |
| fail2ban | защита от сканеров |

---

## Установка

**Скачивание и запуск скрипта**

```bash
wget -qO lucx-ui-latest.sh https://raw.githubusercontent.com/ttrroyy/lucx-ui-pro/main/lucx-ui-latest.sh
bash lucx-ui-latest.sh -install y
```

Повторный запуск запускает полное удаление и повторную установку.

## Проверка и обновление существующего VPS

Для обслуживания уже установленной панели используйте `-check` / `-update`. Требуется установка, созданная этим скриптом, с `/var/lib/lucx-ui-preinstall/owned-by-lucx-ui-pro`.

Проверить версии и текущую схему без изменения настроек панели:

```bash
curl -fsSL https://raw.githubusercontent.com/ttrroyy/lucx-ui-pro/main/lucx-ui-latest.sh -o /root/lucx-ui-latest.sh && bash /root/lucx-ui-latest.sh -check y
```

Проверить и открыть селектор обслуживания:

```bash
curl -fsSL https://raw.githubusercontent.com/ttrroyy/lucx-ui-pro/main/lucx-ui-latest.sh -o /root/lucx-ui-latest.sh && bash /root/lucx-ui-latest.sh -update y
```

Зафиксировать целевой релиз вместо последнего стабильного:

```bash
bash /root/lucx-ui-latest.sh -update y -version v3.9.0-lucx.280
```

Скрипт получает установленный релиз из `x-ui -v`, последний стабильный релиз панели и текущий коммит Pro из GitHub, показывает инбаунды, режимы маршрутизации, связки клиентов, AWG/DKMS, BBR/qdisc, UFW и наличие дополнительных компонентов. Старые VPS без записи ревизии обозначаются как `legacy/unknown`; фактические настройки читаются с VPS.

После предупреждения доступны три пункта: обновить панель и исправить совместимость, исправить текущую версию без замены бинарника, отменить. Неверный ввод повторяет вопрос; EOF отменяет обслуживание. До выбора миграции не выполняются. `-install y` по-прежнему означает полную переустановку.

Перед обслуживанием сохраняется полный backup в `/var/backups/x-ui`; неудачный backup останавливает процедуру. Релиз загружается и проверяется по SHA256 до замены бинарников. Аккаунты, порты, сертификаты, содержимое сайта, DNS, AdGuard и RKN сохраняются. При обновлении не запускается начальная настройка панели. Загруженные sidecar-бинарники, их конфиги и геоданные сохраняются, если отсутствуют в архиве релиза. При ошибке миграции/nginx/готовности AWG возвращаются сохранённые файлы и DB. Состояние уже загруженного модуля ядра и установки системных пакетов этим файловым откатом не отменяется; полный backup остаётся доступным.

В текущей ревизии проверенные миграции предусмотрены для `v3.8.5-lucx.279` и `v3.9.0-lucx.280`. Неизвестный будущий релиз показывается в отчёте, но автоматическое обновление до него прекращается, пока в свежем скрипте не появятся проверенные правила. Понижение версии блокируется. Архитектуры релиза: Linux amd64/arm64.

Изменения поведения: CSQTT переводится в штатный `routeThroughXray=false` — direct NAT, в обход Xray routing/DNS. Pro больше не пишет `ip_forward=1`; это делает панель при запуске туннелей. `ufw default allow routed` сохраняется для общей схемы AWG/QWDTT. Правки применяются также при восстановлении старого backup.

Для AmneziaWG используется штатный UDP ABI-фикс панели, включая перенос старого Pro wrapper на авторскую функцию. Сохраняются проверка DKMS/загруженного модуля и защита выбранного BBR/FQ. После перехода со старого `-lucxudp2` выполняется одна пересборка с идентификатором `-lucxpro280`. В режиме обслуживания AWG installer получает `--no-kernel-upgrade`: ядро VPS автоматически не обновляется и VPS не перезагружается.

Исходники встроенных миграций и инструкция сопровождения находятся в [`assets/compat`](assets/compat/README.md).

**Полное удаление (панель + nginx + AdGuard + rkn-guard + tg-web-proxy)**

```bash
bash lucx-ui-latest.sh -uninstall y
```

---

## AdGuard Home (опционально; поверх панели)

Устанавливает [AdGuard Home](https://github.com/AdguardTeam/AdGuardHome) на домен панели — без отдельного домена и открытых портов, всё через существующий 443:

- **DNS-over-HTTPS** для клиентов: `https://<домен-панели>/dns-query`
- **Админка** — на случайном пути `/adg-<random>/` (логин и пароль выводит скрипт)

```bash
bash lucx-ui-latest.sh -adguard y
```

Повторный запуск безопасен (настройки и пароль сохраняются), он просто перезаписывает nginx.

Удаление:

```bash
bash lucx-ui-latest.sh -adguard-uninstall y
```

---

## rkn-guard (опционально; поверх панели)

Устанавливает [rkn-guard](https://github.com/Flecksis/rkn-guard) для защиты от сканеров и сетевого шума, настраивает автообновление программы и баз IP-адресов сканеров.

```bash
bash lucx-ui-latest.sh -rkn-guard y
```

Повторный запуск переустановит программу.

Удаление:

```bash
bash lucx-ui-latest.sh -rkn-guard-uninstall y
```

---

## Telegram WEB-proxy (опционально; поверх панели)

Устанавливает [telegram web-proxy](https://github.com/telegramdesktop/tproxy-server) в панель, выдает ссылку для подключения.

```bash
bash lucx-ui-latest.sh -tg-web-proxy y
```

Повторный запуск переустановит Telegram WEB-proxy и выдаст новую ссылку для подключения.

Удаление:

```bash
bash lucx-ui-latest.sh -tg-web-proxy-uninstall y
```

---

## Параметры запуска

| Параметр | Описание |
|----------|----------|
| `-install y` | Полная установка |
| `-version <версия>` | Установить конкретную версию lucx-ui (например `v3.8.5-lucx.245`), по умолчанию — последняя |
| `-adguard y` | Установка AdGuard Home |
| `-adguard-uninstall y` | Удаление AdGuard Home |
| `-rkn-guard y` | Устанока rkn-guard |
| `-rkn-guard-uninstall y` | Удаление rkn-guard |
| `-tg-web-proxy y` | Устанока tg-web-proxy |
| `-tg-web-proxy-uninstall y` | Удаление tg-web-proxy |

---

## Clash-подписка

Работает через определение User-Agent — один URL, разное поведение:

• Clash / Mihomo / Stash → получают clash.yaml с готовой конфигурацией

• Обычный браузер / другие клиенты → получают стандартную страницу подписки 3x-ui

---

## Бэкап и восстановление

**Установить скрипт бэкапа**

```bash
wget -qO /usr/local/bin/lucx-ui-backup https://raw.githubusercontent.com/ttrroyy/lucx-ui-pro/main/assets/backup/lucx-ui-backup.sh
chmod +x /usr/local/bin/lucx-ui-backup
```

**Создать бэкап**

```bash
lucx-ui-backup backup
```

**Список бэкапов**

```bash
lucx-ui-backup list
```

**Восстановить из бэкапа** (на чистом сервере, пакеты ставятся автоматически)

```bash
lucx-ui-backup restore /var/backups/x-ui/lucx-ui-backup-20260101-120000.tar.gz
```

Бэкап включает: конфиги nginx, БД LucX, бинарник панели, SSL, сайт-заглушка, AdGuard Home, systemd, cron, UFW. На время backup панель коротко останавливается.

---

## Поддержать проект

Понравилось — ставь ⭐ репозиторию: это помогает другим найти проект и поддерживает разработку. 

---
