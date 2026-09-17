"""Chat provider switch (local Qwen <-> DeepSeek), local fallback, and schema-retry in recomposition."""

import httpx
import openai
import pytest

from magpie import config
from magpie.recompose import plan_schema_problems


@pytest.fixture()
def clean_env(monkeypatch):
    for k in list(config.os.environ):
        if k.startswith(("MAGPIE_", "DEEPSEEK_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MAGPIE_LLM_PROVIDER", "openai")
    config.reset_settings()
    yield monkeypatch
    config.reset_settings()


def test_default_is_local_chat(clean_env):
    s = config.Settings()
    ep = s.chat_endpoint()
    assert ep.name == "local" and ep.model == "qwen3:8b-16k" and "11434" in ep.base_url
    assert s.chat_fallback_endpoint() is None
    assert s.vision_endpoint().host == s.embed_endpoint().host == "localhost:11434"


def test_deepseek_switch_keeps_vision_and_embed_local(clean_env):
    clean_env.setenv("MAGPIE_CHAT_PROVIDER", "deepseek")
    clean_env.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    s = config.Settings()
    ep = s.chat_endpoint()
    assert ep.name == "deepseek" and ep.model == "deepseek-v4-pro" and ep.host == "api.deepseek.com"
    assert ep.api_key == "sk-test-not-real" and ep.reasoning_budget > 0
    fb = s.chat_fallback_endpoint()
    assert fb is not None and fb.name == "local" and fb.model == "qwen3:8b-16k"
    assert s.vision_endpoint().host == "localhost:11434" and s.embed_endpoint().host == "localhost:11434"


def test_deepseek_thinking_switch(clean_env):
    clean_env.setenv("MAGPIE_CHAT_PROVIDER", "deepseek")
    clean_env.setenv("DEEPSEEK_API_KEY", "x")
    clean_env.setenv("DEEPSEEK_THINKING", "off")
    ep = config.Settings().chat_endpoint()
    assert ep.extra_body == {"thinking": {"type": "disabled"}} and ep.reasoning_budget == 0


def test_deepseek_without_key_is_a_clear_error(clean_env):
    clean_env.setenv("MAGPIE_CHAT_PROVIDER", "deepseek")
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        config.Settings().chat_endpoint()


def test_remote_failure_falls_back_to_local_once(clean_env):
    from magpie.llm import OpenAICompatLLM

    clean_env.setenv("MAGPIE_CHAT_PROVIDER", "deepseek")
    clean_env.setenv("DEEPSEEK_API_KEY", "x")
    llm = OpenAICompatLLM(config.Settings())
    calls = []

    def fake_chat_once(ep, messages, *, temperature, max_tokens, json_mode):
        calls.append((ep.name, ep.model, json_mode, max_tokens))
        if ep.name == "deepseek":
            raise openai.APIConnectionError(request=httpx.Request("POST", ep.base_url))
        return '{"ok": true}'

    llm._chat_once = fake_chat_once  # type: ignore[method-assign]
    assert llm.chat_json("sys", "user", max_tokens=100) == {"ok": True}
    assert [c[:2] for c in calls] == [("deepseek", "deepseek-v4-pro"), ("local", "qwen3:8b-16k")]
    assert llm.last_chat_model == "qwen3:8b-16k"


def test_images_never_go_to_the_chat_provider(clean_env):
    from magpie.llm import OpenAICompatLLM

    clean_env.setenv("MAGPIE_CHAT_PROVIDER", "deepseek")
    clean_env.setenv("DEEPSEEK_API_KEY", "x")
    llm = OpenAICompatLLM(config.Settings())
    seen = []
    llm._chat_once = lambda ep, messages, **kw: (seen.append(ep.name), '{"summary": "s"}')[1]  # type: ignore[method-assign]
    llm.chat_json("sys", "describe", images=[b"\xff\xd8fake"])
    assert seen == ["local"]


def test_unparsable_reply_is_nudged_then_fails_without_fallback(clean_env):
    from magpie.llm import OpenAICompatLLM

    llm = OpenAICompatLLM(config.Settings())
    attempts = []
    llm._chat_once = lambda ep, messages, **kw: (attempts.append(kw["json_mode"]), "not json at all")[1]  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="not parsable"):
        llm.chat_json("sys", "user")
    assert attempts == [True, False]


# ------------------------------------------------------------------ recomposition schema gate


def test_plan_schema_problems():
    ids = {"mat_a", "mat_b", "mat_c"}
    good = {"human_direction": "x", "groups": [{"name": "G", "members": [{"material_id": "mat_a", "reason": "r"}]}], "excluded": []}
    assert plan_schema_problems(good, ids) == []
    assert plan_schema_problems({"Material Pack": {"材质": ["001"]}}, ids)  # the qwen drift we saw
    assert plan_schema_problems({"groups": [{"name": "G", "members": [{"material_id": "mat_zzz", "reason": "r"}]}]}, ids)
    mostly_ok = {"groups": [{"name": "G", "members": [{"material_id": m, "reason": "r"} for m in ["mat_a", "mat_b", "mat_c"]] + [{"material_id": "mat_zzz", "reason": "r"}]}]}
    assert plan_schema_problems(mostly_ok, ids) == []  # one stray id out of four is tolerated (dropped later)


def test_recompose_retries_once_on_off_schema_reply(db, ingestor):
    from conftest import make_image_bytes
    from magpie.models import CapturePayload, Human
    from magpie.recompose import Recomposer
    from magpie.retrieval import Retriever

    for i in range(3):
        ingestor.ingest_image(make_image_bytes(text=f"i{i}"), f"i{i}.png", CapturePayload(modality="image", human=Human(thought=f"想法{i} 纸张质感")))
    fake = ingestor.llm
    real = fake.chat_json
    state = {"n": 0}

    def flaky(system, user, **kw):
        if kw.get("purpose") == "recompose":
            state["n"] += 1
            if state["n"] == 1:
                return {"Material Pack": {"材质": ["001_x"]}}  # off-schema first reply
            assert "rejected" in user  # repair prompt carries the problems
        return real(system, user, **kw)

    fake.chat_json = flaky  # type: ignore[method-assign]
    pack = Recomposer(db=db, retriever=Retriever(db=db, llm=fake), llm=fake).build("做一个有纸张质感的首页", limit=3)
    assert state["n"] == 2 and pack.generation["fallback"] is False and pack.generation["plan_attempts"] == 2
    assert pack.member_ids()
