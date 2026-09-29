#!/usr/bin/env python3
"""2.5단계 v0.4: 유료 단계 승인 게이트. 승인 대상 = bundle(대본+meta+음성+스타일 키+cap+smoke) 해시.
승인 파일 APPROVE_SCRIPT 는 '<이름> <해시 앞 8자리>' 형식이어야 하며 다른 묶음에는 재사용되지 않는다. 사용 후 approved/ 로 이동."""
import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.state import Episode, load_config  # noqa: E402
from lib.gate import bundle_hash, cap_of  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--lang", default="ko")
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    ep = Episode(a.topic, a.lang, smoke=a.smoke)
    pol = ep.read("policy_check.json")
    if pol.get("schema_errors") or not pol.get("ok"):
        print("정책/스키마 미통과 대본은 승인 대상이 아님"); sys.exit(2)
    lc = cfg["languages"][a.lang]
    if lc.get("voice_mode", "seed_audio") == "seed_audio" and not lc.get("voice_id"):
        print("voice_id 미선택 — 음성이 정해져야 승인 묶음을 만들 수 있음"); sys.exit(2)
    if not cfg["style"].get("style_key_job_id"):
        print("style_key_job_id 미선택 — `30_visuals.py --style-samples` 로 샘플 만든 뒤 config 에 고정"); sys.exit(2)

    h = bundle_hash(ep, cfg, a.lang)
    appr = ep._read("approval_script.json")
    if appr and appr.get("bundle_hash") == h:
        print(f"승인됨(동일 묶음 {h[:8]}) — 건너뜀"); return
    script = ep.read("script.json")
    n = len(script["blocks"])
    est = n * 2 + 1
    cap = cap_of(ep, cfg)
    pkg = {"episode": ep.id, "bundle_hash": h, "title": script["title"], "blocks": n, "voice_id": lc.get("voice_id"),
           "style_key": cfg["style"]["style_key_job_id"], "hf_calls_estimate": est, "cap": cap,
           "checklist": ["조언·전문가 행세 없음", "실존 인물 모방 없음", "수치 출처 확인(팩트체크 정정 반영)", "감정 조작 없음", "반복 없음", "예산 cap 이내"]}
    ep.write("approval_request.json", pkg)
    if est > cap:
        print(f"예산 초과: 예상 호출 {est} > cap {cap}"); sys.exit(2)
    flag = ep.dir / "APPROVE_SCRIPT"
    print(f"승인 요청 {ep.dir/'approval_request.json'} (묶음 {h[:8]})")
    print(f"승인: echo '<이름> {h[:8]}' > {flag}")
    if flag.exists():
        parts = flag.read_text().split()
        if len(parts) >= 2 and parts[0] and len(parts[1]) >= 8 and h.startswith(parts[1]):
            ep.write("approval_script.json", {"by": parts[0], "bundle_hash": h, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "cap": cap, "estimate": est})
            done = ep.dir / "approved"; done.mkdir(exist_ok=True)
            shutil.move(str(flag), str(done / f"APPROVE_SCRIPT_{h[:8]}"))
            ep.mark_done("approve_script", artifact={"bundle_hash": h})
            print(f"승인 기록 ({parts[0]}, {h[:8]})"); return
        print("APPROVE_SCRIPT 형식/해시 불일치 — 현재 묶음 해시로 다시 작성")
    sys.exit(3)


if __name__ == "__main__":
    main()
