"""runs 表新增 topk 与 request_id（数据契约 §3.3 / §4.2）。

- `topk`：本批次检索的截断上限（1–20，默认 5），来自 `POST /api/analyze` 的 `topk`——
  参数必须真的影响检索，不能只在校验层收下；worker 在检索时读取（S3.4b）。
- `request_id`：提交请求的 `X-Request-ID`（可空），便于按请求排障。
- CHECK 约束**手工补写**：Alembic autogenerate 不对比 CHECK，只改 ORM 会留下漂移。

downgrade 只删这两列与其 CHECK，不动其它结构（与数据契约 §6 的边界一致）。

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("topk", sa.Integer(), server_default=sa.text("5"),
                                    nullable=False))
    op.add_column("runs", sa.Column("request_id", sa.Text(), nullable=True))
    op.create_check_constraint(op.f("ck_runs_topk_in_range"), "runs",
                               "topk >= 1 AND topk <= 20")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_runs_topk_in_range"), "runs", type_="check")
    op.drop_column("runs", "request_id")
    op.drop_column("runs", "topk")
