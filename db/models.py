import datetime as dt

from sqlalchemy import ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Competitor(Base):
    __tablename__ = "competitors"
    __table_args__ = (UniqueConstraint("domain", name="uq_competitors_domain"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    domain: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="candidate")  # candidate | active | rejected
    is_own: Mapped[bool] = mapped_column(default=False)
    discovered_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    approved_at: Mapped[dt.datetime | None] = mapped_column(default=None)

    snapshots: Mapped[list["PageSnapshot"]] = relationship(back_populates="competitor")
    services: Mapped[list["ServiceOffering"]] = relationship(back_populates="competitor")
    promotions: Mapped[list["Promotion"]] = relationship(back_populates="competitor")
    reviews: Mapped[list["Review"]] = relationship(back_populates="competitor")
    changes: Mapped[list["ChangeLog"]] = relationship(back_populates="competitor")


class PageSnapshot(Base):
    __tablename__ = "page_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id"))
    url: Mapped[str] = mapped_column(Text)
    raw_text: Mapped[str] = mapped_column(Text)
    extracted_json: Mapped[str | None] = mapped_column(Text, default=None)
    captured_at: Mapped[dt.datetime] = mapped_column(default=utcnow)

    competitor: Mapped["Competitor"] = relationship(back_populates="snapshots")


class ServiceOffering(Base):
    __tablename__ = "service_offerings"

    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id"))
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    first_seen_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    active: Mapped[bool] = mapped_column(default=True)

    competitor: Mapped["Competitor"] = relationship(back_populates="services")
    prices: Mapped[list["PriceEntry"]] = relationship(back_populates="service")


class PriceEntry(Base):
    __tablename__ = "price_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    service_offering_id: Mapped[int] = mapped_column(ForeignKey("service_offerings.id"))
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("page_snapshots.id"))
    price_text: Mapped[str] = mapped_column(Text)
    captured_at: Mapped[dt.datetime] = mapped_column(default=utcnow)

    service: Mapped["ServiceOffering"] = relationship(back_populates="prices")


class Promotion(Base):
    __tablename__ = "promotions"

    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id"))
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("page_snapshots.id"))
    description: Mapped[str] = mapped_column(Text)
    captured_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    active: Mapped[bool] = mapped_column(default=True)

    competitor: Mapped["Competitor"] = relationship(back_populates="promotions")


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id"))
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("page_snapshots.id"))
    rating: Mapped[float | None] = mapped_column(default=None)
    reviews_count: Mapped[int | None] = mapped_column(default=None)
    portfolio_size: Mapped[int | None] = mapped_column(default=None)
    source: Mapped[str | None] = mapped_column(Text, default=None)
    captured_at: Mapped[dt.datetime] = mapped_column(default=utcnow)

    competitor: Mapped["Competitor"] = relationship(back_populates="reviews")


class DiscoveryRun(Base):
    """Журнал запусков авто-поиска — нужен для дневного лимита обращений к Search API."""

    __tablename__ = "discovery_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(Text)  # manual | scheduled
    queries: Mapped[str] = mapped_column(Text)
    found_count: Mapped[int] = mapped_column(default=0)
    new_count: Mapped[int] = mapped_column(default=0)
    started_at: Mapped[dt.datetime] = mapped_column(default=utcnow)


class ChangeLog(Base):
    __tablename__ = "change_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id"))
    change_type: Mapped[str] = mapped_column(Text)  # new_service | price_change | new_promotion | rating_change
    description: Mapped[str] = mapped_column(Text)
    captured_at: Mapped[dt.datetime] = mapped_column(default=utcnow)

    competitor: Mapped["Competitor"] = relationship(back_populates="changes")
