"""生成脱敏示例素材包 `evals/fixtures/demo_pack/`。

用途：一条命令重建示例素材包（19 条素材 + 同名旁车说明），供演示与评测集 v1 使用。
输入：`--out`（输出目录，默认 evals/fixtures/demo_pack）、`--force`（覆盖已存在文件）。
输出：素材文件 + 同名 `.txt` 旁车说明；退出码非 0 表示环境不满足（缺 ffmpeg）。

约定：
- 素材全部**合成**（纯色/渐变 + 中文标题文字，少量测试图案），零版权风险、可入库；
- 参数固定、可重复生成；**冻结以入库字节为准**，重新生成会改变字节（ffmpeg 版本差异），
  因此只在需要重建内容时运行，运行后必须重新生成本包的 manifest 哈希；
- 缺 ffmpeg 直接报错退出——项目不做运行时降级（AGENTS.md 第 4 节）。

用法：

    uv run python scripts/make_demo_pack.py
    uv run python scripts/make_demo_pack.py --out <目录> --force
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = PROJECT_ROOT / "evals" / "fixtures" / "demo_pack"

# 中文字体：Windows 用微软雅黑，其余平台回退到常见 CJK 字体；都找不到就退化为无文字画面。
FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)

IMAGE_SIZE = "720x1280"
VIDEO_SIZE = "480x854"
VIDEO_FPS = 24


@dataclass
class Asset:
    """一条素材：画面参数 + 旁车说明（旁车为 None 表示刻意"无说明"）。"""

    rel_path: str
    kind: str                      # image | video
    color: str                     # 背景色，形如 0x1F6FEB
    label: str                     # 画面上叠加的中文标题
    motion: bool = False           # 视频是否用测试图案（有动作感，体积更大）
    audio: bool = False            # 视频是否带音轨
    sidecar: dict = field(default_factory=dict)   # {"title":…, "tags":[…], "description":…}


ASSETS: tuple[Asset, ...] = (
    # ---------- 运动（5）----------
    Asset("运动/球场-挥拍.mp4", "video", "0x1F6FEB", "球场热身", motion=True, audio=True,
          sidecar={"title": "球场热身挥拍",
                   "tags": ["羽毛球", "球场", "挥拍", "反差"],
                   "description": "室内球场正面机位，球拍连续挥动；素人对打、身份反差感强，"
                                  "适合做「反差」类热点的过程画面"}),
    Asset("运动/球拍特写.jpg", "image", "0x2563EB", "球拍特写",
          sidecar={"title": "球拍与手部特写",
                   "tags": ["羽毛球", "球拍", "特写", "同款"],
                   "description": "球拍网面与手部特写，景深浅，适合做「同款」动作展示"}),
    Asset("运动/晨跑-公园.jpg", "image", "0x0EA5E9", "晨跑",
          sidecar={"title": "清晨公园晨跑",
                   "tags": ["跑步", "晨跑", "公园", "热血"],
                   "description": "清晨薄雾里的公园跑道，逆光，气氛热血，适合跟练类内容"}),
    Asset("运动/健身房-撸铁.mp4", "video", "0x7C3AED", "健身房撸铁",
          sidecar={"title": "健身房力量训练",
                   "tags": ["健身", "撸铁", "健身房", "解压"],
                   "description": "健身房哑铃推举中景，暖光，动作节奏解压"}),
    Asset("运动/跳绳-阳台.jpg", "image", "0x0891B2", "阳台跳绳",
          sidecar={"title": "居家阳台跳绳",
                   "tags": ["跳绳", "居家", "跟练", "低门槛"],
                   "description": "出租屋阳台跳绳，室内居家环境，动作低门槛、可跟练"}),

    # ---------- 美食（4）----------
    Asset("美食/家常菜-番茄炒蛋.jpg", "image", "0xEA580C", "番茄炒蛋",
          sidecar={"title": "番茄炒蛋出锅",
                   "tags": ["家常菜", "番茄炒蛋", "做饭", "新手小白"],
                   "description": "平底锅出菜俯拍，热气明显，步骤简单，适合新手小白"}),
    Asset("美食/咖啡-手冲.mp4", "video", "0x78350F", "手冲咖啡", motion=True,
          sidecar={"title": "手冲咖啡注水",
                   "tags": ["咖啡", "手冲", "门店", "治愈"],
                   "description": "手冲壶注水特写，光比大，氛围治愈，适合教程类开头"}),
    Asset("美食/探店-小馆.jpg", "image", "0xB45309", "探店小馆",
          sidecar={"title": "苍蝇馆子探店",
                   "tags": ["探店", "美食", "门店", "测评"],
                   "description": "小馆门口招牌与排队人群，门店实拍，可做探店测评开头"}),
    Asset("美食/减脂餐-沙拉.jpg", "image", "0x65A30D", "减脂沙拉",
          sidecar={"title": "低卡鸡胸沙拉",
                   "tags": ["减脂餐", "低卡", "沙拉", "焦虑"],
                   "description": "白盘沙拉俯拍，配色清淡，适合「节后焦虑」类话题"}),

    # ---------- 职场（4）----------
    Asset("职场/工位-加班.jpg", "image", "0x334155", "深夜工位",
          sidecar={"title": "深夜工位加班",
                   "tags": ["职场", "加班", "办公室通勤", "打工人", "共鸣"],
                   "description": "夜晚工位顶光，屏幕亮着，打工人共鸣感强"}),
    Asset("职场/通勤-地铁.mp4", "video", "0x475569", "早高峰通勤",
          sidecar={"title": "早高峰地铁通勤",
                   "tags": ["通勤", "地铁", "办公室通勤", "打工人", "焦虑"],
                   "description": "地铁车厢拥挤，手持轻微晃动，通勤焦虑感明显"}),
    Asset("职场/会议-白板.jpg", "image", "0x1E293B", "会议室白板",
          sidecar={"title": "会议室白板讨论",
                   "tags": ["会议", "白板", "办公室通勤", "清单合集"],
                   "description": "会议室白板写满流程，多人背影，可做清单合集的版式素材"}),
    Asset("职场/代码-笔记本.jpg", "image", "0x0F172A", "写代码",
          sidecar={"title": "笔记本上写代码",
                   "tags": ["程序员", "代码", "教程", "专业人士"],
                   "description": "笔记本屏幕代码特写，键盘前景虚化，适合教程与测评类内容"}),

    # ---------- 宠物（3）----------
    Asset("宠物/橘猫-翻肚皮.mp4", "video", "0xF59E0B", "橘猫翻肚皮", motion=True, audio=True,
          sidecar={"title": "橘猫翻肚皮求摸",
                   "tags": ["猫咪", "橘猫", "治愈", "解压", "萌宠IP"],
                   "description": "橘猫在沙发上翻肚皮，原声猫叫，治愈解压，萌宠 IP"}),
    Asset("宠物/柯基-遛狗.jpg", "image", "0xD97706", "遛柯基",
          sidecar={"title": "傍晚遛柯基",
                   "tags": ["狗狗", "柯基", "遛狗", "治愈"],
                   "description": "傍晚小区路面牵绳遛狗，低机位，画面治愈"}),
    Asset("宠物/猫咪-窗台.jpg", "image", "0xFBBF24", "窗台晒太阳",
          sidecar={"title": "窗台晒太阳的猫",
                   "tags": ["猫咪", "窗台", "居家", "治愈"],
                   "description": "逆光窗台猫侧影，窗帘半掩，居家治愈氛围"}),

    # ---------- 未整理（3，刻意无旁车说明、命名混乱）----------
    Asset("未整理/IMG_20260901_143012.jpg", "image", "0x1E40AF", "球场一角"),
    Asset("未整理/视频1.mp4", "video", "0xB45309", "沙发上的猫"),
    Asset("未整理/临时素材.mp4", "video", "0x78350F", "咖啡店桌面", motion=True),
)


def _find_font() -> str | None:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def _drawtext(label: str, font: str | None, size: int) -> list[str]:
    if not font:
        return []
    # ffmpeg 滤镜参数里 Windows 盘符的冒号必须转义，斜杠统一成正斜杠
    font_arg = font.replace("\\", "/").replace(":", r"\:")
    text = label.replace(":", r"\:").replace("'", r"\'")
    return ["-vf",
            f"drawtext=fontfile='{font_arg}':text='{text}':fontcolor=white:"
            f"fontsize={size}:x=(w-tw)/2:y=(h-th)/2"]


def _build_image(asset: Asset, out_path: Path, font: str | None) -> None:
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-f", "lavfi", "-i", f"color=c={asset.color}:s={IMAGE_SIZE}",
           "-frames:v", "1", *_drawtext(asset.label, font, 64),
           "-q:v", "4", str(out_path)]
    _run(cmd)


def _build_video(asset: Asset, out_path: Path, font: str | None) -> None:
    source = (f"testsrc2=size={VIDEO_SIZE}:rate={VIDEO_FPS}" if asset.motion
              else f"color=c={asset.color}:s={VIDEO_SIZE}:r={VIDEO_FPS}")
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", source]
    if asset.audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
    cmd += ["-t", "2", *_drawtext(asset.label, font, 48),
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "32", "-preset", "veryslow"]
    if asset.audio:
        cmd += ["-c:a", "aac", "-b:a", "48k", "-shortest"]
    cmd.append(str(out_path))
    _run(cmd)


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()[:400]
        raise RuntimeError(f"ffmpeg 失败：{' '.join(cmd[:6])}… -> {detail}")


def _write_sidecar(out_path: Path, sidecar: dict) -> None:
    lines = [f"标题: {sidecar['title']}", f"标签: {', '.join(sidecar['tags'])}"]
    if sidecar.get("description"):
        lines.append(f"描述: {sidecar['description']}")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def build(out_dir: Path, force: bool = False) -> list[Path]:
    """按内置清单生成整个素材包，返回写出的文件列表。"""
    if shutil.which("ffmpeg") is None:
        raise SystemExit("缺少 ffmpeg：示例素材包需要 ffmpeg 生成，请先安装后重试"
                         "（项目不做运行时降级，见 AGENTS.md 第 4 节）")
    font = _find_font()
    written: list[Path] = []
    for asset in ASSETS:
        target = out_dir / asset.rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not force:
            written.append(target)
            continue
        if asset.kind == "image":
            _build_image(asset, target, font)
        else:
            _build_video(asset, target, font)
        written.append(target)
        if asset.sidecar:
            sidecar_path = target.with_name(target.name + ".txt")
            _write_sidecar(sidecar_path, asset.sidecar)
            written.append(sidecar_path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 evals/fixtures/demo_pack 示例素材包")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的素材")
    args = parser.parse_args(argv)

    out_dir = Path(args.out).resolve()
    written = build(out_dir, force=args.force)
    assets = list(ASSETS)
    videos = sum(1 for asset in assets if asset.kind == "video")
    print(f"素材包已生成：{out_dir}")
    print(f"  素材 {len(assets)} 条（视频 {videos}、图片 {len(assets) - videos}）"
          f"，文件合计 {len(written)} 个（含旁车说明）")
    print("  提示：重新生成会改变字节，冻结前请更新 evals/manifest.json 的哈希")
    return 0


if __name__ == "__main__":
    sys.exit(main())
