"""报告渲染单测：区块齐全、HTML 转义、缺键回退与输出稳定性。

输入形状见 `src/xhs_agent/tools/report.py` 的模块 docstring；
区块口径见 `docs/产品方案.md` §8.1。
"""

from __future__ import annotations

from xhs_agent.tools import report

MARKDOWN_SECTIONS = (
    "# 热点相关性报告",
    "## 一、机会总览",
    "### 为什么能火",
    "### 热点线索（可迁移的爆点要素）",
    "### 可蹭角度",
    "### 素材匹配榜",
    "### 覆盖缺口",
    "### 初步文案",
    "## 三、本次运行",
)


class TestMarkdown:
    def test_covers_every_product_section(self, sample_report_model):
        text = report.render_markdown(sample_report_model)
        for section in MARKDOWN_SECTIONS:
            assert section in text

    def test_carries_hotspot_and_material_names(self, sample_report_model):
        text = report.render_markdown(sample_report_model)
        assert "某顶流明星打羽毛球被拍" in text
        assert "球场热身" in text

    def test_reports_totals_and_errors(self, sample_report_model):
        sample_report_model["errors"] = ["第一条错误"]
        text = report.render_markdown(sample_report_model)
        assert "模型调用：2 次" in text
        assert "第一条错误" in text

    def test_missing_keys_fall_back(self):
        assert "# 热点相关性报告" in report.render_markdown({})

    def test_is_stable(self, sample_report_model):
        assert report.render_markdown(sample_report_model) == \
            report.render_markdown(sample_report_model)


class TestHtml:
    def test_renders_standalone_document(self, sample_report_model):
        html = report.render_html(sample_report_model)
        assert "<html" in html
        assert "球场热身" in html

    def test_escapes_material_title(self, sample_report_model):
        sample_report_model["hotspots"][0]["candidates"][0]["material"]["title"] = \
            "<script>alert(1)</script>"
        html = report.render_html(sample_report_model)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_missing_keys_do_not_raise(self):
        assert report.render_html({})

    def test_is_stable(self, sample_report_model):
        assert report.render_html(sample_report_model) == report.render_html(sample_report_model)
