#!/usr/bin/env python3
"""Склейка списков из внешних источников и собственных добавок.

Берёт источники, перечисленные в `tools/sources.json`, приводит записи к виду
`rule-set` sing-box (version 3, исходный вид) и пишет `via-proxy.json` и
`direct.json`.

Собственные добавки из `own/<файл>.txt` всегда идут первыми блоками результата и
никогда не перетираются: если запись есть и у нас, и в источнике, остаётся наша.

Склейка отказывается работать, если источник отдал пусто или на порядок меньше
прежнего, либо если готовый результат не прошёл `tools/validate.py`. В этих
случаях рабочие файлы не меняются вовсе: сначала пишется черновик, проверяется,
и только потом заменяет рабочий список.

Запуск:
    python3 tools/build_lists.py
    python3 tools/build_lists.py --skip <id>       # пропустить недоступный источник
    python3 tools/build_lists.py --source-file <id>=<путь>   # взять источник из файла
"""

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = 3

# Источник считается «на порядок меньше» при падении объёма ниже этой доли от
# прошлого удачного запуска.
MIN_RATIO = 0.1
# Ниже этой доли объём подозрителен, но склейка продолжается: предупреждение.
WARN_RATIO = 0.5

FETCH_RETRIES = 3
FETCH_PAUSE = 5
FETCH_TIMEOUT = 60
USER_AGENT = "route-lists-builder/1"

TARGETS = ("via-proxy", "direct")

COMMENT_PREFIXES = ("#", "!", ";")

DOMAIN_RE = re.compile(r"^[a-z0-9._-]+$")
SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


class BuildError(Exception):
    """Отказ склейки: рабочие файлы остаются прежними."""


def comment(line):
    stripped = line.strip()
    return not stripped or stripped.startswith(COMMENT_PREFIXES)


def strip_port(value):
    """Убирает порт у записи вида `example.com:443`, не трогая адреса IPv6."""
    if value.count(":") == 1:
        host, _, port = value.partition(":")
        if port.isdigit():
            return host
    return value


def normalize(raw):
    """Приводит запись к паре (поле правила, значение) или к None.

    Домены — в нижний регистр, без схемы, без пути, без ведущих `*` и точек и без
    завершающей точки. Адреса и подсети — в `ip_cidr` с маской (`/32` и `/128`
    для одиночных адресов).
    """
    value = raw.strip()
    if not value or value.startswith(COMMENT_PREFIXES):
        return None
    value = value.split()[0]
    value = value.strip("\"'")

    if SCHEME_RE.match(value):
        value = SCHEME_RE.sub("", value)
    value = strip_port(value)

    # Адрес или подсеть разбирается модулем ipaddress; всё остальное — домен.
    candidate = value.rstrip(".")
    try:
        network = ipaddress.ip_network(candidate, strict=False)
    except ValueError:
        network = None
    if network is not None:
        return "ip_cidr", str(network)

    if "/" in value:
        value = value.split("/", 1)[0]
    while value.startswith(("*", ".")):
        value = value[1:]
    value = value.rstrip(".").lower()

    if not value:
        return None
    if not DOMAIN_RE.match(value):
        return None
    return "domain_suffix", value


def parse_domain_lines(text):
    """Источник вида «строка — запись»."""
    return [line for line in text.splitlines()]


