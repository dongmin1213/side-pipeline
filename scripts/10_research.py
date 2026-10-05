#!/usr/bin/env python3
"""1단계 v0.2: 실제 원문을 읽는 리서치. Codex 백엔드 + live web_search 로 출처를 직접 수집하고,
source URL 이 없는 수치는 facts 에서 제거한다(I1). 검증된 facts 가 최소 기준 미만이면 exit 1."""
import argparse
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.state import Episode, load_config, find_topic  # noqa: E402
from lib.llm import ask_json  # noqa: E402

SYSTEM = """당신은 기업 재무 리서처다. 웹 검색·페이지 열람 도구로 **원문을 직접 읽고** 검증 가능한 사실만 정리한다.
규칙:
1. 모든 수치·날짜·사건에 source(실제로 열어본 페이지 URL)와 date(사건 날짜 또는 문서 날짜)를 붙인다. 열어보지 못한 URL 은 쓰지 않는다.
2. 1차 출처 우선: 공시(DART / SEC EDGAR 10-K·10-Q·8-K, 기업 IR 실적 발표), 판결문, 정부·공공기관 자료, 당시 주요 언론 기사. 블로그·위키는 2차로 표시(kind: "secondary").
   영어 주제면 SEC EDGAR 원문(sec.gov) 과 회사 IR 페이지를 반드시 포함한다.
3. 투자 조언·전망·평가어 금지. 실존 인물은 공적 직함과 공개된 행위만.
4. 서로 다른 출처가 수치를 다르게 말하면 both 를 기록하고 conflict: true.
출력은 JSON 하나:
{"company": "...", "timeline": [{"date","event","source","kind"}], "figures": [{"label","value","unit","date","source","kind","conflict"}],
 "angle_evidence": ["각도를 뒷받침하는 사실 문장 (수치 포함)"], "open_questions": ["확인 못 한 것"],
 "sources": [{"url","title","date","kind"}]}"""


def _ok_url(u):
    p = urlparse(str(u or ""))
    return p.scheme in ("http", "https") and bool(p.netloc)


def verify(facts, cfg):
    """source 가 URL 이 아닌 수치·사건은 제거. sources 는 URL 중복 제거."""
    facts["figures"] = [f for f in facts.get("figures", []) if _ok_url(f.get("source"))]
    facts["timeline"] = [t for t in facts.get("timeline", []) if _ok_url(t.get("source"))]
    seen, srcs = set(), []
    for s in facts.get("sources", []):
        if _ok_url(s.get("url")) and s["url"] not in seen:
            seen.add(s["url"]); srcs.append(s)
    facts["sources"] = srcs
    return facts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--lang", default="ko")
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    topic = find_topic(a.topic, a.lang)
    ep = Episode(a.topic, a.lang, smoke=a.smoke)
    if ep.is_done("research"):
        print("research 완료됨 — 건너뜀"); return

    user = f"""회사: {topic['company']}
이 편의 재무 각도: {topic['angle']}
검색 키워드: {topic.get('keywords')}
언어: {a.lang}
요구: figures 최소 15개(매출·부채·인수가·점유율·시점 등), timeline 최소 10개, sources 최소 {cfg['research']['min_sources']}개(1차 출처 2개 이상).
반드시 도구로 페이지를 열어 확인한 뒤 JSON 만 출력하라."""
    t0 = time.time()
    facts, meta = ask_json(SYSTEM, user, web_search=cfg["research"]["sources"].get("web", True),
                           ledger=ep, stage="research_llm")
    facts = verify(facts, cfg)
    facts["_meta"] = {"topic": topic, "seconds": round(time.time() - t0, 1), "llm": meta}
    ep.write("facts.json", facts)
    n_f, n_s = len(facts["figures"]), len(facts["sources"])
    primary = sum(1 for s in facts["sources"] if s.get("kind") == "primary")
    print(f"facts.json: figures {n_f}, timeline {len(facts['timeline'])}, sources {n_s} (1차 {primary}), {facts['_meta']['seconds']}s, 웹검색 {meta.get('web_searches')}회")
    if n_s < cfg["research"]["min_sources"] or n_f < 8:
        print("검증된 자료 부족 — 대본 단계로 넘어가지 않음 (사람이 sources 를 보강하거나 키워드를 바꿔 재실행)")
        sys.exit(1)
    ep.mark_done("research", artifact={"figures": facts["figures"], "sources": facts["sources"]})


if __name__ == "__main__":
    main()
