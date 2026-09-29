#!/usr/bin/env python3
"""6단계 v0.2: 사람 QA (C2 반영). 검토본 해시(final.mp4 + script)를 승인과 묶는다.
local 모드: episodes/<id>/APPROVE_QA 에 '<이름> <해시 앞 8자리>' 를 적으면 승인.
telegram 모드: allowed_user_ids 의 사용자가 '/approve <id> <hash8>' 를 요청 이후 시각에 보낸 경우만 승인. 응답 ok 검증, durable offset."""
import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.state import Episode, load_config, env, sha  # noqa: E402

API = "https://api.telegram.org/bot{token}/{method}"


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()[:16]


def tg(token, method, files=None, **data):
    r = requests.post(API.format(token=token, method=method), data=data, files=files, timeout=120)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"telegram {method}: {j.get('description')}")
    return j["result"]


def frames(video, ep):
    out = []
    for t in (5, 30, 60):
        p = ep.dir / f"qa_frame_{t}.jpg"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i", str(video), "-frames:v", "1", "-vf", "scale=640:-1", str(p)])
        if p.exists():
            out.append(p)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--lang", default="ko")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--timeout-min", type=int, default=24 * 60)
    a = ap.parse_args()
    cfg = load_config()
    ep = Episode(a.topic, a.lang, smoke=a.smoke)
    meta, pol, outputs, script = ep.read("meta.json"), ep.read("policy_check.json"), ep.read("outputs.json"), ep.read("script.json")
    review_hash = sha({"video": file_hash(outputs["final"]), "script": sha(script), "short": file_hash(outputs["short"]) if outputs.get("short") else None})
    if ep.is_done("qa") and ep.state["done"]["qa"] == sha({"hash": review_hash}):
        print("qa 완료됨(같은 검토본) — 건너뜀"); return
    if not pol.get("ok"):
        ep.write("qa.json", {"decision": "reject", "by": "policy_auto", "review_hash": review_hash, "issues": pol["issues"]}); sys.exit(2)

    checklist = ["①조언·전문가 행세 없음", "②실존 인물 외모·음성 모방 없음", "③수치·출처 원문 대조", "④감정 조작 연출 없음", "⑤반복·AI 티 문장 없음", "⑥롱폼·숏폼 둘 다 확인"]
    pkg = {"episode": ep.id, "review_hash": review_hash, "title": meta["title"], "numbers": pol.get("unique_numbers"),
           "sources": meta.get("sources"), "outputs": outputs, "checklist": checklist, "requested_at": time.time()}
    ep.write("qa_request.json", pkg)
    print(json.dumps({k: pkg[k] for k in ("episode", "review_hash", "title", "numbers")}, ensure_ascii=False))

    if cfg["qa"]["mode"] == "local":
        flag = ep.dir / "APPROVE_QA"
        print(f"검토 후 승인: echo '<이름> {review_hash[:8]}' > {flag}   (반려: REJECT_QA 파일에 사유)")
        rej = ep.dir / "REJECT_QA"
        if rej.exists():
            ep.write("qa.json", {"decision": "reject", "by": "human", "review_hash": review_hash, "reason": rej.read_text().strip()}); sys.exit(2)
        if flag.exists():
            parts = flag.read_text().split()
            if len(parts) >= 2 and review_hash.startswith(parts[1]):
                ep.write("qa.json", {"decision": "approve", "by": parts[0], "review_hash": review_hash, "checklist": checklist, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
                ep.mark_done("qa", artifact={"hash": review_hash}); print("승인 기록됨"); return
            print("APPROVE_QA 의 해시가 현재 검토본과 다름 — 재검토 필요")
        sys.exit(3)

    if cfg["qa"]["mode"] == "discord":
        from lib.discord_qa import post, wait_decision
        token, channel = env("DISCORD_BOT_TOKEN"), env("DISCORD_CHANNEL_ID")
        allowed = cfg["qa"].get("discord_allowed_user_ids") or []
        if not allowed:
            print("qa.discord_allowed_user_ids 비어 있음"); sys.exit(2)
        text = (f"**[{ep.id}] QA 요청** (검토본 `{review_hash[:8]}`)\n제목: {meta['title']}\n고유 수치 {pol.get('unique_numbers')} / 출처 {len(meta.get('sources', []))}\n"
                + "\n".join(checklist) + f"\n승인: `/approve {ep.id} {review_hash[:8]}`\n반려: `/reject {ep.id} <사유>`")
        files = [(f"{ep.id}_script.json", ep.dir / "script.json")] + [(p.name, p) for p in frames(outputs["final"], ep)]
        for key in ("final", "short"):
            if outputs.get(key):
                files.append((f"{ep.id}_{key}.mp4", outputs[key]))
        sent = post(token, channel, text, files)
        print("Discord 검토 요청 전송, 승인 대기 중…")
        d = wait_decision(token, channel, sent["id"], ep.id, review_hash, allowed, a.timeout_min)
        if d is None:
            print("QA 타임아웃"); sys.exit(3)
        if d["decision"] == "approve":
            ep.write("qa.json", {**d, "review_hash": review_hash, "checklist": checklist, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
            ep.mark_done("qa", artifact={"hash": review_hash}); post(token, channel, f"[{ep.id}] 승인 기록됨 → 업로드 진행"); return
        ep.write("qa.json", {**d, "review_hash": review_hash}); post(token, channel, f"[{ep.id}] 반려 기록됨: {d.get('reason')}"); sys.exit(2)

    # telegram
    token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    allowed = set(cfg["qa"].get("telegram_allowed_user_ids") or [])
    if not allowed:
        print("telegram_allowed_user_ids 비어 있음"); sys.exit(2)
    text = (f"[{ep.id}] QA 요청 (검토본 {review_hash[:8]})\n제목: {meta['title']}\n고유 수치 {pol.get('unique_numbers')} / 출처 {len(meta.get('sources', []))}\n"
            + "\n".join(checklist) + f"\n승인: /approve {ep.id} {review_hash[:8]}\n반려: /reject {ep.id} <사유>")
    sent = tg(token, "sendMessage", chat_id=chat, text=text)
    tg(token, "sendDocument", files={"document": (f"{ep.id}_script.json", json.dumps(script, ensure_ascii=False).encode())}, chat_id=chat)
    for p in frames(outputs["final"], ep):
        with open(p, "rb") as f:
            tg(token, "sendPhoto", files={"photo": f}, chat_id=chat)
    for key in ("final", "short"):
        v = outputs.get(key)
        if v and Path(v).stat().st_size < 50 * 1024 * 1024:
            with open(v, "rb") as f:
                tg(token, "sendVideo", files={"video": f}, chat_id=chat, caption=f"{ep.id} {key}")
        elif v:
            tg(token, "sendMessage", chat_id=chat, text=f"{key} 50MB 초과 — 로컬: {v}")
    cursor_file = ep.dir.parent / ".telegram_offset"
    offset = int(cursor_file.read_text()) if cursor_file.exists() else 0
    deadline, req_ts = time.time() + a.timeout_min * 60, sent["date"]
    while time.time() < deadline:
        upd = requests.get(API.format(token=token, method="getUpdates"), params={"timeout": 50, "offset": offset}, timeout=70).json()
        for u in upd.get("result", []):
            offset = u["update_id"] + 1; cursor_file.write_text(str(offset))
            m = u.get("message") or {}
            uid, txt = (m.get("from") or {}).get("id"), (m.get("text") or "").split()
            if uid not in allowed or m.get("date", 0) < req_ts or len(txt) < 2 or txt[1] != ep.id:
                continue
            if txt[0] == "/approve" and len(txt) >= 3 and review_hash.startswith(txt[2]):
                ep.write("qa.json", {"decision": "approve", "by": uid, "review_hash": review_hash, "checklist": checklist, "message_id": m.get("message_id"), "ts": m["date"]})
                ep.mark_done("qa", artifact={"hash": review_hash}); tg(token, "sendMessage", chat_id=chat, text=f"[{ep.id}] 승인 기록됨"); return
            if txt[0] == "/reject":
                ep.write("qa.json", {"decision": "reject", "by": uid, "review_hash": review_hash, "reason": " ".join(txt[2:])}); sys.exit(2)
    print("QA 타임아웃"); sys.exit(3)


if __name__ == "__main__":
    main()
