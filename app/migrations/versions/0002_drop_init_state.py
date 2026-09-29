"""Drop init_state.

The pre-Alembic seeder recorded schema and data fingerprints there to decide
whether to re-sync on every start. Migrations now own the schema and seeding
is a one-time import, so nothing reads it any more.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-29
"""
from collections.abc import Sequence

from alembic import op

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # IF EXISTS: only databases built by the old seeder have it.
    op.execute('DROP TABLE IF EXISTS init_state')


def downgrade() -> None:
    # Nothing to restore: the table held derived bookkeeping only.
    pass
