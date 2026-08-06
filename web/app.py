"""Панель мониторинга конкурентов.

Страницы рисует сервер: панель смотрит один человек несколько раз в неделю, и
ради этого держать отдельный фронтенд незачем. Оформление перенесено из CRM
sales-bot без изменений — те же классы и тот же CSS (web/static/app.css).

Долгие действия — поиск, проверка сайтов, расчёт прайса — уходят в фон:
проверка четырёх сайтов занимает минуты, столько браузер ждать не станет.
Страница сразу говорит, что работа началась, а результат появляется после
обновления.
"""

import datetime as dt
import json
import logging
import threading
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from db.models import (
    ChangeLog,
    Competitor,
    PageSnapshot,
    PriceEntry,
    Promotion,
    Review,
    ServiceOffering,
    utcnow,
)
from db.session import SessionLocal
from services import discovery, pricing, scan

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
router = APIRouter()

# Результат последнего расчёта прайса. Считает его модель, занимает это десятки
# секунд, и пересчитывать при каждом открытии страницы незачем.
PRICING_FILE = WEB_DIR / "last_pricing.json"

STATUS_LABELS = {
    "candidate": "Ждёт решения",
    "active": "Под наблюдением",
    "rejected": "Отклонён",
}

CHANGE_LABELS = {
    "new_service": "Новая услуга",
    "price_change": "Цена",
    "new_promotion": "Акция",
    "promotion_ended": "Акция снята",
    "rating_change": "Рейтинг",
    "first_scan": "Первый сбор",
}

POSITION_LABELS = {"below": "ниже рынка", "at": "по рынку", "above": "выше рынка"}
POSITION_TAGS = {"below": "st-active", "at": "st-own", "above": "st-candidate"}

PERIOD_LABELS = {
    "month": "в месяц",
    "hour": "за час",
    "project": "за проект",
    "one_time": "разово",
}

FLASHES = {
    "discover_started": ("ok", "Ищем конкурентов. Обновите страницу через минуту."),
    "scan_started": ("ok", "Проверяем сайты. Это занимает несколько минут."),
    "pricing_started": ("ok", "Считаем предложение по нашему прайсу. Обновите страницу через минуту."),
    "busy": ("warn", "Предыдущая проверка ещё идёт — дождитесь её окончания."),
    "approved": ("ok", "Конкурент взят под наблюдение."),
    "rejected": ("ok", "Убрали из списка."),
    "added": ("ok", "Сайт добавлен и взят под наблюдение."),
    "exists": ("warn", "Такой сайт уже есть в списке."),
    "bad_url": ("err", "Не разобрал адрес — нужен полный URL со схемой https://."),
    "limit": ("warn", "Дневной лимит поисковых запросов исчерпан."),
}

# Одновременно идёт не больше одной длинной работы: два скана разом лезут на те
# же сайты и жгут лимиты API впустую.
_busy = threading.Lock()


def _fmt(moment: dt.datetime | None) -> str:
    if moment is None:
        return ""
    return moment.strftime("%d.%m %H:%M")


def _visible_prices(session, competitor_id: int) -> list[PriceEntry]:
    """Актуальные цены конкурента, кроме тех, что с чужих нам рынков."""
    entries = session.scalars(
        select(PriceEntry)
        .join(ServiceOffering, ServiceOffering.id == PriceEntry.service_offering_id)
        .where(ServiceOffering.competitor_id == competitor_id)
        .order_by(PriceEntry.captured_at.desc())
    ).all()
    return [entry for entry in entries if not pricing.is_ignored(entry.price_text)]


def _cheapest_price(session, competitor_id: int) -> dict | None:
    """С чего начинается прайс конкурента — самая низкая из показываемых цен."""
    parsed = []
    for entry in _visible_prices(session, competitor_id):
        price = pricing.parse_price(entry.price_text)
        if price is None:
            continue
        usd = pricing.to_usd(price["low"], price["currency"])
        # Сравниваем в долларах, показываем как на сайте: дешевизна должна
        # определяться суммой, а не тем, в какой валюте её написали.
        parsed.append((usd if usd is not None else price["low"], price))
    if not parsed:
        return None
    price = min(parsed, key=lambda pair: pair[0])[1]
    return {"text": pricing.pretty(price["text"]), "usd": pricing.usd_label(price)}


