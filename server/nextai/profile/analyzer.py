"""Cheap, deterministic task analysis (Japanese + English) used as Dynamic Profile input."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from ..util import clamp, estimate_tokens

_F = re.I | re.S


def _rx(p: str) -> re.Pattern:
    return re.compile(p, _F)


R_IMAGE = _rx(r"(画像|イラスト|絵|写真|アイコン|ロゴ|壁紙|ポスター).{0,15}(生成|作って|つくって|作成|描いて|かいて|出力)"
              r"|\b(draw|paint|generate|create|make)\b.{0,20}\b(image|picture|illustration|photo|logo|icon)\b")
R_VIDEO = _rx(r"(動画|ムービー|アニメーション|映像).{0,15}(生成|作って|つくって|作成)"
              r"|\b(generate|create|make)\b.{0,20}\b(video|animation|clip)\b")
R_MUSIC = _rx(r"(音楽|曲|BGM|メロディ|ジングル|サウンド).{0,15}(生成|作って|つくって|作成|作曲)|作曲して"
              r"|\bcompose\b|\b(generate|create|make)\b.{0,20}\b(music|song|melody|soundtrack)\b")
R_PROJECT = _rx(r"(アプリ|アプリケーション|プロジェクト|システム|サイト|ツール|ゲーム|ライブラリ|CLI|API).{0,15}"
                r"(作って|つくって|作成して|開発して|実装して|構築して|書いて)|複数(の)?ファイル|一式"
                r"|\b(build|create|scaffold|implement)\b.{0,25}\b(app|application|project|website|tool|game|library)\b")
R_CODE = _rx(r"```|コード|プログラム|関数|メソッド|クラス|実装|バグ|デバッグ|リファクタ|コンパイル|スクリプト|正規表現|例外|スタックトレース"
             r"|\b(python|javascript|typescript|java|c\+\+|c#|rust|golang|go言語|sql|html|css|bash|powershell|regex"
             r"|function|class|def|import|traceback|exception|compile|refactor|debug|bug|code)\b")
R_WEB = _rx(r"最新|今日|今週|現在の|ニュース|検索して|調べて|ググって|ウェブ|ネットで|価格|相場|天気|株価|為替|発売日|リリース"
            r"|\b(latest|news|search|look up|google|current|today|price|weather|release)\b")
R_URL = _rx(r"https?://[^\s<>\"']+")
R_REASON = _rx(r"証明|計算|数学|論理|推論|なぜ|理由|比較|分析|戦略|最適|設計|検討|評価|考察|トレードオフ|アルゴリズム"
               r"|\b(prove|calculate|math|analy[sz]e|compare|why|strategy|optimi[sz]e|design|evaluate|trade-?off)\b")
R_TRANSLATE = _rx(r"翻訳|英訳|和訳|英語に|日本語に|\btranslate\b")
R_SUMMARY = _rx(r"要約|まとめて|要点|サマリ|\bsummar(y|ize|ise)\b|\btl;?dr\b")
R_WRITING = _rx(r"文章|作文|メール|手紙|ブログ|記事|小説|物語|詩|キャッチコピー|スピーチ|挨拶文|レポート"
                r"|\bwrite\b.{0,20}\b(email|essay|story|article|poem|letter|blog)\b")
R_MEMORY = _rx(r"覚えて(おいて)?|記憶して|忘れないで|\bremember (that|this)\b")
R_EXEC = _rx(r"実行して|動かして|計算して|グラフ(に|を)|シミュレーション|検証して|テストして|\b(run|execute)\b.{0,15}\b(code|script|this)\b")
R_QUALITY = _rx(r"高品質|じっくり|詳しく|詳細に|丁寧に|徹底的|本気で|最高品質|しっかり|深く考えて|\b(thorough(ly)?|in detail|deep(ly)?|carefully)\b")
R_FAST = _rx(r"手短|簡潔|ざっくり|すぐに|急いで|一言で|短く|\b(brief(ly)?|quick(ly)?|short answer|tl;?dr)\b")
R_GREETING = _rx(r"^(こんにちは|こんばんは|おはよう|ありがとう|よろしく|はじめまして|hi|hello|hey|thanks|thank you)[!！。.\s]*$")
R_REQ = re.compile(r"^\s*(\d+[.)．、]|[-*・●■]\s)", re.M)

COMMANDS = {"/image": "image_gen", "/video": "video_gen", "/music": "music_gen", "/web": "research",
            "/code": "coding", "/agent": "project", "/fast": None, "/deep": None}


@dataclass
class TaskAnalysis:
    task_type: str = "chat"
    complexity: float = 0.2
    needs_web: bool = False
    needs_code_exec: bool = False
    needs_files: bool = False
    needs_vision: bool = False
    multi_step: bool = False
    memory_op: bool = False
    explicit_mode: str = "auto"
    explicit_command: str | None = None
    language: str = "ja"
    input_tokens: int = 0
    attachments: int = 0
    urls: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    text: str = ""
    deep_research: bool = False
    autonomous: bool = False

    @property
    def capability(self) -> str:
        return {"coding": "coding", "project": "coding", "reasoning": "reasoning", "research": "tools",
                "vision": "vision", "writing": "writing", "translation": "japanese",
                "file_analysis": "reasoning"}.get(self.task_type, "chat")

    @property
    def is_media(self) -> bool:
        return self.task_type in ("image_gen", "video_gen", "music_gen")

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("text", None)
        return d


def analyze(text: str, *, attachments: list[dict] | None = None, history_turns: int = 0,
            mode: str = "auto") -> TaskAnalysis:
    attachments = attachments or []
    raw = (text or "").strip()
    a = TaskAnalysis(explicit_mode=mode if mode in ("auto", "fast", "quality") else "auto")
    first = raw.split(maxsplit=1)[0].lower() if raw else ""
    if first in COMMANDS:
        a.explicit_command = first
        raw = raw[len(first):].strip()
        if first == "/fast":
            a.explicit_mode = "fast"
        elif first == "/deep":
            a.explicit_mode = "quality"
        elif COMMANDS[first]:
            a.task_type = COMMANDS[first]
            a.reasons.append(f"コマンド {first}")
    a.text = raw
    a.input_tokens = estimate_tokens(raw) + sum(int(x.get("tokens", 0)) for x in attachments)
    a.attachments = len(attachments)
    a.language = "ja" if re.search(r"[぀-ヿ一-鿿]", raw) else "en"
    a.urls = R_URL.findall(raw)[:5]
    images = [x for x in attachments if str(x.get("mime", "")).startswith("image/")]
    docs = [x for x in attachments if not str(x.get("mime", "")).startswith("image/")]
    a.needs_vision = bool(images)
    a.needs_files = bool(docs)
    a.memory_op = bool(R_MEMORY.search(raw))
    if a.explicit_mode == "auto":
        if R_QUALITY.search(raw):
            a.explicit_mode = "quality"
            a.reasons.append("品質指定キーワード")
        elif R_FAST.search(raw):
            a.explicit_mode = "fast"
            a.reasons.append("速度指定キーワード")

    is_code = bool(R_CODE.search(raw))
    is_reason = bool(R_REASON.search(raw))
    a.needs_web = bool(R_WEB.search(raw)) or bool(a.urls) or a.task_type == "research"
    a.needs_code_exec = bool(R_EXEC.search(raw)) and (is_code or is_reason)

    if a.task_type == "chat":
        for rx, kind in ((R_IMAGE, "image_gen"), (R_VIDEO, "video_gen"), (R_MUSIC, "music_gen")):
            if rx.search(raw) and not images:
                a.task_type = kind
                a.reasons.append(f"生成依頼を検出 ({kind})")
                break
    if a.task_type == "chat":
        if R_PROJECT.search(raw):
            a.task_type = "project"
        elif images:
            a.task_type = "vision"
        elif is_code:
            a.task_type = "coding"
        elif docs:
            a.task_type = "file_analysis"
        elif R_TRANSLATE.search(raw) and not a.urls:
            a.task_type = "translation"
            a.needs_web = False
        elif R_SUMMARY.search(raw) and not a.urls and not R_WEB.search(raw.replace("今日", "")):
            a.task_type = "summarize"
            a.needs_web = False
        elif a.needs_web:
            a.task_type = "research"
        elif is_reason:
            a.task_type = "reasoning"
        elif R_WRITING.search(raw):
            a.task_type = "writing"
        if a.task_type != "chat":
            a.reasons.append(f"タスク種別: {a.task_type}")

    reqs = len(R_REQ.findall(raw)) + len(re.findall(r"(そして|さらに|加えて|その上で|and also|additionally)", raw))
    a.multi_step = reqs >= 2 or a.task_type == "project"
    c = 0.15
    c += min(0.25, a.input_tokens / 2500 * 0.25)
    c += min(0.2, 0.07 * reqs)
    c += {"coding": 0.15, "project": 0.35, "reasoning": 0.18, "research": 0.12, "file_analysis": 0.12,
          "vision": 0.08, "writing": 0.06, "summarize": 0.04}.get(a.task_type, 0.0)
    c += min(0.15, 0.05 * a.attachments)
    if a.needs_code_exec:
        c += 0.08
    if history_turns > 12:
        c += 0.05
    if a.explicit_mode == "quality":
        c += 0.12
    elif a.explicit_mode == "fast":
        c -= 0.15
    if R_GREETING.match(raw) or (len(raw) < 12 and not attachments and "?" not in raw and "？" not in raw):
        c = 0.05
        a.reasons.append("短い雑談")
    a.complexity = round(clamp(c, 0.0, 1.0), 3)
    if mode in ("autonomous", "deep") and not a.is_media:
        # 自律特化: plan → research / execute → verify on its own, with long limits. For questions (not coding or
        # projects) that means a cited research report (Deep Research).
        a.autonomous, a.multi_step, a.explicit_mode = True, True, "quality"
        a.complexity = max(a.complexity, 0.9)
        if a.task_type in ("chat", "research", "summarize", "writing", "reasoning") or mode == "deep":
            a.deep_research, a.needs_web = True, True
            if a.task_type == "chat" or mode == "deep":
                a.task_type = "research"
        a.reasons.append("自律特化モード")
    return a
