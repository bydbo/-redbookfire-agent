"""E8 第二阶段：解除 `run_matches.material_id` 对当前素材的删除级联。

候选行保留原 UUID 作为历史身份；当前素材是否存在由读侧动态查询，不写回历史字段。
回退会重新加回 CASCADE 外键，因此存在孤儿候选时拒绝 downgrade，避免历史被静默删除。
"""

import sqlalchemy as sa

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

FK_NAME = "fk_run_matches_material_id_materials"


def upgrade() -> None:
    op.drop_constraint(FK_NAME, "run_matches", type_="foreignkey")


def downgrade() -> None:
    bind = op.get_bind()
    orphans = bind.execute(sa.text(
        "SELECT count(*) FROM run_matches AS m "
        "LEFT JOIN materials AS x ON x.id = m.material_id WHERE x.id IS NULL"
    )).scalar()
    if orphans:
        raise RuntimeError(
            f"存在 {orphans} 条历史候选引用已删除素材；回退会级联删除历史，已拒绝"
        )
    op.create_foreign_key(FK_NAME, "run_matches", "materials",
                          ["material_id"], ["id"], ondelete="CASCADE")
