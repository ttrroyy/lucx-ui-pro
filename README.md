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

## Обновление установленной панели

`-update` доступен с **v3.9.0-lucx.286** и новее для установки, созданной lucx-ui-pro. Обновляет панель штатным установщиком и применяет актуальную совместимость нашей схемы.

**Скачать свежий скрипт и открыть меню обновления:**

```bash
curl -fsSL https://raw.githubusercontent.com/ttrroyy/lucx-ui-pro/main/lucx-ui-latest.sh -o /root/lucx-ui-latest.sh && bash /root/lucx-ui-latest.sh -update y
```

По умолчанию выбирается последний стабильный релиз AlexeyLCP/lucx-ui. В меню — обновление или отмена. Уже установленная версия повторно не обновляется; понижение блокируется.

**Выбрать конкретный релиз:**

```bash
bash /root/lucx-ui-latest.sh -update y -version v3.9.0-lucx.286
```

Перед изменениями создаётся backup в `/var/backups/x-ui`. Проверяются клиентские связи и настройки нашей схемы; остальные поля, которыми управляет автор панели, могут изменяться при штатной миграции. При обнаруженной ошибке возвращаются сохранённые файлы и БД. Системные пакеты и загруженный модуль ядра файловым откатом не возвращаются. Соединения временно прерываются.

> `-install y` — полная переустановка с удалением прежних данных после подтверждения.

**Полное удаление**

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
