"""유료 단계 공통 게이트 v0.4 (리뷰 11 blocker 2·3·4):
- approval bundle = sha(script + meta + voice_id + style_key + cap + smoke) — 이 해시가 approval_script.json 과 일치해야 유료 호출 가능
- cap 예약: 제출 전에 ledger 의 *_submitted 수 + 예약 1 ≤ cap 검사
- stale 자산: voice/visuals 에 저장된 bundle 해시가 현재와 다르면 재사용 금지(stale/ 로 이동)"""
import shutil
import sys
import time

from .state import sha


def bundle_hash(ep, cfg, lang):
    script, meta = ep.read("script.json"), ep.read("meta.json")
    lc = cfg["languages"][lang]
    cap = cfg["budget"]["max_hf_calls_smoke"] if ep.smoke else cfg["budget"]["max_hf_calls_per_episode"]
    return sha({"script": script, "meta": meta, "voice_id": lc.get("voice_id"), "voice_type": lc.get("voice_type"),
                "style_key": cfg["style"].get("style_key_job_id"), "cap": cap, "smoke": ep.smoke, "lang": lang})


def require_approval(ep, cfg, lang):
    h = bundle_hash(ep, cfg, lang)
    appr = ep._read("approval_script.json")
    if not appr or appr.get("bundle_hash") != h:
        print(f"유료 단계 차단: 현재 대본·음성·스타일·예산 묶음({h[:8]})에 대한 승인이 없음 → 25_approve_script.py 실행")
        sys.exit(2)
    return h


def cap_of(ep, cfg):
    return cfg["budget"]["max_hf_calls_smoke"] if ep.smoke else cfg["budget"]["max_hf_calls_per_episode"]


def calls_used(ep):
    return sum(1 for e in ep.ledger() if e["stage"].endswith("_submitted") or e["stage"].endswith("_submit_unknown"))


def reserve_call(ep, cfg):
    cap = cap_of(ep, cfg)
    used = calls_used(ep)
    if used + 1 > cap:
        print(f"예산 cap {cap} 초과(사용 {used}) — 제출 중단"); sys.exit(2)
    return used + 1


def stale_check(ep, name, current_hash):
    """voice.json/visuals.json 이 다른 bundle 로 만들어졌으면 stale/ 로 옮기고 None 반환."""
    data = ep._read(name)
    if data and data.get("bundle_hash") and data["bundle_hash"] != current_hash:
        st = ep.dir / "stale"; st.mkdir(exist_ok=True)
        shutil.move(str(ep.dir / name), str(st / f"{time.strftime('%Y%m%d%H%M%S')}_{name}"))
        ep.add_cost("stale_asset_moved", file=name, old_bundle=data["bundle_hash"], new_bundle=current_hash)
        return None
    return data