def _nav(active: str) -> list[dict]:
    """Разделы панели. Значки — те же контурные, что в CRM."""
    items = [
        ("summary", "/", "Сводка", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/></svg>'),
        ("competitors", "/competitors", "Конкуренты", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="8" r="3.2"/><path d="M3.5 19.5c0-3 2.5-5 5.5-5s5.5 2 5.5 5"/><path d="M16 6.5a3 3 0 0 1 0 5.6"/><path d="M17.5 19.5c0-2.3-1-4-2.6-4.9"/></svg>'),
        ("changes", "/changes", "Изменения", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h4l3 7 4-14 3 7h4"/></svg>'),
        ("prices", "/prices", "Цены", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v18"/><path d="M16.5 7.5c0-1.7-2-3-4.5-3s-4.5 1.1-4.5 3 2 2.6 4.5 3 4.5 1.3 4.5 3-2 3-4.5 3-4.5-1.3-4.5-3"/></svg>'),
    ]
    return [
        {"key": key, "url": url, "title": title, "icon": icon, "active": key == active}
        for key, url, title, icon in items
    ]


def _page(request: Request, template: str, section: str, **context) -> HTMLResponse:
    flash_key = request.query_params.get("flash")
    flashes = [FLASHES[flash_key]] if flash_key in FLASHES else []
    return templates.TemplateResponse(
        request,
        template,
        {"nav": _nav(section), "flashes": flashes, **context},
    )


def _back(url: str, flash: str | None = None) -> RedirectResponse:
    # 303: после POST браузер должен перейти на страницу обычным GET, иначе
    # обновление страницы повторит действие.
    return RedirectResponse(f"{url}?flash={flash}" if flash else url, status_code=303)


def _run_in_background(work, *args) -> bool:
    """Запускает длинную работу, если другая такая не идёт. Вернёт False, если занято."""
    if not _busy.acquire(blocking=False):
        return False
    try:
        work(*args)
    except Exception:  # сайт лежит, лимит API, сеть — панель об этом не падает
        log.exception("фоновая работа не удалась")
    finally:
        _busy.release()
    return True


# ─── Страницы ───


@router.get("/", response_class=HTMLResponse)
def summary(request: Request):
    with SessionLocal() as session:
        week_ago = utcnow().replace(tzinfo=None) - dt.timedelta(days=7)
        metrics = {
            "active": session.scalar(
                select(func.count(Competitor.id)).where(
                    Competitor.status == "active", Competitor.is_own.is_(False)
                )
            ),
            "candidates": session.scalar(
                select(func.count(Competitor.id)).where(Competitor.status == "candidate")
            ),
            "changes_week": session.scalar(
                select(func.count(ChangeLog.id)).where(ChangeLog.captured_at >= week_ago)
            ),
            "prices": session.scalar(select(func.count(PriceEntry.id))),
        }

        changes = [
            {
                "competitor_id": competitor.id,
                "domain": competitor.domain,
                "description": change.description,
                "change_type": change.change_type,
                "type_label": CHANGE_LABELS.get(change.change_type, change.change_type),
            }
            for change, competitor in session.execute(
                select(ChangeLog, Competitor)
                .join(Competitor, ChangeLog.competitor_id == Competitor.id)
                .order_by(ChangeLog.captured_at.desc())
                .limit(8)
            ).all()
        ]

        candidates = [
            {"id": c.id, "domain": c.domain, "name": c.name, "url": c.url}
            for c in session.scalars(
                select(Competitor)
                .where(Competitor.status == "candidate")
                .order_by(Competitor.discovered_at.desc())
                .limit(6)
            ).all()
        ]

        last = session.scalar(select(func.max(PageSnapshot.captured_at)))

    return _page(
        request,
        "summary.html",
        "summary",
        metrics=metrics,
        changes=changes,
        candidates=candidates,
        last_scan=_fmt(last),
    )


@router.get("/competitors", response_class=HTMLResponse)
def competitors(request: Request, status: str | None = None):
    with SessionLocal() as session:
        query = select(Competitor).order_by(Competitor.is_own.desc(), Competitor.id)
        if status in STATUS_LABELS:
            query = query.where(Competitor.status == status)

        rows = []
        for competitor in session.scalars(query).all():
            services = session.scalar(
                select(func.count(ServiceOffering.id)).where(
                    ServiceOffering.competitor_id == competitor.id
                )
            )
            last = session.scalar(
                select(func.max(PageSnapshot.captured_at)).where(
                    PageSnapshot.competitor_id == competitor.id
                )
            )
            # В колонке цен полезно не «сколько их всего», а с чего начинается
            # прайс: по этому числу конкуренты и сравниваются взглядом.
            cheapest = _cheapest_price(session, competitor.id)
            rows.append(
                {
                    "id": competitor.id,
                    "domain": competitor.domain,
                    "name": competitor.name,
                    "status": competitor.status,
                    "status_label": STATUS_LABELS.get(competitor.status, competitor.status),
                    "is_own": competitor.is_own,
                    "services": services,
                    "price_from": cheapest,
                    "last_scan": _fmt(last),
                }
            )

        total = session.scalar(select(func.count(Competitor.id)))

    return _page(
        request,
        "competitors.html",
        "competitors",
        rows=rows,
        total=total,
        status=status if status in STATUS_LABELS else None,
    )


@router.get("/competitors/{competitor_id}", response_class=HTMLResponse)
def competitor_card(request: Request, competitor_id: int):
    with SessionLocal() as session:
        competitor = session.get(Competitor, competitor_id)
        if competitor is None:
            return _back("/competitors")

        services = []
        for offering in session.scalars(
            select(ServiceOffering)
            .where(ServiceOffering.competitor_id == competitor_id)
            .order_by(ServiceOffering.first_seen_at)
        ).all():
            history = [
                entry
                for entry in session.scalars(
                    select(PriceEntry)
                    .where(PriceEntry.service_offering_id == offering.id)
                    .order_by(PriceEntry.captured_at.desc())
                ).all()
                if not pricing.is_ignored(entry.price_text)
            ]
            current = pricing.parse_price(history[0].price_text) if history else None
            services.append(
                {
                    "name": offering.name,
                    "description": offering.description,
                    "price": pricing.pretty(history[0].price_text) if history else None,
                    "price_usd": pricing.usd_label(current) if current else None,
                    # Прошлая цена показывается зачёркнутой рядом с новой — так
                    # видно направление движения, а не только текущее число.
                    "old_price": pricing.pretty(history[1].price_text) if len(history) > 1 else None,
                    "first_seen": _fmt(offering.first_seen_at),
                }
            )

        # Услуги с ценой — наверх: ради них таблицу и открывают. Внутри групп
        # порядок прежний, по времени появления.
        services.sort(key=lambda s: s["price"] is None)
        priced = sum(1 for s in services if s["price"])

        promotions = [
            {"description": promo.description, "captured_at": _fmt(promo.captured_at)}
            for promo in session.scalars(
                select(Promotion)
                .where(Promotion.competitor_id == competitor_id, Promotion.active.is_(True))
                .order_by(Promotion.captured_at.desc())
            ).all()
        ]

        review = session.scalars(
            select(Review)
            .where(Review.competitor_id == competitor_id)
            .order_by(Review.captured_at.desc())
            .limit(1)
        ).first()

        changes = [
            {
                "change_type": change.change_type,
                "type_label": CHANGE_LABELS.get(change.change_type, change.change_type),
                "description": change.description,
                "captured_at": _fmt(change.captured_at),
            }
            for change in session.scalars(
                select(ChangeLog)
                .where(ChangeLog.competitor_id == competitor_id)
                .order_by(ChangeLog.captured_at.desc())
                .limit(15)
            ).all()
        ]

        last = session.scalar(
            select(func.max(PageSnapshot.captured_at)).where(
                PageSnapshot.competitor_id == competitor_id
            )
        )
        pages = list(
            session.scalars(
                select(PageSnapshot.url)
                .where(
                    PageSnapshot.competitor_id == competitor_id,
                    PageSnapshot.captured_at == last,
                )
                .distinct()
            ).all()
        )

        positioning = None
        snapshot = session.scalars(
            select(PageSnapshot)
            .where(
                PageSnapshot.competitor_id == competitor_id,
                PageSnapshot.extracted_json.is_not(None),
            )
            .order_by(PageSnapshot.captured_at.desc())
            .limit(1)
        ).first()
        if snapshot is not None:
            try:
                positioning = json.loads(snapshot.extracted_json).get("positioning")
            except (json.JSONDecodeError, AttributeError):
                positioning = None

        card = {
            "id": competitor.id,
            "name": competitor.name,
            "domain": competitor.domain,
            "url": competitor.url,
            "status": competitor.status,
            "is_own": competitor.is_own,
        }

    return _page(
        request,
        "competitor.html",
        "competitors",
        competitor=card,
        status_label=STATUS_LABELS.get(card["status"], card["status"]),
        services=services,
        priced=priced,
        promotions=promotions,
        review=review,
        changes=changes,
        pages=pages,
        positioning=positioning,
        last_scan=_fmt(last),
    )


@router.get("/changes", response_class=HTMLResponse)
def changes(request: Request, days: int = 7):
    since = utcnow().replace(tzinfo=None) - dt.timedelta(days=days)
    with SessionLocal() as session:
        rows = [
            {
                "captured_at": _fmt(change.captured_at),
                "competitor_id": competitor.id,
                "domain": competitor.domain,
                "change_type": change.change_type,
                "type_label": CHANGE_LABELS.get(change.change_type, change.change_type),
                "description": change.description,
            }
            for change, competitor in session.execute(
                select(ChangeLog, Competitor)
                .join(Competitor, ChangeLog.competitor_id == Competitor.id)
                .where(ChangeLog.captured_at >= since)
                .order_by(ChangeLog.captured_at.desc())
            ).all()
        ]

    return _page(request, "changes.html", "changes", rows=rows, days=days)


@router.get("/prices", response_class=HTMLResponse)
def prices(request: Request):
    with SessionLocal() as session:
        data = pricing.summary(session)
        domains = {
            c.domain: c.id for c in session.scalars(select(Competitor)).all()
        }

    rows = [
        {
            "competitor_id": domains.get(item["domain"]),
            "domain": item["domain"],
            "service": item["service"],
            "price_text": pricing.pretty(item["price_text"]),
            "price_usd": item["usd_label"],
            "period_label": PERIOD_LABELS.get(item["period"], ""),
        }
        # Сортируем по долларовому эквиваленту: так рядом стоят сопоставимые
        # предложения, а не сначала все гривневые, потом все долларовые.
        for item in sorted(
            data["prices"], key=lambda i: (i["usd_low"] is None, i["usd_low"] or i["low"])
        )
    ]

    offer = None
    if PRICING_FILE.exists():
        try:
            offer = json.loads(PRICING_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            offer = None

    return _page(
        request,
        "prices.html",
        "prices",
        prices=rows,
        stats=data["stats"],
        offer=offer,
        position_labels=POSITION_LABELS,
        position_tags=POSITION_TAGS,
    )


# ─── Действия ───


@router.post("/competitors/add")
def add_competitor(url: str = Form(...), name: str = Form("")):
    domain = discovery.normalize_domain(url)
    if not domain:
        return _back("/competitors", "bad_url")

    with SessionLocal() as session:
        if session.scalar(select(Competitor).where(Competitor.domain == domain)):
            return _back("/competitors", "exists")
        session.add(
            Competitor(
                name=name.strip() or domain,
                domain=domain,
                url=url,
                status="active",
                approved_at=utcnow(),
            )
        )
        session.commit()
    return _back("/competitors", "added")


@router.post("/competitors/{competitor_id}/approve")
def approve(competitor_id: int):
    if not _set_status(competitor_id, "active"):
        return _back("/competitors")
    return _back(f"/competitors/{competitor_id}", "approved")


@router.post("/competitors/{competitor_id}/reject")
def reject(competitor_id: int):
    _set_status(competitor_id, "rejected")
    return _back("/competitors", "rejected")


def _set_status(competitor_id: int, status: str) -> bool:
    """Меняет состояние конкурента. False — такого конкурента нет."""
    with SessionLocal() as session:
        competitor = session.get(Competitor, competitor_id)
        if competitor is None:
            return False
        competitor.status = status
        if status == "active":
            competitor.approved_at = utcnow()
        session.commit()
    return True


@router.post("/competitors/{competitor_id}/scan")
def scan_one(competitor_id: int, tasks: BackgroundTasks):
    tasks.add_task(_run_in_background, _scan_one_work, competitor_id)
    return _back(f"/competitors/{competitor_id}", "scan_started")


@router.post("/actions/scan")
def scan_everyone(tasks: BackgroundTasks):
    tasks.add_task(_run_in_background, _scan_all_work)
    return _back("/", "scan_started")


@router.post("/actions/discover")
def discover(tasks: BackgroundTasks):
    tasks.add_task(_run_in_background, _discover_work)
    return _back("/competitors", "discover_started")


@router.post("/actions/pricing")
def recommend_pricing(tasks: BackgroundTasks):
    tasks.add_task(_run_in_background, _pricing_work)
    return _back("/prices", "pricing_started")


def _scan_one_work(competitor_id: int) -> None:
    with SessionLocal() as session:
        competitor = session.get(Competitor, competitor_id)
        if competitor is not None:
            scan.scan_competitor(session, competitor)


def _scan_all_work() -> None:
    with SessionLocal() as session:
        scan.scan_all(session)


def _discover_work() -> None:
    with SessionLocal() as session:
        try:
            discovery.run_discovery(session)
        except discovery.DiscoveryLimitError:
            log.warning("дневной лимит поиска исчерпан")


def _pricing_work() -> None:
    with SessionLocal() as session:
        result = pricing.recommend(session)
    result["made_at"] = _fmt(utcnow())
    PRICING_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
