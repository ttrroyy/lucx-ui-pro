# lucx-ui-pro

🇬🇧 [English version](README_EN.md)

Автоматическая установка панели [lucx-ui](https://github.com/AlexeyLCP/lucx-ui) с nginx, SSL, автосозданием инбаундов, self-hosted DoH и настройкой DNS в xray.

- Debian 12 / Ubuntu 24,26
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

**Шаг 1 — скачать скрипт**

```bash
wget -qO x-ui-latest.sh https://raw.githubusercontent.com/mozaroc/3x-ui-pro/main/x-ui-latest.sh
```

**Шаг 2 — запустить**

```bash
bash x-ui-latest.sh
```

---

## AdGuard Home (опционально)

Устанавливает [AdGuard Home](https://github.com/AdguardTeam/AdGuardHome) на домен панели — без отдельного домена и открытых портов, всё через существующий 443:

- **DNS-over-HTTPS** для клиентов: `https://<домен-панели>/dns-query`
- **Админка** — на случайном пути `/adg-<random>/` (логин и пароль выводит скрипт)

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/mozaroc/3x-ui-pro/main/x-ui-adguard.sh)
```

Повторный запуск безопасен (настройки и пароль сохраняются). После установщика или патча запустите скрипт ещё раз — они перезаписывают конфиг nginx.

Удаление:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/mozaroc/3x-ui-pro/main/x-ui-adguard.sh) -uninstall y
```

---

## Удаление

```bash
bash x-ui-latest.sh -uninstall y
```

---

## Параметры запуска

| Параметр | Описание |
|----------|----------|
| `-install n` | Пропустить установку системных пакетов (по умолчанию `y`) |
| `-subdomain <домен>` | Домен панели и подписок |
| `-reality_domain <домен>` | Домен назначения для REALITY |
| `-auto_domain y` | Автоопределение домена (без ручного ввода) |
| `-version <версия>` | Установить конкретную версию 3x-ui (например `3.4.2`), по умолчанию — последняя |
| `-uninstall y` | Полное удаление |

---

## Бэкап и восстановление

**Установить скрипт бэкапа**

```bash
wget -qO /usr/local/bin/x-ui-backup https://raw.githubusercontent.com/mozaroc/3x-ui-pro/main/assets/backup/x-ui-backup.sh
chmod +x /usr/local/bin/x-ui-backup
```

**Создать бэкап**

```bash
x-ui-backup backup
```

**Список бэкапов**

```bash
x-ui-backup list
```

**Восстановить из бэкапа** (на чистом сервере, пакеты ставятся автоматически)

```bash
x-ui-backup restore /var/backups/x-ui/x-ui-backup-20260101-120000.tar.gz
```

Бэкап включает: конфиги nginx, БД панели, бинарник 3x-ui, SSL-сертификаты, веб-контент, systemd-юниты, cron, правила UFW.

---
