# lucx-ui-pro

Автоматическая установка панели [lucx-ui](https://github.com/AlexeyLCP/lucx-ui) с nginx, SSL, автосозданием инбаундов, self-hosted DoH и настройкой DNS в xray.

- Debian 12,13 / Ubuntu 24,26
- Два домена или поддомена (для панели/DNS и для REALITY)
- Автоматическое обновление SSL-сертификатов
- Поддержка VLESS TCP REALITY, VLESS XHTTP TLS — через порт 443, а так же Hysteria 2, qWDTT и CSQTT

---

## Что устанавливается

| Компонент | Описание |
|-----------|----------|
| lucx-ui | VPN-панель с веб-интерфейсом |
| nginx | Обратный прокси, SNI-роутинг |
| certbot | Let's Encrypt SSL |
| Фейковый сайт | Случайный HTML-сайт-прикрытие |
| Бэкап | Скрипт резервного копирования |
| AdGuard Home | Опционально: self-hosted DNS с блокировкой рекламы (DoH) |

---

## Установка

**Скачивание и запуск скрипта**

```bash
wget -qO lucx-ui-latest.sh https://raw.githubusercontent.com/ttrroyy/lucx-ui-pro/main/lucx-ui-latest.sh
bash lucx-ui-latest.sh -install y -subdomain panel.example.com -reality_domain r.example.com
```

# panel.example.com - домен панели

# r.example.com - домен reality

# для них нужно создать A-запись на IP вашего VPS

**Полное удаление (панель + nginx + AdGuard)**

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

Повторный запуск безопасен (настройки и пароль сохраняются). После установщика или патча запустите скрипт ещё раз — они перезаписывают конфиг nginx.

Удаление:

```bash
bash lucx-ui-latest.sh -adguard-uninstall y
```

---

## Параметры запуска

| Параметр | Описание |
|----------|----------|
| `-install y` | Полная установка |
| `-subdomain <домен>` | Домен панели и подписок |
| `-reality_domain <домен>` | Домен назначения для REALITY |
| `-version <версия>` | Установить конкретную версию lucx-ui (например `v3.8.5-lucx.245`), по умолчанию — последняя |
| `-uninstall y` | Полное удаление |
| `-adguard y` | Установка AdGuard (поверх панели; панель не трогать) |
| `-adguard-uninstall y` | Удаление только AdGuard (поверх панели; панель не трогать) |

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
lucx-ui-backup restore /var/backups/lucx-ui/lucx-ui-backup-20260101-120000.tar.gz
```

Бэкап включает: конфиги nginx, БД LucX, бинарник панели, SSL, сайт-заглушка, AdGuard Home, systemd, cron, UFW. На время backup панель коротко останавливается.

---
