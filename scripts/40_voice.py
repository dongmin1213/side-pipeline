#!/usr/bin/env python3
"""4단계 v0.4: 승인 묶음 확인 → cap 예약 → 제출(ID 즉시 저장) → 대기 → ffprobe 길이 검증(None 이면 실패).
재실행 시 pending job 은 재접속(resume), 새로 만들지 않음. 다른 묶음의 voice.json 은 stale 처리."""
import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.state import Episode, load_config  # noqa: E402
from lib import hf  # noqa: E402
from lib.gate import require_approval, reserve_call, stale_check  # noqa: E402


def duration_of(url):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", url], capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--lang", default="ko")
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    lc = cfg["languages"][a.lang]
    ep = Episode(a.topic, a.lang, smoke=a.smoke)
    h = require_approval(ep, cfg, a.lang)
    script = ep.read("script.json")
    bs = cfg["episode"]["block_seconds"]
    voice = stale_check(ep, "voice.json", h) or {"bundle_hash": h, "takes": {}}
    if lc.get("voice_mode", "seed_audio") == "human":
        print("EN 사람 음성 경로는 v0.4 미구현(I10) — KR smoke 범위 밖"); sys.exit(2)

    for b in script["blocks"]:
        n = str(b["n"]); t = voice["takes"].get(n, {})
        if t.get("status") == "succeeded":
            continue
        try:
            if t.get("job_id") and t.get("status") in ("pending", "running"):
                jid, url = hf.resume(t["job_id"], ep, "voice", kind="audio")
            else:
                reserve_call(ep, cfg)
                voice["takes"][n] = {"status": "pending"}; ep.write("voice.json", voice)
                jid, url, _ = hf.generate_voice(b["narration"], lc["voice_id"], lc.get("voice_type", "preset"), b["n"], lc.get("speech_rate"), ep)
        except hf.HFPending as e:
            voice["takes"][n] = {"status": "pending", "job_id": e.job_id}; ep.write("voice.json", voice)
            print(f"블록 {n}: job {e.job_id} 미완료 — 재실행 시 재접속"); sys.exit(2)
        except hf.HFError as e:
            voice["takes"][n] = {"status": "failed", "error": str(e)[:300]}; ep.write("voice.json", voice)
            print(f"블록 {n} 실패: {e} — 중단"); sys.exit(2)
        d = duration_of(url)
        if d is None:
            voice["takes"][n] = {"job_id": jid, "url": url, "status": "probe_failed"}; ep.write("voice.json", voice)
            print(f"블록 {n}: ffprobe 실패 — 검증 불가, 중단"); sys.exit(2)
        status = "succeeded" if d <= bs + 0.5 else "too_long"
        voice["takes"][n] = {"job_id": jid, "url": url, "mode": "seed_audio", "duration": d, "status": status}
        ep.write("voice.json", voice)
        print(f"블록 {n}: {d}s {'' if status == 'succeeded' else '← 길이 초과: narration 축약 후 재생성'}")
    bad = [b["n"] for b in script["blocks"] if voice["takes"].get(str(b["n"]), {}).get("status") != "succeeded"]
    if bad:
        print(f"미완성/길이초과 블록: {bad}"); sys.exit(2)
    if len({t["job_id"] for t in voice["takes"].values()}) != len(voice["takes"]):
        print("음성 job id 중복"); sys.exit(2)
    ep.mark_done("voice", artifact={"bundle": h, "jobs": {k: v["job_id"] for k, v in voice["takes"].items()}})
    print(f"voice.json: {len(voice['takes'])} 테이크 완료")


if __name__ == "__main__":
    main()
