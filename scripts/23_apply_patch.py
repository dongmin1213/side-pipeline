#!/usr/bin/env python3
"""2.3단계: 리뷰어가 제시한 결정적 패치(config/patches/<episode>-vN.json)를 script.json 에 적용. LLM 호출 없음.
- reorder.map: {새 n: 원본 n} — 씬 프롬프트·fact_refs 를 원본 블록에서 가져온 뒤 번호를 새 n 으로
- blocks[n]: narration(필수) / fact_refs / scene_prompt / fact_refs_from_timeline_date(facts.timeline 의 날짜로 source 조회)
- title / description_replace
적용 후 정책·스키마 재검사 → policy_check.json 갱신, script 해시 변경으로 하류 무효화."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.state import Episode, load_config, find_topic, ROOT  # noqa: E402
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("s20", Path(__file__).with_name("20_script.py")); s20 = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(s20)
from lib.policy import check_script  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--lang", default="ko")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--patch", required=True, help="config/patches/*.json")
    a = ap.parse_args()
    cfg = load_config()
    ep = Episode(a.topic, a.lang, smoke=a.smoke)
    script, meta, facts = ep.read("script.json"), ep.read("meta.json"), ep.read("facts.json")
    patch = json.loads((ROOT / a.patch).read_text(encoding="utf-8")) if not Path(a.patch).is_absolute() else json.loads(Path(a.patch).read_text(encoding="utf-8"))
    orig = {b["n"]: dict(b) for b in script["blocks"]}
    new_blocks = {n: dict(b) for n, b in orig.items()}

    # 1) reorder (원본 블록 통째로 이동)
    for new_n, src_n in (patch.get("reorder") or {}).get("map", {}).items():
        b = dict(orig[int(src_n)]); b["n"] = int(new_n); new_blocks[int(new_n)] = b

    # 2) narration/refs/scene overrides
    date_src = {t.get("date"): t.get("source") for t in facts.get("timeline", []) if t.get("source")}
    for n, ov in patch.get("blocks", {}).items():
        b = new_blocks[int(n)]
        if ov.get("narration"):
            b["narration"] = ov["narration"]
        if ov.get("fact_refs"):
            b["fact_refs"] = ov["fact_refs"]
        if ov.get("fact_refs_from_timeline_date"):
            src = date_src.get(ov["fact_refs_from_timeline_date"])
            if src:
                b["fact_refs"] = [src]
        if ov.get("scene_prompt"):
            b["scene_prompt"] = ov["scene_prompt"]
        b["patched"] = a.patch.split("/")[-1]
    script["blocks"] = [new_blocks[n] for n in sorted(new_blocks)]
    for i, b in enumerate(script["blocks"], 1):
        b["n"] = i

    # 3) title/description
    if patch.get("title"):
        script["title"] = patch["title"]
    if patch.get("shorts_block_range"):
        script["shorts_block_range"] = patch["shorts_block_range"]
    desc = script.get("description", "")
    for old, new in patch.get("description_replace", []):
        desc = desc.replace(old, new)
    if patch.get("description"):  # 전체 교체 (출처 목록은 기존 것을 뒤에 유지)
        tail = desc[desc.find("출처:"):] if "출처:" in desc else (desc[desc.find("Sources:"):] if "Sources:" in desc else "")
        desc = patch["description"].rstrip() + ("\n\n" + tail if tail else "")
    script["description"] = desc

    # 4) 재검사
    meta = {"title": script["title"], "description": script["description"], "tags": script.get("tags", []), "sources": script.get("sources", []),
            "shorts_block_range": script.get("shorts_block_range")}
    report = check_script(script["blocks"], meta, cfg, people_names=cfg.get("policy", {}).get("real_person_names") or [])
    # 언어별 스키마 검사는 20_script 와 동일 규칙(EN: 발화 14~26단어·숫자≤2·한글 금지·필수 문구 / KO: 48~70자)
    topic = find_topic(a.topic, a.lang)
    bs = cfg["episode"]["block_seconds"]
    n_blocks = cfg["smoke"]["blocks"] if a.smoke else cfg["episode"]["duration_minutes"] * 60 // bs
    schema_errs = s20.validate_schema(script, n_blocks, bs, cfg["episode"]["shorts"]["seconds"], a.lang, topic.get("required_phrases") or [])
    lens = [s20.spoken_words_en(b["narration"]) if a.lang == "en" else len(b["narration"]) for b in script["blocks"]]
    ep.write("script.json", script); ep.write("meta.json", meta)
    ep.write("policy_check.json", {"schema_errors": schema_errs, **report, "patched_with": a.patch})
    if schema_errs or not report["ok"]:
        print(f"패치 후 검사 실패: {schema_errs[:5]} {report['issues'][:5]}"); sys.exit(2)
    ep.mark_done("script", artifact=script)  # 해시 변경 → approve/voice/visuals 무효화
    print(f"패치 적용: 블록 {len(patch.get('blocks', {}))}개 교체, 재배열 {len((patch.get('reorder') or {}).get('map', {}))}개, 길이 {min(lens)}~{max(lens)}{'단어' if a.lang == 'en' else '자'}, 고유 수치 {report['unique_numbers']}, 정책 통과")


if __name__ == "__main__":
    main()
