import enum
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Sequence,
    String,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class WordType(str, enum.Enum):
    parasite = 'parasite'
    commonly_mispronounced = 'commonly-mispronounced'


class Word(Base):
    __tablename__ = 'word'

    # Explicit sequence: seeded ids (0-22193) come from the JSON, while rows
    # created through the admin page draw from word_id_seq.
    id: Mapped[int] = mapped_column(
        Integer, Sequence('word_id_seq'), primary_key=True, autoincrement=False
    )
    # native_enum values must match the word_type enum in db/schema.sql.
    type: Mapped[WordType] = mapped_column(
        Enum(
            WordType,
            name='word_type',
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        index=True,
    )
    word: Mapped[str] = mapped_column(String, nullable=False)
    audio_key: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    correct_versions: Mapped[list['CorrectVersion']] = relationship(
        back_populates='parent',
        cascade='all, delete-orphan',
        order_by='CorrectVersion.position',
        lazy='selectin',
    )

    __table_args__ = (CheckConstraint("word <> ''", name='word_not_empty'),)


class CorrectVersion(Base):
    __tablename__ = 'correct_version'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    word_id: Mapped[int] = mapped_column(
        ForeignKey('word.id', ondelete='CASCADE'), nullable=False, index=True
    )
    word: Mapped[str] = mapped_column(String, nullable=False)
    incorrect_usage: Mapped[str | None] = mapped_column(String, nullable=True)
    correct_usage: Mapped[str | None] = mapped_column(String, nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    parent: Mapped[Word] = relationship(back_populates='correct_versions')
