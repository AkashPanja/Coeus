from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary,
                        String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from coeus.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)


class Camera(Base):
    __tablename__ = "cameras"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    # Live monitoring
    monitor_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    rtsp_url: Mapped[str] = mapped_column(String(512), default="")
    sub_rtsp_url: Mapped[str] = mapped_column(String(512), default="")
    onvif_host: Mapped[str] = mapped_column(String(128), default="")
    onvif_port: Mapped[int] = mapped_column(Integer, default=80)
    onvif_user: Mapped[str] = mapped_column(String(128), default="")
    onvif_password: Mapped[str] = mapped_column(String(128), default="")
    detect_fps: Mapped[float] = mapped_column(Float, default=5.0)
    face_mode: Mapped[str] = mapped_column(String(32), default="local")  # local|llm|local+llm|off
    cooldown_s: Mapped[int] = mapped_column(Integer, default=30)
    # Archive
    recording_folder: Mapped[str] = mapped_column(String(256), default="")
    archive_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    yt_title_template: Mapped[str] = mapped_column(
        String(256), default="{camera} — {date} (part {part}/{parts})")
    yt_privacy: Mapped[str] = mapped_column(String(16), default="private")
    yt_playlist_id: Mapped[str] = mapped_column(String(64), default="")

    zones: Mapped[list[Zone]] = relationship(back_populates="camera", cascade="all, delete-orphan")


class Zone(Base):
    __tablename__ = "zones"
    id: Mapped[int] = mapped_column(primary_key=True)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), default="include")  # include|exclude
    points: Mapped[list] = mapped_column(JSON, default=list)  # [[x,y],...] normalised 0..1
    min_motion_ratio: Mapped[float] = mapped_column(Float, default=0.01)
    camera: Mapped[Camera] = relationship(back_populates="zones")


class Person(Base):
    __tablename__ = "persons"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    samples: Mapped[list[FaceSample]] = relationship(back_populates="person",
                                                     cascade="all, delete-orphan")


class FaceSample(Base):
    __tablename__ = "face_samples"
    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    image_path: Mapped[str] = mapped_column(String(512))
    backend: Mapped[str] = mapped_column(String(32), default="sface")
    embedding: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    person: Mapped[Person] = relationship(back_populates="samples")


class Rule(Base):
    __tablename__ = "rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    event_types: Mapped[list] = mapped_column(JSON, default=list)   # empty = any
    camera_ids: Mapped[list] = mapped_column(JSON, default=list)    # empty = any
    zone_names: Mapped[list] = mapped_column(JSON, default=list)    # empty = any
    person_ids: Mapped[list] = mapped_column(JSON, default=list)    # empty = any
    time_start: Mapped[str] = mapped_column(String(5), default="")  # "HH:MM" local
    time_end: Mapped[str] = mapped_column(String(5), default="")
    days: Mapped[list] = mapped_column(JSON, default=list)          # 0=Mon..6=Sun, empty=any
    cooldown_s: Mapped[int] = mapped_column(Integer, default=60)
    actions: Mapped[list] = mapped_column(JSON, default=list)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    camera_id: Mapped[int | None] = mapped_column(ForeignKey("cameras.id", ondelete="SET NULL"),
                                                  nullable=True)
    camera_name: Mapped[str] = mapped_column(String(128), default="")
    zone: Mapped[str] = mapped_column(String(64), default="")
    type: Mapped[str] = mapped_column(String(32), index=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("persons.id", ondelete="SET NULL"),
                                                  nullable=True)
    person_name: Mapped[str] = mapped_column(String(128), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    snapshot_path: Mapped[str] = mapped_column(String(512), default="")
    face_path: Mapped[str] = mapped_column(String(512), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    extra: Mapped[dict] = mapped_column(JSON, default=dict)


class ActionLog(Base):
    __tablename__ = "action_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"),
                                                 nullable=True)
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("rules.id", ondelete="SET NULL"),
                                                nullable=True)
    action_type: Mapped[str] = mapped_column(String(32))
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[str] = mapped_column(Text, default="")


class ArchiveJob(Base):
    __tablename__ = "archive_jobs"
    __table_args__ = (UniqueConstraint("camera_folder", "day", "part"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    camera_folder: Mapped[str] = mapped_column(String(256))
    day: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD
    part: Mapped[int] = mapped_column(Integer, default=1)
    parts: Mapped[int] = mapped_column(Integer, default=1)
    # pending -> merging -> merged -> uploading -> done | failed | cancelled
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    segments: Mapped[list] = mapped_column(JSON, default=list)  # [{path,start,duration}]
    gaps: Mapped[list] = mapped_column(JSON, default=list)      # [[after_iso, seconds]]
    output_path: Mapped[str] = mapped_column(String(512), default="")
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    upload_session_uri: Mapped[str] = mapped_column(Text, default="")
    video_id: Mapped[str] = mapped_column(String(32), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)
