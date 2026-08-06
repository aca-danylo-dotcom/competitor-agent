"""Раскладка извлечённых данных по таблицам и детект изменений."""

import datetime as dt
import difflib
import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from config import settings
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
from services.pricing import parse_price


SIMILARITY_THRESHOLD = 0.75
# Акции — свободный текст, модель переписывает их сильнее, чем названия услуг.
PROMO_SIMILARITY_THRESHOLD = 0.55
# Сколько лишних слов допускаем у более длинной формулировки той же позиции.
# «Разработка чат-ботов» и «Разработка чат-ботов (Стандарт)» — одна услуга,
# а «тариф Стандарт» и «тариф Безлимит» — уже разные: у них разные слова с обеих сторон.
MAX_EXTRA_TOKENS = 1
# Акции формулируются вольнее, там разброс слов больше.
PROMO_MAX_EXTRA_TOKENS = 4
# Обрезка слова до основы: «сайта» и «сайтов» должны попадать в один токен.
STEM_LENGTH = 4


def key_of(name: str) -> str:
    """Ключ для сопоставления услуг между сканами: без регистра и лишних пробелов."""
    return re.sub(r"\s+", " ", name.strip().lower())


def _tokens(text: str) -> set[str]:
    return {word[:STEM_LENGTH] for word in re.findall(r"\w+", text.lower()) if word}


