"""
从 goji 关键词主库生成 pipeline run 目录。
追踪已跑过的国家，避免重复搜索。

用法：
    python pick_keywords.py --list                         # 查看各国状态
    python pick_keywords.py --countries US Germany         # 生成美国+德国的 run
    python pick_keywords.py --countries "United Kingdom"   # 支持全名
    python pick_keywords.py --countries US --force         # 强制重跑已完成的国家
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from pathlib import Path

from keyword_parser import parse_markdown
from serper_search import normalize_country

POOL_DIR = Path(__file__).parent / "keywords_pool"
MASTER_MD = POOL_DIR / "master.md"
TRACKING_CSV = POOL_DIR / "tracking.csv"
RUNS_DIR = Path(__file__).parent / "runs"

TRACKING_FIELDS = ["country_code", "country_display", "run_id", "picked_at"]


def load_done() -> dict[str, str]:
    """Return {country_code: run_id} for already-picked countries."""
    if not TRACKING_CSV.is_file():
        return {}
    done: dict[str, str] = {}
    with TRACKING_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            done[row["country_code"]] = row["run_id"]
    return done


def append_tracking(run_id: str, picks: list[tuple[str, str]]) -> None:
    write_header = not TRACKING_CSV.is_file() or TRACKING_CSV.stat().st_size == 0
    now = dt.datetime.now().isoformat(timespec="seconds")
    with TRACKING_CSV.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRACKING_FIELDS)
        if write_header:
            writer.writeheader()
        for code, display in picks:
            writer.writerow({"country_code": code, "country_display": display,
                             "run_id": run_id, "picked_at": now})


def write_run_md(path: Path, pool, display_countries: list[str]) -> None:
    lines = ["# COUNTRIES"]
    for c in display_countries:
        lines.append(f"- {c}")
    lines.append("")
    lines.append("# ANCHOR")
    for a in pool.anchors:
        lines.append(f"- {a}")
    for dim, mods in pool.modifiers_by_dim().items():
        if mods:
            lines.append(f"\n# MODIFIER-{dim}")
            for m in mods:
                lines.append(f"- {m}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_list() -> None:
    pool = parse_markdown(MASTER_MD)
    done = load_done()
    n_queries = len(pool.anchors) * pool.total_modifiers()
    print(f"{'国家':<22} {'代码':<6} {'状态':<8} 已用 run_id")
    print("-" * 65)
    for country in pool.countries:
        display, code = normalize_country(country)
        run_id = done.get(code, "")
        status = "done" if run_id else "unused"
        print(f"{display:<22} {code:<6} {status:<8} {run_id}")
    total = n_queries * len(pool.countries)
    print(f"\n每国 {n_queries} 条查询（{len(pool.anchors)} anchor × {pool.total_modifiers()} modifier），共 {len(pool.countries)} 国 = {total} 条")


def cmd_pick(countries_raw: list[str], force: bool) -> None:
    pool = parse_markdown(MASTER_MD)
    done = load_done()

    resolved: list[tuple[str, str]] = []  # (display, code)
    for raw in countries_raw:
        display, code = normalize_country(raw)
        resolved.append((display, code))

    conflicts = [(d, c) for d, c in resolved if c in done]
    if conflicts and not force:
        for display, code in conflicts:
            print(f"警告: {display}({code}) 已跑过 run={done[code]}，用 --force 强制重跑", file=sys.stderr)
        sys.exit(1)

    display_countries = [d for d, _ in resolved]
    date_str = dt.date.today().isoformat()
    slug = "_".join(c for _, c in resolved)
    run_id = f"{date_str}_goji_{slug}"
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    md_path = run_dir / "01_keywords.md"
    write_run_md(md_path, pool, display_countries)
    append_tracking(run_id, [(c, d) for d, c in resolved])

    n_queries = len(pool.anchors) * pool.total_modifiers() * len(resolved)
    print(f"✓ 已生成: {md_path}")
    print(f"  国家: {', '.join(display_countries)}  |  预计查询数: {n_queries}")
    print("\n运行命令（--max-queries 控制每次花费）：")
    print(f"  python pipeline.py {md_path}")
    print(f"  python pipeline.py {md_path} --max-queries 30")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="从关键词主库生成 pipeline run 目录。")
    p.add_argument("--countries", nargs="*", default=None, help="目标国家，支持代码(US)或全名(Germany)")
    p.add_argument("--force", action="store_true", help="强制重跑已完成的国家")
    p.add_argument("--list", action="store_true", help="查看各国完成状态")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.list:
        cmd_list()
    elif args.countries:
        cmd_pick(args.countries, force=args.force)
    else:
        parse_args().print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
