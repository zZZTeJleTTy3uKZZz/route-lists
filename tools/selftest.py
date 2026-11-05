#!/usr/bin/env python3
"""Самопроверка склейки на местных образцах: сеть не нужна.

Проверяет то, что нельзя увидеть глазами в готовом файле:

* свои добавки стоят первыми блоками и не перетираются источником;
* своя запись главнее источника, в том числе в другом списке;
* повторный запуск даёт тот же файл;
* пустой источник, источник с объёмом на порядок меньше прежнего и негодная
  запись в своих добавках отменяют склейку, а рабочие файлы остаются прежними.

Запуск: python3 tools/selftest.py (код возврата 0 — проверка прошла).
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / ".git" / "cache" / "selftest"
FIXTURES = WORK / "root"
SOURCES = WORK / "src"
BUILD = ROOT / "tools" / "build_lists.py"
VALIDATE = ROOT / "tools" / "validate.py"

CHECKS = {"passed": 0, "failed": 0}


def check(condition, message):
    if condition:
        CHECKS["passed"] += 1
        print(f"ok - {message}")
        return True
    CHECKS["failed"] += 1
    print(f"FAIL - {message}")
    return False


def run_build():
    arguments = [sys.executable, "-I", str(BUILD), "--root", str(FIXTURES), "--work-dir", str(WORK / "build")]
    for source in ("amnezia-ip-lite", "itdog-outside", "itdog-inside", "refilter-domains"):
        arguments.extend(["--source-file", f"{source}={SOURCES / (source + '.txt')}"])
    return subprocess.run(arguments, capture_output=True, text=True)


def run_validate(directory):
    return subprocess.run([sys.executable, "-I", str(VALIDATE), str(directory)], capture_output=True, text=True)


def document(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def snapshot():
    return {
        name: (FIXTURES / name).read_bytes()
        for name in ("via-proxy.json", "direct.json", "state/sources.json")
    }


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def setup():
    if WORK.exists():
        shutil.rmtree(WORK)
    FIXTURES.mkdir(parents=True)
    SOURCES.mkdir(parents=True)
    (FIXTURES / "tools").mkdir(parents=True)
    shutil.copy2(VALIDATE, FIXTURES / "tools" / "validate.py")

    config = json.loads((ROOT / "tools" / "sources.json").read_text(encoding="utf-8"))
    for source in config["sources"]:
        source["min_entries"] = 1
    write(FIXTURES / "tools" / "sources.json", json.dumps(config, ensure_ascii=False, indent=2))

    write(FIXTURES / "own" / "via-proxy.txt", "# свои\nshared.example\nown-only.example\n")
    write(FIXTURES / "own" / "direct.txt", "Example.ORG.\n192.0.2.0/24\ninside.example\n")

    write(SOURCES / "amnezia-ip-lite.txt", json.dumps([{"hostname": "198.51.100.0/24"}, {"hostname": "198.51.100.7"}]))
    write(SOURCES / "itdog-outside.txt", "shared.example\nsource-direct.example\n")
    write(SOURCES / "itdog-inside.txt", "inside.example\nthrough-proxy.example\n")
    write(
        SOURCES / "refilter-domains.txt",
        "".join(f"proxy-{index}.example\n" for index in range(20)),
    )


def main():
    setup()

    first = run_build()
    check(first.returncode == 0, f"склейка на образцах проходит (код {first.returncode})")
    if first.returncode != 0:
        print(first.stdout)
        print(first.stderr)
        return 1

    direct = document("direct.json")
    proxy = document("via-proxy.json")

    check(direct["version"] == 3 and proxy["version"] == 3, "оба файла — rule-set версии 3")
    check(
        direct["rules"][0] == {"domain_suffix": ["example.org", "inside.example"]},
        "свои добавки стоят первым правилом direct.json, домены приведены к нижнему регистру",
    )
    check(
        direct["rules"][1] == {"ip_cidr": ["192.0.2.0/24"]},
        "своя подсеть стоит вторым правилом direct.json",
    )
    check(
        proxy["rules"][0] == {"domain_suffix": ["shared.example", "own-only.example"]},
        "свои добавки стоят первым правилом via-proxy.json",
    )

    direct_text = (FIXTURES / "direct.json").read_text(encoding="utf-8")
    proxy_text = (FIXTURES / "via-proxy.json").read_text(encoding="utf-8")
    check("shared.example" not in direct_text, "своя запись перекрыла источник в другом списке (direct)")
    check("inside.example" not in proxy_text, "своя запись перекрыла источник в другом списке (via-proxy)")
    check("198.51.100.7/32" in direct_text, "одиночный адрес источника получил маску /32")
    check("source-direct.example" in direct_text, "запись источника попала в список")

    check(run_validate(FIXTURES).returncode == 0, "tools/validate.py принимает результат самопроверки")

    state = json.loads((FIXTURES / "state" / "sources.json").read_text(encoding="utf-8"))
    check(state["sources"]["refilter-domains"] == 20, "в состоянии записан объём источника (20)")

    before = snapshot()
    second = run_build()
    check(second.returncode == 0 and snapshot() == before, "повторный запуск даёт те же файлы")

    # Пустой источник: склейка обязана отказаться и не тронуть рабочие файлы.
    write(SOURCES / "refilter-domains.txt", "")
    empty = run_build()
    check(empty.returncode != 0, "пустой источник отменяет склейку")
    check("пусто" in empty.stderr, "отказ называет причину: источник пуст")
    check(snapshot() == before, "после пустого источника рабочие файлы прежние")

    # Объём на порядок меньше прежнего: тоже отказ.
    write(SOURCES / "refilter-domains.txt", "proxy-0.example\n")
    shrunk = run_build()
    check(shrunk.returncode != 0, "источник с объёмом 5% от прежнего отменяет склейку")
    check("меньше" in shrunk.stderr, "отказ называет причину: объём меньше прежнего")
    check(snapshot() == before, "после урезанного источника рабочие файлы прежние")

    # Негодная запись в своих добавках: отказ, а не тихий пропуск.
    write(SOURCES / "refilter-domains.txt", "".join(f"proxy-{index}.example\n" for index in range(20)))
    own_path = FIXTURES / "own" / "via-proxy.txt"
    good_own = own_path.read_text(encoding="utf-8")
    write(own_path, good_own + "пример.рф\n")
    broken = run_build()
    check(broken.returncode != 0, "негодная запись в своих добавках отменяет склейку")
    check("негодная запись" in broken.stderr, "отказ называет причину: негодная запись")
    check(snapshot() == before, "после негодной записи рабочие файлы прежние")

    # Одна запись в двух своих списках — противоречие, отказ.
    write(own_path, good_own + "shared.example\n")
    write(FIXTURES / "own" / "direct.txt", "Example.ORG.\n192.0.2.0/24\ninside.example\nshared.example\n")
    contradiction = run_build()
    check(contradiction.returncode != 0, "одна своя запись в двух списках отменяет склейку")
    check(snapshot() == before, "после противоречия рабочие файлы прежние")

    print(f"\nпроверок пройдено: {CHECKS['passed']}, провалено: {CHECKS['failed']}")
    return 0 if CHECKS["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