def _is_refinement(a: str, b: str, max_extra: int) -> bool:
    """Одна ли это позиция, названная короче и длиннее.

    Считаем «да», только если слова одного названия целиком входят в другое и лишних
    слов не больше max_extra. Если различающиеся слова есть с обеих сторон — это разные
    позиции («SMM» / «SEO», «Стандарт» / «Безлимит»), как бы похоже они ни выглядели.
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    extra_a, extra_b = ta - tb, tb - ta
    if extra_a and extra_b:
        return False
    return len(extra_a | extra_b) <= max_extra


def find_similar(
    key: str,
    candidates: list[str],
    threshold: float = SIMILARITY_THRESHOLD,
    max_extra: int = MAX_EXTRA_TOKENS,
) -> str | None:
    """Ищет тот же пункт под другой формулировкой.

    Модель от скана к скану переписывает названия услуг и акций («Личная консультация
    интернет-маркетолога» ↔ «Личная консультация интернет маркетолога»). Без этого
    каждая переформулировка выглядела бы как новая позиция.
    """
    if key in candidates:
        return key

    # Одного difflib мало: «SMM продвижение» и «SEO продвижение» отличаются тремя буквами,
    # но это разные услуги. Поэтому дополнительно требуем совпадения по словам.
    best: str | None = None
    best_score = 0.0
    for candidate in candidates:
        if not _is_refinement(key, candidate, max_extra):
            continue
        ratio = difflib.SequenceMatcher(None, key, candidate).ratio()
        if ratio < threshold:
            continue
        score = ratio
        if score > best_score:
            best, best_score = candidate, score

    return best


def _price_differs(old_text: str, new_text: str) -> bool:
    """Сравнивает цены по сути, а не по написанию: «18000 руб» и «18 000 руб» — одно и то же."""
    old, new = parse_price(old_text), parse_price(new_text)
    if old is None or new is None:
        return key_of(old_text) != key_of(new_text)
    return (old["low"], old["high"], old["currency"], old["period"]) != (
        new["low"],
        new["high"],
        new["currency"],
        new["period"],
    )


def _log(session: Session, competitor: Competitor, change_type: str, description: str) -> str:
    session.add(
        ChangeLog(competitor_id=competitor.id, change_type=change_type, description=description)
    )
    return description


def apply_scan(
    session: Session,
    competitor: Competitor,
    results: list[tuple[PageSnapshot, dict]],
) -> list[str]:
    """Сохраняет результаты скана конкурента и возвращает список замеченных изменений."""
    had_history = session.scalar(
        select(ServiceOffering.id).where(ServiceOffering.competitor_id == competitor.id).limit(1)
    ) is not None

    changes: list[str] = []

    for snapshot, data in results:
        snapshot.extracted_json = json.dumps(data, ensure_ascii=False)

    services = _merge_services(results)
    promotions = _merge_promotions(results)
    review_data, review_snapshot = _pick_review(results)

    changes += _ingest_services(session, competitor, services, had_history)
    changes += _ingest_promotions(session, competitor, promotions, had_history)
    if review_snapshot is not None:
        changes += _ingest_review(session, competitor, review_data, review_snapshot, had_history)

    if not had_history:
        _log(
            session,
            competitor,
            "first_scan",
            f"Первичный сбор: услуг — {len(services)}, акций — {len(promotions)}",
        )

    session.commit()
    return changes


def _merge_services(results: list[tuple[PageSnapshot, dict]]) -> dict[str, dict]:
    """Собирает услуги со всех страниц; страница с ценой перебивает страницу без цены."""
    merged: dict[str, dict] = {}
    for snapshot, data in results:
        for item in data.get("services", []):
            k = key_of(item["name"])
            existing = merged.get(k)
            if existing is None or (not existing["item"].get("price") and item.get("price")):
                merged[k] = {"item": item, "snapshot": snapshot}
    return merged


def _merge_promotions(results: list[tuple[PageSnapshot, dict]]) -> list[tuple[str, PageSnapshot]]:
    out: list[tuple[str, PageSnapshot]] = []
    used: set[str] = set()
    for snapshot, data in results:
        for promo in data.get("promotions", []):
            k = key_of(promo)
            if k in used:
                continue
            used.add(k)
            out.append((promo, snapshot))
    return out


def _pick_review(results: list[tuple[PageSnapshot, dict]]) -> tuple[dict, PageSnapshot | None]:
    """Берёт самую информативную страницу по репутации (рейтинг важнее счётчиков)."""
    best: dict = {}
    best_snapshot: PageSnapshot | None = None
    best_score = -1
    for snapshot, data in results:
        score = sum(
            2 if data.get(k) is not None and k == "rating" else 1 if data.get(k) is not None else 0
            for k in ("rating", "reviews_count", "portfolio_size")
        )
        if score > best_score:
            best_score, best, best_snapshot = score, data, snapshot
    return best, (best_snapshot if best_score > 0 else None)


def _ingest_services(
    session: Session, competitor: Competitor, services: dict[str, dict], had_history: bool
) -> list[str]:
    changes: list[str] = []
    existing = {
        key_of(s.name): s
        for s in session.scalars(
            select(ServiceOffering).where(ServiceOffering.competitor_id == competitor.id)
        ).all()
    }

    for k, payload in services.items():
        item, snapshot = payload["item"], payload["snapshot"]
        matched_key = find_similar(k, list(existing))
        offering = existing.get(matched_key) if matched_key else None

        if offering is None:
            offering = ServiceOffering(
                competitor_id=competitor.id,
                name=item["name"],
                description=item.get("description"),
            )
            session.add(offering)
            session.flush()
            existing[k] = offering
            if had_history:
                changes.append(
                    _log(session, competitor, "new_service", f"Новая услуга: {item['name']}")
                )
        else:
            offering.last_seen_at = utcnow()
            offering.active = True
            if item.get("description"):
                offering.description = item["description"]

        price_text = item.get("price")
        if not price_text:
            continue

        last_price = session.scalars(
            select(PriceEntry)
            .where(PriceEntry.service_offering_id == offering.id)
            .order_by(PriceEntry.captured_at.desc())
            .limit(1)
        ).first()

        if last_price is None:
            session.add(
                PriceEntry(
                    service_offering_id=offering.id,
                    snapshot_id=snapshot.id,
                    price_text=price_text,
                )
            )
            if had_history and matched_key is not None:
                changes.append(
                    _log(
                        session,
                        competitor,
                        "price_change",
                        f"У услуги «{offering.name}» появилась цена: {price_text}",
                    )
                )
        elif _price_differs(last_price.price_text, price_text):
            session.add(
                PriceEntry(
                    service_offering_id=offering.id,
                    snapshot_id=snapshot.id,
                    price_text=price_text,
                )
            )
            changes.append(
                _log(
                    session,
                    competitor,
                    "price_change",
                    f"«{offering.name}»: {last_price.price_text} → {price_text}",
                )
            )

    return changes


def _ingest_promotions(
    session: Session,
    competitor: Competitor,
    promotions: list[tuple[str, PageSnapshot]],
    had_history: bool,
) -> list[str]:
    changes: list[str] = []
    active = session.scalars(
        select(Promotion).where(
            Promotion.competitor_id == competitor.id, Promotion.active.is_(True)
        )
    ).all()
    active_by_key = {key_of(p.description): p for p in active}

    for text, snapshot in promotions:
        matched = find_similar(
            key_of(text),
            list(active_by_key),
            PROMO_SIMILARITY_THRESHOLD,
            PROMO_MAX_EXTRA_TOKENS,
        )
        if matched is not None:
            active_by_key[matched].last_seen_at = utcnow()
            continue
        promo = Promotion(
            competitor_id=competitor.id, snapshot_id=snapshot.id, description=text
        )
        session.add(promo)
        active_by_key[key_of(text)] = promo
        if had_history:
            changes.append(_log(session, competitor, "new_promotion", f"Новая акция: {text}"))

    # Акцию считаем снятой, только если её давно не видно, — иначе ловим переформулировки.
    stale_before = utcnow().replace(tzinfo=None) - dt.timedelta(days=settings.promotion_stale_days)
    for promo in active:
        if promo.last_seen_at.replace(tzinfo=None) < stale_before:
            promo.active = False
            changes.append(
                _log(session, competitor, "promotion_ended", f"Акция снята: {promo.description}")
            )

    return changes


def _ingest_review(
    session: Session,
    competitor: Competitor,
    data: dict,
    snapshot: PageSnapshot,
    had_history: bool,
) -> list[str]:
    changes: list[str] = []
    last = session.scalars(
        select(Review)
        .where(Review.competitor_id == competitor.id)
        .order_by(Review.captured_at.desc())
        .limit(1)
    ).first()

    rating = data.get("rating")
    reviews_count = data.get("reviews_count")
    portfolio_size = data.get("portfolio_size")

    same = (
        last is not None
        and last.rating == rating
        and last.reviews_count == reviews_count
        and last.portfolio_size == portfolio_size
    )
    if same:
        return changes

    session.add(
        Review(
            competitor_id=competitor.id,
            snapshot_id=snapshot.id,
            rating=rating,
            reviews_count=reviews_count,
            portfolio_size=portfolio_size,
            source=snapshot.url,
        )
    )

    if last is not None and had_history and last.rating != rating and rating is not None:
        changes.append(
            _log(
                session,
                competitor,
                "rating_change",
                f"Рейтинг: {last.rating} → {rating}",
            )
        )

    return changes
