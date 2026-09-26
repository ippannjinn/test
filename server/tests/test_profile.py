import pytest

from nextai.backends import build_backends
from nextai.db import Database
from nextai.models.catalog import Catalog
from nextai.models.manager import ModelManager
from nextai.models.planner import plan_llm
from nextai.profile.analyzer import analyze
from nextai.profile.engine import ProfileEngine, tuning_label
from nextai.resources.governor import Governor, Level
from nextai.resources.monitor import MockGpuProvider, ResourceMonitor

from conftest import make_settings


@pytest.fixture
def engine(tmp_path):
    s = make_settings(tmp_path)
    s.paths.ensure()
    db = Database(s.paths.db)
    db.migrate()
    gpu = MockGpuProvider()
    gov = Governor(s)
    gov.evaluate(ResourceMonitor(s.paths.data_dir, gpu).sample())
    mgr = ModelManager(s, db, Catalog.load(), build_backends(s, gpu), gov)
    congestion = {"v": 0.0}
    eng = ProfileEngine(s, mgr, gov, lambda: congestion["v"])
    return eng, congestion, gov, mgr


@pytest.mark.parametrize("text,task", [
    ("こんにちは", "chat"),
    ("Pythonでフィボナッチ数列の関数を書いて", "coding"),
    ("今日の東京の天気を調べて", "research"),
    ("猫の画像を生成して", "image_gen"),
    ("海の動画を作って", "video_gen"),
    ("落ち着いたBGMを作曲して", "music_gen"),
    ("TODO管理アプリを作って。テストも書いて", "project"),
    ("この文章を英語に翻訳して: 今日は晴れ", "translation"),
    ("量子力学と古典力学の違いを比較して分析して", "reasoning"),
])
def test_analyzer_task_types(text, task):
    assert analyze(text).task_type == task


def test_greeting_is_speed_and_uses_fast_model(engine):
    eng, *_ = engine
    p = eng.decide(analyze("こんにちは"))
    assert p.label == "Speed" and p.model_id == "qwen3-4b-instruct" and not p.use_agent
    assert p.priority_class == "interactive"


def test_complex_coding_is_autonomous_with_tools(engine):
    eng, *_ = engine
    text = "次の要件でWebスクレイパーのCLIツールを作って。\n1. URLを受け取る\n2. リンクを抽出\n3. テストを書く\nそして動作を検証して"
    p = eng.decide(analyze(text))
    assert p.tuning >= 0.75 and p.use_agent and p.plan and p.verify
    assert p.model_id == "qwen3-coder-30b-a3b"
    assert {"write_file", "run_code"} <= set(p.tools)
    assert p.limits["max_steps"] > 6 and p.limits["max_consecutive_failures"] >= 1


def test_congestion_shifts_toward_speed_unless_quality_pinned(engine):
    eng, cong, *_ = engine
    a = analyze("日本の経済政策の歴史を比較して分析して")
    base = eng.decide(a)
    cong["v"] = 1.0
    busy = eng.decide(a)
    assert busy.tuning < base.tuning
    pinned = eng.decide(analyze("日本の経済政策の歴史を比較して分析して。じっくり高品質で"))
    assert pinned.quality_pinned and pinned.tuning >= 0.75 and pinned.wait_for_quality


def test_continuous_labels():
    assert [tuning_label(t) for t in (0.1, 0.3, 0.5, 0.7, 0.9)] == [
        "Speed", "Speed寄りBalanced", "Balanced", "Balanced寄りAutonomous", "Autonomous"]


def test_policy_interpolation(engine):
    eng, *_ = engine
    lo, mid, hi = eng.policy_at(0.0), eng.policy_at(0.5), eng.policy_at(1.0)
    assert lo["max_steps"] < mid["max_steps"] < hi["max_steps"]
    q = eng.policy_at(0.25)
    assert lo["max_tokens"] < q["max_tokens"] < mid["max_tokens"]


def test_reevaluation_escalates_and_lightens(engine):
    eng, cong, gov, _ = engine
    p = eng.decide(analyze("Pythonでソートアルゴリズムを実装して実行して"))
    if p.model_id != "qwen3-4b-instruct":
        p.fallback_models = [p.model_id] + p.fallback_models
        p.model_id = "qwen3-4b-instruct"
    esc = eng.reevaluate(p, "consecutive_failures")
    assert esc and esc.model_id != "qwen3-4b-instruct" and esc.revision == p.revision + 1
    gov.state.level = Level.HIGH
    light = eng.reevaluate(p, "pressure")
    assert light and light.tool_parallelism == 1 and light.limits["max_steps"] <= 6
    rep = eng.reevaluate(p, "repetition")
    assert rep and rep.plan


def test_media_profile(engine):
    eng, *_ = engine
    # chat turn: an LLM drives it and gets the generate_* tool (plus the other media tools)
    chat = eng.decide(analyze("夜景の写真風の画像を生成して"))
    assert chat.model_id and eng.manager.catalog.get(chat.model_id).kind == "llm"
    assert chat.use_agent and chat.tools[0] == "generate_image" and "generate_music" in chat.tools
    assert not chat.plan and not chat.verify and chat.limits["max_seconds"] >= 900
    # the tool's own profile picks the media model and parameters
    p = eng.decide_media(analyze("夜景の写真風の画像を生成して"))
    assert p.model_id == "flux1-schnell" and p.media["width"] <= 1024 and p.residency == "transient"
    v = eng.decide_media(analyze("花が咲く動画を作って"))
    assert v.media["frames"] <= 49 and v.priority_class == "batch"
    # plain chat gets no media tools
    assert not any(t.startswith("generate_") for t in eng.decide(analyze("こんにちは")).tools)


def test_moe_planner_offloads_experts():
    cat = Catalog.load()
    spec = cat.get("qwen3-30b-a3b-instruct")
    full = plan_llm(spec, 18600, vram_budget_mb=30000, ram_budget_mb=20000)
    assert full.n_cpu_moe == 0
    tight = plan_llm(spec, 18600, vram_budget_mb=7000, ram_budget_mb=20000)
    assert tight and 0 < tight.n_cpu_moe < 48 and tight.est_vram_mb <= 7000
    tiny = plan_llm(spec, 18600, vram_budget_mb=900, ram_budget_mb=20000)
    assert tiny is None or tiny.ctx < 32768
    dense = cat.get("qwen3-4b-instruct")
    part = plan_llm(dense, 2500, vram_budget_mb=2600, ram_budget_mb=8000, ctx=16384)
    assert part and (part.n_gpu_layers < 999 or part.ctx < 16384)


def test_model_set_selection():
    cat = Catalog.load()
    assert cat.select_set(vram_gb=11.9, ram_gb=31.5, disk_free_gb=131)["selected"] == "rtx12g-standard"
    assert cat.select_set(vram_gb=11.9, ram_gb=31.5, disk_free_gb=70)["selected"] == "gpu-compact"
    assert cat.select_set(vram_gb=0, ram_gb=16, disk_free_gb=40)["selected"] == "minimal"
    std = cat.set_by_id("rtx12g-standard")
    assert cat.set_size_gb(std) + 15 < 131


def test_web_search_is_always_available_for_real_questions(engine):
    eng, *_ = engine
    q = eng.decide(analyze("北海道で一番高い山の標高はどれくらいですか？"))
    assert "web_search" in q.tools and "web_fetch" in q.tools and q.use_agent
    assert "web_search" not in eng.decide(analyze("こんにちは")).tools
    assert "web_search" not in eng.decide(analyze("この文を英語に翻訳して: おはよう")).tools
