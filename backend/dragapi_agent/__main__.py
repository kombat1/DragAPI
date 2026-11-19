"""Удалённый агент: python -m dragapi_agent --server http://платформа:8765 --token <токен>

Токен выдаёт администратор платформы («Агенты» → «Добавить агента»). Его можно передать через
переменную DRAGAPI_AGENT_TOKEN, чтобы он не был виден в списке процессов.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import threading
import time


def _bundled_ca() -> str | None:
    """platform-ca.pem из zip агента: в папке агента или в текущей."""
    from pathlib import Path
    for d in (Path(__file__).resolve().parent.parent, Path.cwd()):
        if (d / "platform-ca.pem").is_file():
            return str(d / "platform-ca.pem")
    return None


def main():
    ap = argparse.ArgumentParser(prog="python -m dragapi_agent", description="Агент автоматизации DragAPI")
    ap.add_argument("--server", required=True, help="адрес платформы, например http://192.168.1.10:8765")
    ap.add_argument("--token", default=os.environ.get("DRAGAPI_AGENT_TOKEN"),
                    help="токен агента (или переменная DRAGAPI_AGENT_TOKEN)")
    ap.add_argument("--name", default="", help="имя этого компьютера в платформе")
    ap.add_argument("--stop-hotkey", default="ctrl+alt+esc", help="аварийная остановка (по умолчанию ctrl+alt+esc)")
    ap.add_argument("--no-notify", action="store_true", help="без системных уведомлений")
    ap.add_argument("--ca", help="сертификат центра сертификации платформы (её собственный HTTPS); "
                                 "по умолчанию — platform-ca.pem рядом с агентом, если он есть")
    args = ap.parse_args()
    if not args.token:
        sys.exit("нужен токен: --token или переменная DRAGAPI_AGENT_TOKEN")

    try:
        from websockets.exceptions import ConnectionClosed, InvalidStatus
        from websockets.sync.client import connect
    except ImportError:
        sys.exit("нужна библиотека websockets: pip install -r requirements.txt")

    from .agent import Agent

    server = args.server.rstrip("/")
    url = ("wss://" + server[8:] if server.startswith("https://") else
           "ws://" + server[7:] if server.startswith("http://") else server) + "/api/agents/ws"
    if url.startswith("ws://") and not any(h in url for h in ("://127.0.0.1", "://localhost", "://[::1]")):
        print("Внимание: соединение без шифрования (http). Токен и команды видны в сети — "
              "используйте https или доверенную сеть.", file=sys.stderr)

    ssl_ctx = None
    if url.startswith("wss://"):
        ca = args.ca or _bundled_ca()
        ssl_ctx = ssl.create_default_context()
        if ca:
            try:
                ssl_ctx.load_verify_locations(ca)   # вдобавок к системным: подойдёт и Let's Encrypt
            except (OSError, ssl.SSLError) as e:
                sys.exit(f"не удалось прочитать сертификат {ca}: {e}")

    agent = Agent(name=args.name, stop_hotkey=args.stop_hotkey, notify=not args.no_notify)
    agent.start()
    if agent.driver_error:
        print(f"Внимание: {agent.driver_error}", file=sys.stderr)
    backoff = 1
    while True:
        try:
            with connect(url, additional_headers={"Authorization": f"Bearer {args.token}"},
                         open_timeout=10, max_size=8 * 1024 * 1024, ssl=ssl_ctx) as ws:
                lock = threading.Lock()

                def send(msg, ws=ws):
                    with lock:
                        ws.send(json.dumps(msg, ensure_ascii=False))

                agent.send_fn = send
                send(agent.hello())
                print(f"Подключено к {server} как «{agent.name}». Аварийная остановка: "
                      f"{'+'.join(agent.stop_keys)}", flush=True)
                backoff = 1
                for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(msg, dict):
                        agent.handle(msg)
        except InvalidStatus as e:
            code = e.response.status_code
            if code in (401, 403):
                agent.shutdown()
                sys.exit("платформа не приняла токен агента — проверьте его или выпустите новый")
            print(f"платформа ответила {code}, повтор через {backoff} с", file=sys.stderr)
        except ssl.SSLCertVerificationError as e:
            print(f"сертификат платформы не прошёл проверку ({e.verify_message}). Если платформа на своём "
                  "сертификате (--tls-self-signed) — положите platform-ca.pem из zip агента рядом или укажите "
                  f"--ca <файл>. Повтор через {backoff} с", file=sys.stderr)
        except (OSError, ConnectionClosed, TimeoutError) as e:
            print(f"нет связи с платформой ({type(e).__name__}: {e}), повтор через {backoff} с", file=sys.stderr)
        except KeyboardInterrupt:
            agent.shutdown()
            return
        finally:
            agent.send_fn = None
            agent.on_disconnect()
        try:
            time.sleep(backoff)
        except KeyboardInterrupt:
            agent.shutdown()
            return
        backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    main()
