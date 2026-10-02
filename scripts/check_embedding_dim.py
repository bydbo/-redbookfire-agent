"""一次性校准：实测 embedding 模型输出维度，与数据契约的 `vector(1024)` 对齐。

用途：S2.2 开工前置条件（DoR）。`vector(1024)` 会在 S2.3 的初始迁移里固化，
      等 S2.5 才发现维度不一致就要返工迁移、模型、回填与契约。
输入：无需参数——读 `config/config.toml` 的 `[embedding]` 段与 `config/.env` 的密钥；
      可用 `--text` 换校准文本、`--expected` 换期望维度。
输出：模型名、端点、实测维度、期望维度与结论（**绝不打印密钥**）；退出码 0 = 一致。

用法：

    uv run python scripts/check_embedding_dim.py
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from xhs_agent.config import load_config

DEFAULT_TEXT = "热点相关性检索的向量维度校准"


def embed(base_url: str, model: str, api_key: str, text: str, timeout: int = 60) -> list[float]:
    """调一次 OpenAI 兼容的 /embeddings，返回向量。"""
    payload = json.dumps({"model": model, "input": text}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/embeddings",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8", errors="replace"))
    return list(data["data"][0]["embedding"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校准 embedding 输出维度")
    parser.add_argument("--text", default=DEFAULT_TEXT, help="校准用的短文本")
    parser.add_argument("--expected", type=int, default=1024, help="契约期望的维度")
    args = parser.parse_args(argv)

    cfg = load_config()
    embedding = cfg.embedding
    api_key = embedding.resolved_key()
    if not api_key:
        print(f"缺少密钥：{embedding.api_key_env} 为空——请写进 config/.env 或导出同名环境变量",
              file=sys.stderr)
        print(f"修复：把 {embedding.api_key_env}=<你的密钥> 写进 config/.env（模板见 "
              "config/.env.example）", file=sys.stderr)
        return 1

    print(f"模型：{embedding.model}")
    print(f"端点：{embedding.base_url}")
    print(f"调用参数：input=<{len(args.text)} 字短文本>，未传 dimensions（取服务端默认）")
    try:
        vector = embed(embedding.base_url, embedding.model, api_key, args.text)
    except (urllib.error.URLError, KeyError, ValueError, json.JSONDecodeError) as exc:
        print(f"调用失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    actual = len(vector)
    print(f"实测维度：{actual}")
    print(f"契约维度：{args.expected}")
    if actual == args.expected:
        print("结论：一致 ✅ —— 契约无需修改，S2.2 可以直接开工")
        return 0
    print("结论：不一致 ❌ —— 先补 ADR，再按检索契约 §九 修订数据契约与检索契约，然后才动模型代码",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
