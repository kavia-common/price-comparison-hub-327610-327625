from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""


class SiteConfig(Base):
    """Admin-managed configuration for a target site/parser."""

    __tablename__ = "site_configs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    parser_key: Mapped[str] = mapped_column(String(128), default="generic")
    robots_policy: Mapped[str] = mapped_column(String(32), default="respect")  # respect|ignore
    fetch_mode: Mapped[str] = mapped_column(String(32), default="http")  # http|headless
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=dt.datetime.utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=dt.datetime.utcnow,
        onupdate=dt.datetime.utcnow,
    )


class CompareQuery(Base):
    """A user compare request, stored for history/detail views."""

    __tablename__ = "compare_queries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cache_key: Mapped[str] = mapped_column(String(64), index=True)
    product_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    input_urls: Mapped[list[str]] = mapped_column(JSONB, default=list)
    normalized_terms: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="completed")  # queued|running|completed|failed
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=dt.datetime.utcnow)

    offers: Mapped[list["Offer"]] = relationship(back_populates="query", cascade="all, delete-orphan")


class Offer(Base):
    """A single offer extracted from a specific URL/source for a query."""

    __tablename__ = "offers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    query_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("compare_queries.id"), index=True)
    source_domain: Mapped[str] = mapped_column(String(255), index=True)
    source_url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, default="")
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    price_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)  # cents; nullable if unknown
    availability: Mapped[str] = mapped_column(String(64), default="unknown")
    raw_payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=dt.datetime.utcnow)

    query: Mapped[CompareQuery] = relationship(back_populates="offers")
    history: Mapped[list["OfferHistory"]] = relationship(
        back_populates="offer", cascade="all, delete-orphan"
    )


class OfferHistory(Base):
    """Price history snapshots for an offer across runs."""

    __tablename__ = "offer_history"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    offer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("offers.id"), index=True)
    captured_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=dt.datetime.utcnow)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    price_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    availability: Mapped[str] = mapped_column(String(64), default="unknown")

    offer: Mapped[Offer] = relationship(back_populates="history")


Index("ix_offer_query_domain", Offer.query_id, Offer.source_domain)
