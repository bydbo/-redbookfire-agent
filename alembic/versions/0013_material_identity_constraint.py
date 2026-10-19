"""E6 身份迁移收口；旧库必须先 plan / apply / verify，再升级此 revision。"""

import sqlalchemy as sa

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

PATH_CHECK = ("path <> '' AND path !~ '(^/|:|\\\\|(^|/)[.]{1,2}(/|$)|//|/$|[[:cntrl:]]"
              "|(^|/)[[:space:]]|[[:space:]]($|/)|\\x7f)'")


def upgrade() -> None:
    if op.get_bind().execute(sa.text(f"SELECT EXISTS(SELECT 1 FROM materials WHERE NOT ({PATH_CHECK}))")).scalar():
        raise RuntimeError("存在旧格式素材身份：停在 0012，执行 normalize_material_identity.py plan/apply/verify")
    op.create_check_constraint("path_canonical", "materials", PATH_CHECK)


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT EXISTS(SELECT 1 FROM materials) OR EXISTS(SELECT 1 FROM material_identity_batches)")).scalar():
        raise RuntimeError("非空身份库不支持格式回退；请从停写前备份恢复，不拼接绝对路径")
    op.drop_constraint("path_canonical", "materials", type_="check")
