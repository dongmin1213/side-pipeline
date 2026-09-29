#!/usr/bin/env python3
"""실행기 v0.2 (C1): 환경 점검 강제, 순서 = 리서치 → 대본(정책) → **사람 승인** → 음성 → 클립 → 조립 → QA → 업로드.
--smoke: 2블록 계약 테스트 (리뷰 권장 첫 유료 실행). --until N 으로 무비용 구간(1~2)만 실행 가능."""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STAGES = [
    (1, "scripts/10_research.py", False),
    (1.5, "scripts/15_verify_sources.py", False),
    (2, "scripts/20_script.py", False),
    (3, "scripts/25_approve_script.py", False),
    (4, "scripts/40_voice.py", True),
    (5, "scripts/30_visuals.py", True),
    (6, "scripts/50_assemble.py", True),
    (7, "scripts/60_qa.py", False),
    (8, "scripts/70_publish.py", False),
]


def run(script, args):
    return subprocess.run([sys.executable, str(ROOT / script), *args]).returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--lang", default="ko")
    ap.add_argument("--from", dest="start", type=float, default=1)
    ap.add_argument("--until", type=float, default=8)
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args()
    common = ["--topic", a.topic, "--lang", a.lang] + (["--smoke"] if a.smoke else [])
    paid = any(p for n, _, p in STAGES if a.start <= n <= a.until)
    env_args = (["--paid"] if paid else []) + (["--publish"] if a.until >= 8 else [])
    if run("scripts/00_check_env.py", env_args) != 0:
        print("환경 점검 실패 — 중단"); sys.exit(1)
    for n, script, _ in STAGES:
        if n < a.start or n > a.until:
            continue
        print(f"\n=== [{n}] {script} {'(smoke)' if a.smoke else ''} ===")
        rc = run(script, common)
        if rc != 0:
            print(f"단계 {n} 종료코드 {rc} — 중단"); sys.exit(rc)
    print("\n완료")


if __name__ == "__main__":
    main()
