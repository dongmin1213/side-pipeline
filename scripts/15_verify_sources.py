#!/usr/bin/env python3
"""1.5단계: facts.json 의 모든 출처 URL 을 실제로 열어 live/blocked/dead 로 분류. dead 출처에만 기대는 수치·사건은 제거.
결과 source_report.json. 살아있는(또는 blocked) 출처가 min_sources 미만이면 exit 1."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.state import Episode, load_config  # noqa: E402
from lib.sources import apply_to_facts  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--lang", default="ko")
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    ep = Episode(a.topic, a.lang, smoke=a.smoke)
    facts = ep.read("facts.json")
    facts, report = apply_to_facts(facts)
    ep.write("facts.json", facts)
    ep.write("source_report.json", report)
    alive = len(facts["sources"])
    print(f"출처 {report['checked']}개 검사: live {report['live']} / blocked {report['blocked']}(사람 확인) / dead {report['dead']} → 제거된 수치 {len(report['dropped_figures'])}, 사건 {len(report['dropped_timeline'])}")
    for u in report["dead_urls"]:
        print("  dead:", u)
    if alive < cfg["research"]["min_sources"] or len(facts["figures"]) < 8:
        print("검증 후 자료 부족 — 중단"); sys.exit(1)
    ep.mark_done("verify_sources", artifact={"sources": facts["sources"]})


if __name__ == "__main__":
    main()