def parse_json_hostnames(text):
    """Источник вида JSON-списка объектов с полем `hostname` (или `ip`)."""
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BuildError(f"источник не разбирается как JSON: {exc}")
    if isinstance(document, dict):
        items = []
        for key in ("hostnames", "domains", "ips", "items"):
            if isinstance(document.get(key), list):
                items.extend(document[key])
    elif isinstance(document, list):
        items = document
    else:
        raise BuildError("источник JSON: ожидался список или объект со списком")

    lines = []
    for item in items:
        if isinstance(item, str):
            lines.append(item)
        elif isinstance(item, dict):
            for key in ("hostname", "domain", "ip", "cidr"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    lines.append(value)
                    break
    return lines


PARSERS = {
    "domain-lines": parse_domain_lines,
    "json-hostnames": parse_json_hostnames,
}


def fetch(url):
    last_error = None
    for attempt in range(1, FETCH_RETRIES + 1):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            last_error = f"код ответа {exc.code}"
            if exc.code < 500:
                break
        except (urllib.error.URLError, OSError) as exc:
            last_error = str(exc)
        if attempt < FETCH_RETRIES:
            print(f"  повтор {attempt}/{FETCH_RETRIES - 1} через {FETCH_PAUSE} с: {last_error}", file=sys.stderr)
            time.sleep(FETCH_PAUSE)
    raise BuildError(f"{url}: не удалось получить ({last_error})")


def load_json(path, what):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BuildError(f"{what}: файла нет ({path})")
    except json.JSONDecodeError as exc:
        raise BuildError(f"{what}: JSON не разбирается ({path}: {exc})")


def read_own(path):
    """Собственные добавки: файл `<поле> — запись`. Негодная запись — отказ."""
    if not path.exists():
        return []
    entries = []
    problems = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if comment(line):
            continue
        parsed = normalize(line)
        if parsed is None:
            problems.append(f"{path.name}:{number}: негодная запись {line.strip()!r}")
            continue
        entries.append(parsed)
    if problems:
        raise BuildError("свои добавки:\n  " + "\n  ".join(problems))
    return entries


def check_volume(source, count, previous):
    """Отказ, если источник отдал пусто или на порядок меньше прежнего."""
    if count == 0:
        raise BuildError(
            f"{source['id']}: источник отдал пусто — рабочие списки не меняются"
        )
    minimum = int(source.get("min_entries", 1))
    if count < minimum:
        raise BuildError(
            f"{source['id']}: записей {count}, это меньше нижнего предела {minimum} — "
            "рабочие списки не меняются"
        )
    if previous and count < previous * MIN_RATIO:
        raise BuildError(
            f"{source['id']}: записей {count}, а в прошлый раз было {previous} "
            f"(меньше {int(MIN_RATIO * 100)}%) — рабочие списки не меняются"
        )
    if previous and count < previous * WARN_RATIO:
        print(
            f"  внимание: {source['id']}: записей {count} против {previous} в прошлый раз",
            file=sys.stderr,
        )


def merge(blocks, seen, blocked):
    """Раскладывает блоки записей по полям, отбрасывая повторы (первый победил).

    `blocked` — свои записи другого списка: они главнее источников, поэтому из
    этого списка убираются.
    """
    rules = []
    stats = []
    for label, entries in blocks:
        domains = []
        cidrs = []
        duplicates = 0
        overridden = 0
        for field, value in entries:
            key = (field, value)
            if key in blocked:
                overridden += 1
                continue
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            if field == "domain_suffix":
                domains.append(value)
            else:
                cidrs.append(value)
        rules.append((label, domains, cidrs))
        stats.append(
            {
                "label": label,
                "domains": len(domains),
                "cidrs": len(cidrs),
                "duplicates": duplicates,
                "overridden": overridden,
            }
        )
    return rules, stats


def render(rules):
    document = {"version": VERSION, "rules": []}
    for _label, domains, cidrs in rules:
        if domains:
            document["rules"].append({"domain_suffix": domains})
        if cidrs:
            document["rules"].append({"ip_cidr": cidrs})
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def write_candidate(directory, name, text):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def validate(directory, script):
    result = subprocess.run(
        [sys.executable, "-I", str(script), str(directory)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip() or "без сообщения"
        raise BuildError(f"tools/validate.py не принял результат:\n{message}")
    return result.stdout.strip()


def build_source(source, overrides):
    if source["id"] in overrides:
        text = Path(overrides[source["id"]]).read_text(encoding="utf-8", errors="replace")
    else:
        text = fetch(source["url"])
    parser = PARSERS.get(source.get("kind", "domain-lines"))
    if parser is None:
        raise BuildError(f"{source['id']}: незнакомый вид источника {source.get('kind')!r}")
    lines = parser(text)
    entries = []
    skipped = 0
    for line in lines:
        parsed = normalize(line)
        if parsed is None:
            if line.strip() and not comment(line):
                skipped += 1
            continue
        entries.append(parsed)
    return entries, skipped


def parse_overrides(values):
    overrides = {}
    for item in values or []:
        key, separator, path = item.partition("=")
        if not separator or not key or not path:
            raise BuildError(f"--source-file: ожидалось <id>=<путь>, получено {item!r}")
        overrides[key] = path
    return overrides


def main(argv=None):
    root_default = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Склейка списков из источников")
    parser.add_argument("--root", default=str(root_default), help="корень репозитория списков")
    parser.add_argument("--config", default=None, help="файл описания источников")
    parser.add_argument("--state", default=None, help="файл состояния с прошлыми объёмами")
    parser.add_argument("--own-dir", default=None, help="каталог собственных добавок")
    parser.add_argument("--work-dir", default=None, help="каталог для черновиков")
    parser.add_argument("--validate-script", default=None, help="проверка формата")
    parser.add_argument("--source-file", action="append", default=[], help="<id>=<путь> вместо сети")
    parser.add_argument("--skip", action="append", default=[], help="пропустить источник по id")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    config_path = Path(args.config) if args.config else root / "tools" / "sources.json"
    state_path = Path(args.state) if args.state else root / "state" / "sources.json"
    own_dir = Path(args.own_dir) if args.own_dir else root / "own"
    work_dir = Path(args.work_dir) if args.work_dir else root / ".git" / "cache" / "build"
    validate_script = Path(args.validate_script) if args.validate_script else root / "tools" / "validate.py"

    try:
        overrides = parse_overrides(args.source_file)
        config = load_json(config_path, "описание источников")
        sources = config.get("sources")
        if not isinstance(sources, list) or not sources:
            raise BuildError("описание источников: пустой список sources")
        for source in sources:
            for key in ("id", "url", "target", "kind"):
                if not source.get(key):
                    raise BuildError(f"описание источников: у записи нет поля {key}: {source!r}")
            if source["target"] not in TARGETS:
                raise BuildError(
                    f"{source['id']}: target={source['target']!r}, допустимо {list(TARGETS)}"
                )
        ids = [source["id"] for source in sources]
        if len(ids) != len(set(ids)):
            raise BuildError("описание источников: повтор id")
        unknown = set(overrides) - set(ids)
        if unknown:
            raise BuildError(f"--source-file: неизвестные id {sorted(unknown)}")
        unknown = set(args.skip) - set(ids)
        if unknown:
            raise BuildError(f"--skip: неизвестные id {sorted(unknown)}")

        state = {}
        if state_path.exists():
            state = load_json(state_path, "состояние")
        previous_sources = state.get("sources", {})
        previous_outputs = state.get("outputs", {})

        own = {}
        for target in TARGETS:
            own[target] = read_own(own_dir / f"{target}.txt")

        own_keys = {target: {(field, value) for field, value in own[target]} for target in TARGETS}
        both = own_keys["via-proxy"] & own_keys["direct"]
        if both:
            names = ", ".join(sorted(value for _field, value in both))
            raise BuildError(
                f"свои добавки: одна и та же запись в двух списках ({names}) — "
                "оставьте её только в одном"
            )

        collected = {target: [] for target in TARGETS}
        # Пропущенный источник сохраняет прошлый объём: иначе следующий запуск
        # останется без точки сравнения.
        parsed_counts = {key: value for key, value in previous_sources.items() if key in args.skip}
        skipped_sources = set()
        for source in sources:
            skipped = source["id"] in args.skip
            if skipped:
                skipped_sources.add(source["id"])
                print(f"источник {source['id']}: пропущен по --skip", file=sys.stderr)
                continue
            print(f"источник {source['id']}: {source['url']}")
            entries, unparsed = build_source(source, overrides)
            count = len(entries)
            check_volume(source, count, previous_sources.get(source["id"]))
            parsed_counts[source["id"]] = count
            collected[source["target"]].append((source["id"], entries))
            extra = f", негодных строк {unparsed}" if unparsed else ""
            print(f"  записей {count}{extra}")

        outputs = {}
        summary = {}
        present = {}
        for target in TARGETS:
            blocks = [("свои", own[target])] + collected[target]
            other = "direct" if target == "via-proxy" else "via-proxy"
            seen = set()
            rules, stats = merge(blocks, seen, own_keys[other])
            total = sum(item["domains"] + item["cidrs"] for item in stats)
            previous = previous_outputs.get(f"{target}.json")
            if previous and not skipped_sources and total < previous * MIN_RATIO:
                raise BuildError(
                    f"{target}.json: записей {total}, а было {previous} "
                    f"(меньше {int(MIN_RATIO * 100)}%) — рабочие списки не меняются"
                )
            outputs[f"{target}.json"] = render(rules)
            summary[target] = {"stats": stats, "total": total}
            present[target] = seen
            print(f"\n{target}.json: {total} записей")

        overlap = present["direct"] & present["via-proxy"]
        if overlap:
            names = ", ".join(sorted(value for _field, value in overlap)[:10])
            print(
                f"  внимание: в обоих списках названо записей: {len(overlap)} ({names}): "
                "какой список главнее, решает порядок правил в клиенте",
                file=sys.stderr,
            )

        candidate = work_dir / "next"
        if candidate.exists():
            shutil.rmtree(candidate)
        candidate.mkdir(parents=True)
        for name, text in outputs.items():
            write_candidate(candidate, name, text)
        verdict = validate(candidate, validate_script)

        for name in outputs:
            os.replace(candidate / name, root / name)

        state_path.parent.mkdir(parents=True, exist_ok=True)
        new_state = {
            "sources": parsed_counts,
            "outputs": {name: summary[name[:-5]]["total"] for name in outputs},
        }
        state_path.write_text(json.dumps(new_state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        print(f"\n{verdict}")
        for target in TARGETS:
            parts = []
            for item in summary[target]["stats"]:
                parts.append(f"{item['label']} {item['domains'] + item['cidrs']}")
            duplicates = sum(item["duplicates"] for item in summary[target]["stats"])
            overridden = sum(item["overridden"] for item in summary[target]["stats"])
            tail = f"повторов отброшено {duplicates}"
            if overridden:
                tail += f", перекрыто своими {overridden}"
            print(f"{target}.json: {summary[target]['total']} записей ({', '.join(parts)}; {tail})")
        return 0
    except BuildError as exc:
        print(f"отказ: {exc}", file=sys.stderr)
        print("рабочие списки не изменены", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
