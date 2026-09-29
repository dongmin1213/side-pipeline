#!/usr/bin/env python3
"""2.2단계: 사람/리뷰어 팩트체크 결과를 facts·대본에 반영.
--corrections <json>: [{"pattern": 정규식, "note": 정정 내용}] — facts 의 해당 항목에 correction 을 붙이고(삭제는 안 함),
                      script_parts 의 청크 중 pattern 에 걸리는 블록이 있으면 그 청크만 정정 내용을 넣어 재작성한다.
기본 corrections: config/corrections/<topic>.json"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.state import Episode, load_config, find_topic, ROOT  # noqa: E402
from lib.llm import ask_json  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--lang", default="ko")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--corrections")
    ap.add_argument("--facts-only", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    topic = find_topic(a.topic, a.lang)
    ep = Episode(a.topic, a.lang, smoke=a.smoke)
    cpath = Path(a.corrections) if a.corrections else ROOT / "config" / "corrections" / f"{a.topic}.json"
    corrections = json.loads(cpath.read_text(encoding="utf-8"))
    facts = ep.read("facts.json")

    # 1) facts 에 정정 주석
    hit_f = 0
    for c in corrections:
        rx = re.compile(c["pattern"])
        for coll in ("figures", "timeline", "angle_evidence"):
            items = facts.get(coll) or []
            for i, it in enumerate(items):
                text = json.dumps(it, ensure_ascii=False) if not isinstance(it, str) else it
                if rx.search(text):
                    hit_f += 1
                    if isinstance(it, dict):
                        it.setdefault("corrections", []).append(c["note"])
                    else:
                        items[i] = f"{it} [정정: {c['note']}]"
    facts.setdefault("corrections_applied", []).extend([c["note"] for c in corrections])
    ep.write("facts.json", facts)
    ep.write("facts_corrections.json", {"applied": corrections, "facts_items_annotated": hit_f})
    print(f"facts 주석 {hit_f}건")
    if a.facts_only:
        return

    # 2) 대본 청크 재작성 (걸리는 블록이 있는 청크만)
    parts = ep._read("script_parts.json") or {}
    if not parts:
        print("script_parts.json 없음 — 대본 생성 후 재실행"); return
    style, neg = cfg["style"]["style_descriptor"], cfg["style"]["negative"]
    from importlib import import_module
    BLOCK_SYS = import_module("20_script").BLOCK_SYS if False else None  # (직접 import 불가: 파일명 숫자) → 아래 재정의
    BLOCK_SYS = """당신은 얼굴 없는 데이터 다큐 채널의 작가다. 조언·전문가 행세 금지. 검증된 facts 의 사실과 숫자만 사용.
블록 = 정확히 10초(한국어 45~55자). 각 블록 fact_refs(출처 URL). 씬 프롬프트는 영어, {STYLE} 포함, 실존 인물 얼굴·로고·화면 텍스트 금지.
출력 JSON: {"blocks": [{"n": <번호>, "narration": "...", "scene_prompt": "...", "fact_refs": ["url"]}]}"""
    rewritten = []
    for key, blocks in list(parts.items()):
        if not key.startswith("blocks_") or not isinstance(blocks, list):
            continue
        hits = [(b["n"], c["note"]) for b in blocks for c in corrections if re.search(c["pattern"], b.get("narration", "") + " " + b.get("scene_prompt", ""))]
        if not hits:
            continue
        lo, hi = key.split("_")[1:]
        user = f"""회사: {topic['company']} / 언어: {a.lang}
이 구간(블록 {lo}~{hi})에 사실 오류가 있다. 아래 정정 사항을 반영해 **같은 번호·같은 개수**로 다시 써라. 정정과 무관한 블록은 최대한 유지.
정정 사항(블록번호, 내용): {hits}
전체 정정 목록: {[c['note'] for c in corrections]}
{{STYLE}} = "{style}" / NEGATIVE = "{neg}"
기존 블록: {json.dumps(blocks, ensure_ascii=False)}
facts(정정 주석 포함): {json.dumps({k: facts.get(k) for k in ('figures', 'timeline', 'angle_evidence', 'sources')}, ensure_ascii=False)}"""
        part, _ = ask_json(BLOCK_SYS, user, ledger=ep, stage=f"patch_{key}", timeout=600)
        new_blocks = part.get("blocks") or []
        if len(new_blocks) != len(blocks) or [b.get("n") for b in new_blocks] != [b.get("n") for b in blocks]:
            print(f"{key}: 재작성 결과 블록 수/번호 불일치 — 원본 유지"); continue
        parts[key] = new_blocks; rewritten.append(key)
        ep.write("script_parts.json", parts)
    # script.json 완료 표시는 지워서 20_script 가 재조립·재검사하도록
    ep.state["done"].pop("script", None); ep.write("state.json", ep.state)
    print(f"재작성 청크: {rewritten or '없음'} → 20_script.py 를 다시 실행해 재검사")


if __name__ == "__main__":
    main()
