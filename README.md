# lucx-ui-pro

Автоматическая установка панели [lucx-ui](https://github.com/AlexeyLCP/lucx-ui) с nginx, SSL, автосозданием инбаундов, автогенерацией доменов (только для теста, повышенный риск бана ТСПУ), сайтом заглушкой, self-hosted DoH, fail2ban, защитой от сканеров, настройкой DNS в xray, а также поддержкой Clash/Mihomo подписки.

- Debian 12,13 / Ubuntu 24,26
- Один домен или поддомен с A-записью на IPv4 VPS: общий для панели, подписок/DNS, REALITY, XHTTP TLS и опционального Telegram WEB-proxy.

---

## Что устанавливается

Поддержка VLESS TCP REALITY, VLESS XHTTP TLS, Telegram WEB-proxy — через порт 443, а также [Hysteria 2](https://github.com/apernet/hysteria) на выбранном UDP-порту. Hysteria 2 можно добавить только при полной установке.

| Компонент | Описание |
|-----------|----------|
| [lucx-ui](https://github.com/AlexeyLCP/lucx-ui) | VPN-панель с веб-интерфейсом |
| [nginx](https://nginx.org/) | Обратный прокси, SNI-роутинг |
| [certbot](https://certbot.eff.org/) | Let's Encrypt SSL |
| Фейковый сайт | Случайный HTML-сайт-прикрытие |
| Бэкап | Скрипт резервного копирования |
| AdGuard Home | Опционально: self-hosted DNS с блокировкой рекламы (DoH) |
| rkn-guard | Опционально: защита от сканеров РКН |
| BitTorrent Guard | Опционально: блокировка торрент-трафика по сигнатурам |
| [fail2ban](https://github.com/fail2ban/fail2ban) | Защита от сканеров |

---

## Установка

Все команды ниже сначала скачивают актуальный скрипт из GitHub. Если скачивание завершится ошибкой, действие не запустится. Само скачивание версию установленной панели не меняет.

**Скачивание и запуск скрипта**

```bash
curl -fsSL https://raw.githubusercontent.com/ttrroyy/lucx-ui-pro/main/lucx-ui-latest.sh -o /root/lucx-ui-latest.sh && bash /root/lucx-ui-latest.sh -install y
```

Повторный запуск полностью удаляет прежнюю установку и ставит новую после подтверждения. Клиенты и настройки удаляются; сертификаты сохраняются. Для возврата прежних данных используйте восстановление из backup.

## Обновление установленной панели

`-update` доступен с **v3.9.0-lucx.286** и новее для установки, созданной lucx-ui-pro. Обновляет панель штатным установщиком и применяет актуальную совместимость нашей схемы. На версии ниже 286 команда остановится с сообщением о неподдерживаемой версии, без обновления панели.

**Скачать свежий скрипт и открыть меню обновления:**

```bash
curl -fsSL https://raw.githubusercontent.com/ttrroyy/lucx-ui-pro/main/lucx-ui-latest.sh -o /root/lucx-ui-latest.sh && bash /root/lucx-ui-latest.sh -update y
```

По умолчанию выбирается последний стабильный релиз AlexeyLCP/lucx-ui. В меню можно обновить панель вместе с архитектурой, обновить только архитектуру с сохранением версии панели или отменить действие. Уже установленная версия повторно не обновляется; понижение блокируется.

**Обновить до конкретного релиза** (должен быть новее установленного):

```bash
curl -fsSL https://raw.githubusercontent.com/ttrroyy/lucx-ui-pro/main/lucx-ui-latest.sh -o /root/lucx-ui-latest.sh && bash /root/lucx-ui-latest.sh -update y -version v3.9.0-lucx.287
```

Перед изменениями создаётся backup в `/var/backups/x-ui`. Проверяются клиентские связи и настройки нашей схемы; остальные поля, которыми управляет автор панели, могут изменяться при штатной миграции. При обнаруженной ошибке возвращаются сохранённые файлы и БД. Системные пакеты и загруженный модуль ядра файловым откатом не возвращаются. Соединения временно прерываются.

> `-install y` — полная переустановка с удалением прежних данных после подтверждения.

**Полное удаление**

```bash
curl -fsSL https://raw.githubusercontent.com/ttrroyy/lucx-ui-pro/main/lucx-ui-latest.sh -o /root/lucx-ui-latest.sh && bash /root/lucx-ui-latest.sh -uninstall y
```

---

## Компоненты

Открыть общее меню:

```bash
curl -fsSL https://raw.githubusercontent.com/ttrroyy/lucx-ui-pro/main/lucx-ui-latest.sh -o /root/lucx-ui-latest.sh && bash /root/lucx-ui-latest.sh -components y
```

Выберите **Установить**, **Переустановить** или **Удалить**, затем введите номера нужных компонентов через пробел или запятую. В меню видно, какие компоненты активны, отключены или не установлены. Для уже установленного компонента действие «Установить» использует ту же логику, что и переустановка.

| Компонент | Описание |
|-----------|----------|
| [AdGuard Home](https://github.com/AdguardTeam/AdGuardHome) | DNS с блокировкой рекламы. DoH: `https://<домен-панели>/dns-query`, админка — на случайном пути `/adg-<random>/`. Настройки и пароль сохраняются при повторной установке. Если Xray использует этот DoH, удаление потребует подтверждения после предупреждения. |
| [RKN Guard](https://github.com/Flecksis/rkn-guard) | Защита от сканеров, автообновление программы и списков IP. |
| [BitTorrent Guard](https://github.com/VIBEHOST-LLC/vibehost-guard) | Сигнатурная защита от торрент-трафика, без ключа и регистрации. Работает и отдельно от панели; сохраняется при её удалении. |
| [Telegram WEB-proxy](https://github.com/telegramdesktop/tproxy-server) | Подключение Telegram через общий домен и 443. При переустановке выдаётся новая ссылка. |

Включённый BitTorrent Guard обновляется при обычном `-update y`. Блокировка зашифрованного или обфусцированного BitTorrent не гарантируется.

---

## Параметры запуска

| Параметр | Описание |
|----------|----------|
| `-install y` | Полная установка |
| `-version <версия>` | Выбрать релиз для установки или обновления (например `v3.9.0-lucx.286`), по умолчанию — последний стабильный |
| `-update y` | Обновить установленную через Pro панель версии 286 и новее |
| `-uninstall y` | Полное удаление установки с сохранением сертификатов |
| `-components y` | Меню установки, переустановки и удаления компонентов |

---

## Clash-подписка

Работает через определение User-Agent — один URL, разное поведение:

• Clash / Mihomo / Stash → получают clash.yaml с готовой конфигурацией

• Обычный браузер / другие клиенты → получают стандартную страницу подписки 3x-ui

---

## Бэкап и восстановление

**Установить скрипт бэкапа**

```bash
curl -fsSL https://raw.githubusercontent.com/ttrroyy/lucx-ui-pro/main/assets/backup/lucx-ui-backup.sh -o /usr/local/bin/lucx-ui-backup && chmod +x /usr/local/bin/lucx-ui-backup
```

Каждая команда ниже скачивает свежую версию скрипта бэкапа; отдельная предварительная установка не требуется.

**Создать бэкап**

```bash
curl -fsSL https://raw.githubusercontent.com/ttrroyy/lucx-ui-pro/main/assets/backup/lucx-ui-backup.sh -o /usr/local/bin/lucx-ui-backup && bash /usr/local/bin/lucx-ui-backup backup
```

**Список бэкапов**

```bash
curl -fsSL https://raw.githubusercontent.com/ttrroyy/lucx-ui-pro/main/assets/backup/lucx-ui-backup.sh -o /usr/local/bin/lucx-ui-backup && bash /usr/local/bin/lucx-ui-backup list
```

**Восстановить из бэкапа** (на чистом сервере, пакеты ставятся автоматически)

```bash
curl -fsSL https://raw.githubusercontent.com/ttrroyy/lucx-ui-pro/main/assets/backup/lucx-ui-backup.sh -o /usr/local/bin/lucx-ui-backup && bash /usr/local/bin/lucx-ui-backup restore /var/backups/x-ui/lucx-ui-backup-20260101-120000.tar.gz
```

Бэкап включает: конфиги nginx, БД LucX, бинарник панели, SSL, сайт-заглушка, AdGuard Home, systemd, cron, UFW. На время backup панель коротко останавливается.

---

## Поддержать проект

Понравилось — ставь ⭐ репозиторию: это помогает другим найти проект и поддерживает разработку. 

---
