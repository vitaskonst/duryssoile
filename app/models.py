import enum
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Sequence,
    Text,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# The schema itself is defined by the migrations in migrations/. The models
# mirror it, indexes and constraint names included, so that `alembic check`
# reports any drift between the two.


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
    type: Mapped[WordType] = mapped_column(
        Enum(
            WordType,
            name='word_type',
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    word: Mapped[str] = mapped_column(Text, nullable=False)
    audio_key: Mapped[str | None] = mapped_column(Text, nullable=True)
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

    __table_args__ = (
        CheckConstraint("word <> ''", name='word_word_check'),
        Index('word_type_id_idx', 'type', 'id'),
        # Supports the API's case-insensitive prefix filter.
        Index('word_type_lower_word_idx', 'type', text('lower(word) text_pattern_ops')),
    )


class CorrectVersion(Base):
    __tablename__ = 'correct_version'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    word_id: Mapped[int] = mapped_column(
        ForeignKey('word.id', ondelete='CASCADE'), nullable=False
    )
    word: Mapped[str] = mapped_column(Text, nullable=False)
    incorrect_usage: Mapped[str | None] = mapped_column(Text, nullable=True)
    correct_usage: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    parent: Mapped[Word] = relationship(back_populates='correct_versions')

    __table_args__ = (
        CheckConstraint("word <> ''", name='correct_version_word_check'),
        Index('correct_version_word_id_position_idx', 'word_id', 'position'),
    )
