#!/usr/bin/env python3
"""Проверка формата списков. Падает с ненулевым кодом, если файл непригоден.

Проверка нужна потому, что файл читает программа, а правит человек. Ошибка в
JSON или незнакомое поле не ломают сборку клиента: он получает непригодный
набор и работает так, будто список пуст. Это самый тихий отказ из возможных —
список есть, он обновляется, и он не действует.
"""

import json
import re
import sys
from pathlib import Path

# Поля правил, которые понимает клиент. Незнакомое поле он игнорирует молча,
# поэтому список закрытый: опечатка в имени поля обязана быть отказом здесь, а
# не пустым набором на машине.
ALLOWED_FIELDS = {
    "domain",
    "domain_suffix",
    "domain_keyword",
    "domain_regex",
    "ip_cidr",
}

SUPPORTED_VERSIONS = {1, 2, 3}

DOMAIN_RE = re.compile(r"^[a-z0-9._-]+$")
CIDR_RE = re.compile(r"^[0-9a-fA-F:.]+/\d{1,3}$")

FILES = ("via-proxy.json", "direct.json")


def fail(path, message):
    print(f"{path}: {message}", file=sys.stderr)
    return 1


def check_entry(path, field, value, seen):
    errors = 0
    if not isinstance(value, str) or not value.strip():
        return fail(path, f"{field}: запись не строка или пуста: {value!r}")
    key = (field, value)
    if key in seen:
        errors += fail(path, f"{field}: повтор записи {value!r}")
    seen.add(key)

    if field == "ip_cidr":
        if not CIDR_RE.match(value):
            errors += fail(path, f"ip_cidr: нужна маска, получено {value!r}")
    elif field == "domain_regex":
        try:
            re.compile(value)
        except re.error as exc:
            errors += fail(path, f"domain_regex: выражение не разбирается ({exc}): {value!r}")
    else:
        if value != value.lower():
            errors += fail(path, f"{field}: домен не в нижнем регистре: {value!r}")
        if value.endswith("."):
            errors += fail(path, f"{field}: завершающая точка: {value!r}")
        if "://" in value or "/" in value:
            errors += fail(path, f"{field}: это адрес, а не домен: {value!r}")
        if not DOMAIN_RE.match(value):
            errors += fail(path, f"{field}: недопустимые знаки в домене: {value!r}")
    return errors


def check_file(path):
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return fail(path.name, f"JSON не разбирается: {exc}")

    errors = 0
    if not isinstance(document, dict):
        return fail(path.name, "корень обязан быть объектом")

    version = document.get("version")
    if version not in SUPPORTED_VERSIONS:
        errors += fail(path.name, f"version={version!r}: поддерживаются {sorted(SUPPORTED_VERSIONS)}")

    rules = document.get("rules")
    if not isinstance(rules, list):
        return errors + fail(path.name, "rules обязан быть списком")

    unknown_top = set(document) - {"version", "rules"}
    if unknown_top:
        errors += fail(path.name, f"незнакомые поля в корне: {sorted(unknown_top)}")

    seen = set()
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            errors += fail(path.name, f"rules[{index}]: правило обязано быть объектом")
            continue
        unknown = set(rule) - ALLOWED_FIELDS
        if unknown:
            errors += fail(path.name, f"rules[{index}]: незнакомые поля {sorted(unknown)}")
        for field, values in rule.items():
            if field not in ALLOWED_FIELDS:
                continue
            if not isinstance(values, list):
                errors += fail(path.name, f"rules[{index}].{field}: нужен список")
                continue
            for value in values:
                errors += check_entry(path.name, field, value, seen)
    return errors


def main():
    root = Path(__file__).resolve().parent.parent
    errors = 0
    for name in FILES:
        path = root / name
        if not path.exists():
            errors += fail(name, "файла нет")
            continue
        errors += check_file(path)
    if errors:
        print(f"непригодно: {errors} замечаний", file=sys.stderr)
        return 1
    print(f"пригодно: {len(FILES)} файла")
    return 0


if __name__ == "__main__":
    sys.exit(main())
