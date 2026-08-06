"""CLI управления агентом: поиск, одобрение, сканирование, изменения, цены.

Примеры:
    python cli.py add https://aetherix.com.ua/ --own --name Aetherix
    python cli.py discover
    python cli.py list --status candidate
    python cli.py approve 3
    python cli.py scan --all
    python cli.py changes --days 7
    python cli.py pricing
"""

import argparse
import datetime as dt
import json

from sqlalchemy import select

from db.models import ChangeLog, Competitor, ServiceOffering, utcnow
from db.session import SessionLocal
from services import discovery, pricing, scan


def cmd_add(args) -> None:
    with SessionLocal() as session:
        domain = discovery.normalize_domain(args.url)
        if not domain:
            print("Не разобрал домен — укажи полный URL со схемой (https://...)")
            return
        existing = session.scalar(select(Competitor).where(Competitor.domain == domain))
        if existing:
            print(f"Уже есть: #{existing.id} {existing.name} ({domain}), статус {existing.status}")
            return
        competitor = Competitor(
            name=args.name or domain,
            domain=domain,
            url=args.url,
            status="active",
            is_own=args.own,
            approved_at=utcnow(),
        )
        session.add(competitor)
        session.commit()
        label = "наш сайт" if args.own else "конкурент"
        print(f"Добавлен {label}: #{competitor.id} {competitor.name} ({domain})")


def cmd_discover(args) -> None:
    with SessionLocal() as session:
        try:
            result = discovery.run_discovery(session, source=args.source)
        except discovery.DiscoveryLimitError as exc:
            print(f"⛔ {exc}")
            return

        print(f"Запросы: {', '.join(result['queries'])}")
        print(f"Найдено результатов: {result['found']}, новых кандидатов: {len(result['added'])}")
        for item in result["added"]:
            print(f"  #{item['id']}  {item['domain']}  — {item['name'][:70]}")
        if result["skipped"]:
            print(f"Пропущены каталоги: {', '.join(result['skipped'])}")
        if result["runs_today"] is not None:
            print(f"Ручных запусков сегодня: {result['runs_today']}")


def cmd_list(args) -> None:
    with SessionLocal() as session:
        query = select(Competitor).order_by(Competitor.id)
        if args.status:
            query = query.where(Competitor.status == args.status)
        rows = session.scalars(query).all()
        if not rows:
            print("Пусто.")
            return
        for c in rows:
            services = session.scalar(
                select(ServiceOffering.id).where(ServiceOffering.competitor_id == c.id).limit(1)
            )
            mark = " ★наш" if c.is_own else ""
            scanned = "есть данные" if services else "не сканирован"
            print(f"#{c.id:<4} {c.status:<10}{mark:<5} {c.domain:<32} {scanned}  {c.name[:50]}")


def _set_status(competitor_id: int, status: str) -> None:
    with SessionLocal() as session:
        competitor = session.get(Competitor, competitor_id)
        if competitor is None:
            print(f"Нет конкурента #{competitor_id}")
            return
        competitor.status = status
        if status == "active":
            competitor.approved_at = utcnow()
        session.commit()
        print(f"#{competitor.id} {competitor.domain} → {status}")


def cmd_approve(args) -> None:
    _set_status(args.id, "active")


def cmd_reject(args) -> None:
    _set_status(args.id, "rejected")


def _print_report(report: dict) -> None:
    if "error" in report:
        print(f"✖ {report['domain']}: {report['error']}")
        return
    print(f"✔ {report['domain']}: страниц {len(report['pages'])}, услуг {report['services']}, акций {report['promotions']}")
    for page in report["pages"]:
        print(f"    ← {page}")
    for change in report["changes"]:
        print(f"    ! {change}")


def cmd_scan(args) -> None:
    with SessionLocal() as session:
        if args.all:
            for report in scan.scan_all(session, include_own=not args.skip_own, max_pages=args.pages):
                _print_report(report)
            return

        competitor = session.get(Competitor, args.id)
        if competitor is None:
            print(f"Нет конкурента #{args.id}")
            return
        _print_report(scan.scan_competitor(session, competitor, max_pages=args.pages))


