"""Manage the local macOS service independently of Terminal and coding sessions."""

import argparse
import os
import plistlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABEL = "ru.megapolis.intelligence"
DOMAIN = f"gui/{os.getuid()}"
SERVICE = f"{DOMAIN}/{LABEL}"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
URL = "http://127.0.0.1:8765"


def launchctl(*args):
    return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, check=False)


def healthy():
    try:
        with urllib.request.urlopen(URL + "/healthz", timeout=1) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError):
        return False


def start():
    executable = ROOT / ".venv/bin/radar"
    if not executable.exists():
        raise RuntimeError("Сначала установите зависимости: uv sync --frozen")
    logs = ROOT / "data/logs"
    logs.mkdir(parents=True, exist_ok=True)
    config = {
        "Label": LABEL,
        "ProgramArguments": [str(executable), "serve", "--host", "127.0.0.1", "--port", "8765"],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "StandardOutPath": str(logs / "server.stdout.log"),
        "StandardErrorPath": str(logs / "server.stderr.log"),
        "EnvironmentVariables": {
            "PATH": f"{ROOT / '.venv/bin'}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
        },
    }
    data = plistlib.dumps(config)
    loaded = launchctl("print", SERVICE).returncode == 0
    if not loaded and healthy():
        raise RuntimeError(
            "На порту 8765 уже работает сервер вне фоновой службы. "
            "Остановите его перед установкой службы."
        )
    changed = not PLIST.exists() or PLIST.read_bytes() != data
    if changed:
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        temporary = PLIST.with_suffix(".tmp")
        temporary.write_bytes(data)
        temporary.chmod(0o600)
        temporary.replace(PLIST)
        if loaded:
            result = launchctl("bootout", SERVICE)
            if result.returncode:
                raise RuntimeError("Не удалось обновить службу: " + result.stderr.strip())
            loaded = False
    if not loaded:
        result = launchctl("bootstrap", DOMAIN, str(PLIST))
        if result.returncode:
            raise RuntimeError("Не удалось запустить службу: " + result.stderr.strip())
    for _ in range(60):
        if healthy():
            print(f"Мегаполис запущен в фоне: {URL}")
            return
        time.sleep(0.25)
    raise RuntimeError(f"Служба установлена, но сервер не отвечает. Журнал: {logs}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status", "uninstall"])
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("Этот способ запуска предназначен для macOS")
    if args.action == "start":
        start()
    elif args.action in {"stop", "uninstall"}:
        if launchctl("print", SERVICE).returncode == 0:
            result = launchctl("bootout", SERVICE)
            if result.returncode:
                raise RuntimeError("Не удалось остановить службу: " + result.stderr.strip())
            # bootout can return before the old process releases its listener.
            # A following start must not mistake that process for the new one.
            for _ in range(60):
                if launchctl("print", SERVICE).returncode != 0 and not healthy():
                    break
                time.sleep(0.25)
            else:
                raise RuntimeError("Служба ещё завершает работу. Повторите запуск после остановки.")
        if args.action == "uninstall":
            PLIST.unlink(missing_ok=True)
        print("Служба остановлена" if args.action == "stop" else "Автозапуск удалён")
    else:
        loaded = launchctl("print", SERVICE).returncode == 0
        print(f"Служба: {'загружена' if loaded else 'не загружена'}")
        print(f"Сайт: {URL} — {'доступен' if healthy() else 'не отвечает'}")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
