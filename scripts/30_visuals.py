#!/usr/bin/env python3
"""3단계 v0.4: 스타일 키는 별도 승인 대상(--style-samples 로 샘플만 생성 → 사용자가 config 에 고정). 클립은 승인 묶음 + 음성 완료 후.
cap 예약 → 제출(ID 즉시 저장) → 대기. pending 은 resume, failed 는 새로 만들지 않고 중단."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.state import Episode, load_config  # noqa: E402
from lib import hf  # noqa: E402
from lib.gate import require_approval, reserve_call, stale_check  # noqa: E402


def style_samples(cfg, ep, n):
    """스타일 키 후보 n장. 승인 묶음과 무관하게 cap(smoke) 안에서 실행. 결과는 style_samples.json."""
    out = ep._read("style_samples.json") or {"samples": []}
    for i in range(n):
        reserve_call(ep, cfg)
        prompt = f"{cfg['style']['style_descriptor']}. Universal style reference frame for a data documentary series, variant {i + 1}: abstract editorial scene, no people, no text."
        jid, url, secs = hf.generate_image(cfg["style"]["image_model"], prompt, cfg["episode"]["aspect"], ep)
        out["samples"].append({"job_id": jid, "url": url, "seconds": secs}); ep.write("style_samples.json", out)
        print(f"샘플 {i + 1}: {jid} → {url}")
    print("마음에 드는 샘플의 job_id 를 config/pipeline.yaml style.style_key_job_id 에 고정한 뒤 25_approve_script.py 를 실행")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--lang", default="ko")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--style-samples", type=int, default=0)
    a = ap.parse_args()
    cfg = load_config()
    ep = Episode(a.topic, a.lang, smoke=a.smoke)
    if a.style_samples:
        style_samples(cfg, ep, a.style_samples); return
    h = require_approval(ep, cfg, a.lang)
    if not ep.is_done("voice"):
        print("voice 미완료 — 스킬 규칙상 음성 전량 완료 후 클립 생성"); sys.exit(2)
    key = cfg["style"].get("style_key_job_id")
    script = ep.read("script.json")
    vis = stale_check(ep, "visuals.json", h) or {"bundle_hash": h, "style_key_id": key, "clips": {}}

    for b in script["blocks"]:
        n = str(b["n"]); c = vis["clips"].get(n, {})
        if c.get("status") == "succeeded":
            continue
        try:
            if c.get("job_id") and c.get("status") in ("pending", "running"):
                jid, url = hf.resume(c["job_id"], ep, "clip", kind="video")
            else:
                reserve_call(ep, cfg)
                vis["clips"][n] = {"status": "pending"}; ep.write("visuals.json", vis)
                jid, url, _ = hf.generate_clip(cfg["style"]["video_model"], b["scene_prompt"], key, b["n"], cfg["episode"]["block_seconds"],
                                               cfg["episode"]["resolution"], cfg["episode"]["aspect"], ep)
        except hf.HFPending as e:
            vis["clips"][n] = {"status": "pending", "job_id": e.job_id}; ep.write("visuals.json", vis)
            print(f"블록 {n}: job {e.job_id} 미완료 — 재실행 시 재접속"); sys.exit(2)
        except hf.HFError as e:
            vis["clips"][n] = {"status": "failed", "error": str(e)[:300]}; ep.write("visuals.json", vis)
            print(f"블록 {n} 실패: {e} — 중단(자동 재생성 없음)"); sys.exit(2)
        vis["clips"][n] = {"job_id": jid, "url": url, "status": "succeeded"}; ep.write("visuals.json", vis)
        print(f"블록 {n} 완료")
    missing = [b["n"] for b in script["blocks"] if vis["clips"].get(str(b["n"]), {}).get("status") != "succeeded"]
    if missing:
        print(f"미완성 블록: {missing}"); sys.exit(2)
    ep.mark_done("visuals", artifact={"bundle": h, "jobs": {k: v["job_id"] for k, v in vis["clips"].items()}})
    print(f"visuals.json: 클립 {len(vis['clips'])}개 완료")


if __name__ == "__main__":
    main()
