"""Запуск сервера платформы: python -m app [--no-auth] [--host H] [--port P] [--reload] [--config FILE]
                                         [--tls-self-signed | --tls-cert F --tls-key F] [--behind-proxy]."""

from __future__ import annotations

import argparse
import os
import sys


def main():
    if sys.platform == "win32":   # консоль Windows: кириллица в выводе не должна ронять сервер
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
    ap = argparse.ArgumentParser(prog="python -m app", description="Сервер платформы DragAPI")
    ap.add_argument("--no-auth", action="store_true",
                    help="без входа; разрешено, только пока в платформе нет пользователей и только на 127.0.0.1")
    ap.add_argument("--host", help="адрес (по умолчанию из dragapi.toml или 127.0.0.1; 0.0.0.0 — все сети)")
    ap.add_argument("--port", type=int, help="порт (по умолчанию из dragapi.toml или 8765)")
    ap.add_argument("--reload", action="store_true", help="перезапуск при изменении кода (разработка)")
    ap.add_argument("--config", help="файл настроек вместо backend/dragapi.toml")
    ap.add_argument("--no-local-agent", action="store_true",
                    help="не управлять мышью и клавиатурой этой машины (сценарии — только на удалённых агентах)")
    tls = ap.add_argument_group("HTTPS и работа на сервере")
    tls.add_argument("--tls-self-signed", action="store_true",
                     help="HTTPS с собственным сертификатом платформы (выпускается сам, файлы — в data/tls/)")
    tls.add_argument("--tls-cert", help="HTTPS со своим сертификатом: файл сертификата (PEM, с цепочкой)")
    tls.add_argument("--tls-key", help="закрытый ключ к --tls-cert (PEM)")
    tls.add_argument("--behind-proxy", action="store_true",
                     help="HTTPS делает обратный прокси (Caddy, nginx): доверять его заголовкам X-Forwarded-*")
    args = ap.parse_args()

    if args.config:
        os.environ["DRAGAPI_CONFIG"] = os.path.abspath(args.config)
    if args.no_local_agent:
        os.environ["DRAGAPI_LOCAL_AGENT"] = "off"
    if args.no_auth:
        os.environ["DRAGAPI_AUTH"] = "off"  # дочерние процессы (--reload) видят то же

    from . import auth, config, tls as tlsmod
    try:
        cfg = dict(config.load())
        auth.check_startup()
    except (auth.AuthError, config.ConfigError) as e:
        sys.exit(f"DragAPI: {e}")

    # флаги важнее файла
    if args.host:
        cfg["host"] = args.host
    if args.port:
        cfg["port"] = args.port
    if args.tls_self_signed:
        cfg.update(tls_self_signed=True, tls_cert=None, tls_key=None)
    if args.tls_cert or args.tls_key:
        cfg.update(tls_cert=args.tls_cert, tls_key=args.tls_key, tls_self_signed=False)
    if args.behind_proxy:
        cfg["behind_proxy"] = True

    p = tlsmod.plan(cfg, auth_off=auth.off_requested()[0])
    for w in p.warnings:
        print(f"DragAPI: внимание — {w}", file=sys.stderr)
    if p.errors:
        sys.exit("DragAPI: " + "\nDragAPI: ".join(p.errors))

    if p.mode == "self-signed":
        try:
            cert, key, fresh = tlsmod.ensure_self_signed(tlsmod.local_names(cfg.get("tls_names"), p.host))
        except Exception as e:  # noqa: BLE001 — понятное сообщение вместо трассы
            sys.exit(f"DragAPI: не удалось выпустить сертификат: {type(e).__name__}: {e}")
        p.cert, p.key = str(cert), str(key)
        ca = tlsmod.ca_path()
        print(f"DragAPI: HTTPS с сертификатом платформы{' (выпущен заново)' if fresh else ''}.\n"
              f"  Чтобы браузер не предупреждал, добавьте в доверенные центр сертификации {ca}\n"
              f"  (отпечаток SHA-256 {tlsmod.fingerprint(ca)}). Удалённые агенты получают его в zip сами.",
              file=sys.stderr)
    os.environ["DRAGAPI_TLS_MODE"] = p.mode or ""

    import uvicorn
    os.environ["DRAGAPI_PORT"] = str(p.port)   # превью API-проектов вызывают сценарии через свой же сервер
    if p.scheme == "https" and not os.environ.get("DRAGAPI_URL"):
        os.environ["DRAGAPI_URL"] = f"https://127.0.0.1:{p.port}"
    print(f"DragAPI: {p.url}", file=sys.stderr)
    uvicorn.run(
        "app.main:app", host=p.host, port=p.port, reload=args.reload,
        ssl_certfile=p.cert, ssl_keyfile=p.key,
        # за прокси схема (https) и адрес клиента берутся из X-Forwarded-*, но только от доверенных адресов
        proxy_headers=True,
        forwarded_allow_ips=(cfg.get("trusted_proxies") or "127.0.0.1") if p.mode == "proxy" else "127.0.0.1",
    )


def backup_cli(argv: list[str]) -> None:
    """python -m app backup [ФАЙЛ] [--password-env ПЕРЕМЕННАЯ]  ·  python -m app restore ФАЙЛ [--password-env …]"""
    ap = argparse.ArgumentParser(prog=f"python -m app {argv[0]}")
    if argv[0] == "backup":
        ap.add_argument("file", nargs="?", help="куда сохранить (по умолчанию dragapi-backup-<дата>.zip)")
    else:
        ap.add_argument("file", help="файл копии (.zip или .dragapi-backup.enc)")
    ap.add_argument("--password-env", help="имя переменной окружения с паролем копии (зашифровать / расшифровать)")
    args = ap.parse_args(argv[1:])
    import time
    from . import backup
    from .paths import DATA
    password = os.environ.get(args.password_env, "") if args.password_env else None
    if args.password_env and not password:
        sys.exit(f"переменная {args.password_env} пуста")
    try:
        if argv[0] == "backup":
            out = args.file or time.strftime("dragapi-backup-%Y%m%d-%H%M") + (".dragapi-backup.enc" if password else ".zip")
            blob = backup.create(DATA, password)
            with open(out, "wb") as f:
                f.write(blob)
            print(f"Резервная копия: {out} ({len(blob) / 1e6:.1f} МБ). В ней ключ шифрования секретов — "
                  f"храните её как пароль{'' if password else ' или делайте копию с --password-env'}.")
        else:
            with open(args.file, "rb") as f:
                manifest = backup.stage_restore(DATA, f.read(), password)
            print(f"Копия от {time.strftime('%Y-%m-%d %H:%M', time.localtime(manifest['created']))} подготовлена "
                  f"({manifest.get('projects', '?')} проектов). Она применится при следующем запуске платформы; "
                  f"текущие данные будут отложены рядом.")
    except (backup.BackupError, OSError) as e:
        sys.exit(f"DragAPI: {e}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("backup", "restore"):
        backup_cli(sys.argv[1:])
    else:
        main()
