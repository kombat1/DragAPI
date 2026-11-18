"""Запуск сервера платформы: python -m app [--no-auth] [--host H] [--port P] [--reload] [--config FILE]."""

from __future__ import annotations

import argparse
import os
import sys


def main():
    ap = argparse.ArgumentParser(prog="python -m app", description="Сервер платформы DragAPI")
    ap.add_argument("--no-auth", action="store_true",
                    help="без входа; разрешено, только пока в платформе нет пользователей")
    ap.add_argument("--host", help="адрес (по умолчанию из dragapi.toml или 127.0.0.1)")
    ap.add_argument("--port", type=int, help="порт (по умолчанию из dragapi.toml или 8765)")
    ap.add_argument("--reload", action="store_true", help="перезапуск при изменении кода (разработка)")
    ap.add_argument("--config", help="файл настроек вместо backend/dragapi.toml")
    ap.add_argument("--no-local-agent", action="store_true",
                    help="не управлять мышью и клавиатурой этой машины (сценарии — только на удалённых агентах)")
    args = ap.parse_args()

    if args.config:
        os.environ["DRAGAPI_CONFIG"] = os.path.abspath(args.config)
    if args.no_local_agent:
        os.environ["DRAGAPI_LOCAL_AGENT"] = "off"
    if args.no_auth:
        os.environ["DRAGAPI_AUTH"] = "off"  # дочерние процессы (--reload) видят то же

    from . import auth, config
    try:
        cfg = config.load()
        auth.check_startup()
    except (auth.AuthError, config.ConfigError) as e:
        sys.exit(f"DragAPI: {e}")

    import uvicorn
    port = args.port or cfg.get("port", 8765)
    os.environ["DRAGAPI_PORT"] = str(port)   # превью API-проектов вызывают сценарии через свой же сервер
    uvicorn.run("app.main:app", host=args.host or cfg.get("host", "127.0.0.1"), port=port, reload=args.reload)


if __name__ == "__main__":
    main()
