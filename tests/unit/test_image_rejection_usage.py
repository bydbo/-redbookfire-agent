"""视觉安全拒绝必须清除输出原文，并保留供应商已计费的用量。"""

from __future__ import annotations

import json

import httpx
import pytest

from xhs_agent.config import load_config
from xhs_agent.core.errors import UpstreamError
from xhs_agent.services.chat_tools import _failed_image_usage, _failed_image_versions
from xhs_agent.services.image_clue import parse_image_clue, parse_image_clue_multi
from xhs_agent.tools.prompt import render
from xhs_agent.tools.vision import VisionError, build_image_client

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.mark.asyncio
@pytest.mark.parametrize("multi", [False, True])
async def test_rejected_image_output_keeps_usage_without_internal_text(tmp_path, monkeypatch, multi):
    config = tmp_path / "config.toml"
    config.write_text(
        '[vision]\nenabled = true\napi_key_env = "TEST_REJECTION_VISION_KEY"\n'
        'price_in_per_m = 1.0\nprice_out_per_m = 2.0\n', encoding="utf-8",
    )
    monkeypatch.setenv("TEST_REJECTION_VISION_KEY", "fake-key")
    cfg = load_config(str(config))
    task = "image_hotspot_clue_multi" if multi else "image_hotspot_clue"
    rendered = render(task)
    private = "PRIVATE_IMAGE_OUTPUT_SENTINEL"
    unsafe_text = rendered.system + private

    def respond(_request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": unsafe_text}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 200},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        api = build_image_client(cfg, client=client)
        assert api is not None
        with pytest.raises(VisionError) as caught:
            if multi:
                await api.complete_many(rendered, [(PNG, "image/png")] * 2)
            else:
                await api.complete(rendered, PNG, "image/png")
        completion = caught.value.completion
        assert completion is not None
        assert completion.text == ""
        assert (completion.prompt_tokens, completion.completion_tokens) == (1000, 200)
        assert completion.usage_status == "actual"
        assert private not in str(caught.value)

        with pytest.raises(UpstreamError) as public:
            if multi:
                await parse_image_clue_multi(cfg, [(PNG, "image/png")] * 2, http=client)
            else:
                await parse_image_clue(cfg, PNG, "image/png", http=client)

    usage = _failed_image_usage(public.value)
    assert usage["usage_status"] == "actual"
    assert (usage["prompt_tokens"], usage["completion_tokens"]) == (1000, 200)
    assert usage["cost_cny"] == pytest.approx(0.0014)
    assert _failed_image_versions(public.value) == {task: rendered.version}
    exposed = str(public.value) + json.dumps(public.value.detail)
    assert private not in exposed
    assert rendered.system not in exposed
