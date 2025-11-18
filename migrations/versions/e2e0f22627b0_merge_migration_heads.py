"""merge migration heads

Revision ID: e2e0f22627b0
Revises: 143c315bdad9, 779dfe522107
Create Date: 2025-11-18 18:52:17.786541

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e2e0f22627b0'
down_revision = ('143c315bdad9', '779dfe522107')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