def cmd_changes(args) -> None:
    since = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=args.days)
    with SessionLocal() as session:
        rows = session.execute(
            select(ChangeLog, Competitor)
            .join(Competitor, ChangeLog.competitor_id == Competitor.id)
            .where(ChangeLog.captured_at >= since)
            .order_by(ChangeLog.captured_at.desc())
        ).all()
        if not rows:
            print(f"За {args.days} дн. изменений не зафиксировано.")
            return
        for change, competitor in rows:
            stamp = change.captured_at.strftime("%d.%m %H:%M")
            print(f"{stamp}  {competitor.domain:<28} [{change.change_type}] {change.description}")


def cmd_prices(args) -> None:
    with SessionLocal() as session:
        data = pricing.summary(session)
        if not data["prices"]:
            print("Цен пока не собрано.")
            return
        for item in sorted(data["prices"], key=lambda i: (i["competitor"], i["low"])):
            print(f"{item['domain']:<28} {item['service'][:40]:<42} {item['price_text']}")
        print("\nСтатистика рынка:")
        for currency, s in data["stats"].items():
            print(
                f"  {currency}: позиций {s['count']}, мин {s['min']:.0f}, "
                f"медиана {s['median_low']:.0f}–{s['median_high']:.0f}, макс {s['max']:.0f}"
            )


def cmd_pricing(args) -> None:
    with SessionLocal() as session:
        result = pricing.recommend(session)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if not result.get("recommendations"):
            print(result.get("notes", "Нет рекомендаций."))
            return
        print("Предложение по нашему прайсу:\n")
        for rec in result["recommendations"]:
            print(f"  {rec.get('service')}: {rec.get('price')}  [{rec.get('market_position')}]")
            print(f"      {rec.get('rationale')}")
        if result.get("notes"):
            print(f"\nВывод: {result['notes']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Агент мониторинга конкурентов")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="добавить сайт вручную")
    p_add.add_argument("url")
    p_add.add_argument("--name", default=None)
    p_add.add_argument("--own", action="store_true", help="это наш сайт")
    p_add.set_defaults(func=cmd_add)

    p_disc = sub.add_parser("discover", help="найти новых конкурентов")
    p_disc.add_argument("--source", choices=["manual", "scheduled"], default="manual")
    p_disc.set_defaults(func=cmd_discover)

    p_list = sub.add_parser("list", help="список конкурентов")
    p_list.add_argument("--status", choices=["candidate", "active", "rejected"], default=None)
    p_list.set_defaults(func=cmd_list)

    p_ok = sub.add_parser("approve", help="одобрить кандидата")
    p_ok.add_argument("id", type=int)
    p_ok.set_defaults(func=cmd_approve)

    p_no = sub.add_parser("reject", help="отклонить кандидата")
    p_no.add_argument("id", type=int)
    p_no.set_defaults(func=cmd_reject)

    p_scan = sub.add_parser("scan", help="просканировать конкурента")
    p_scan.add_argument("id", type=int, nargs="?")
    p_scan.add_argument("--all", action="store_true", help="все активные")
    p_scan.add_argument("--skip-own", action="store_true", help="не сканировать наш сайт")
    p_scan.add_argument("--pages", type=int, default=3, help="сколько страниц брать с сайта")
    p_scan.set_defaults(func=cmd_scan)

    p_ch = sub.add_parser("changes", help="журнал изменений")
    p_ch.add_argument("--days", type=int, default=7)
    p_ch.set_defaults(func=cmd_changes)

    p_pr = sub.add_parser("prices", help="собранные цены конкурентов")
    p_pr.set_defaults(func=cmd_prices)

    p_rec = sub.add_parser("pricing", help="предложить наш прайс на основе рынка")
    p_rec.add_argument("--json", action="store_true")
    p_rec.set_defaults(func=cmd_pricing)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "scan" and not args.all and args.id is None:
        print("Укажи id конкурента или --all")
        return
    args.func(args)


if __name__ == "__main__":
    main()
