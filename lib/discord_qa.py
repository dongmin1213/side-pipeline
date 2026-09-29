"""Discord QA 채널 (텔레그램 대체). 봇 토큰 + 채널 ID, REST 만 사용(게이트웨이 불필요).
- 검토본 요청: 메시지 + 첨부(프레임 jpg, 대본 json; 영상은 10MB 이하일 때만)
- 승인 폴링: GET /channels/{id}/messages?after=<요청 메시지 id> 에서 허용 user id 의 '/approve <ep> <hash8>' 만 인정
파일 한도: 부스트 없는 서버 봇 업로드 10MB (Discord 공식). 토큰은 헤더로만 전달(URL 노출 없음)."""
import time
from pathlib import Path

import requests

API = "https://discord.com/api/v10"
MAX_UPLOAD = 10 * 1024 * 1024


def _h(token):
    return {"Authorization": f"Bot {token}", "User-Agent": "side-content-engine (qa, 0.3)"}


def _req(method, token, path, **kw):
    for attempt in range(3):
        r = requests.request(method, f"{API}{path}", headers=_h(token), timeout=60, **kw)
        if r.status_code == 429:  # rate limit
            time.sleep(float(r.headers.get("Retry-After", "2")) + 0.5); continue
        if r.status_code >= 400:
            raise RuntimeError(f"discord {method} {path}: {r.status_code} {r.text[:200]}")
        return r.json() if r.text else {}
    raise RuntimeError(f"discord {method} {path}: rate limited 3x")


def post(token, channel_id, content, files=None):
    """files: [(filename, path)] — 10MB 초과는 건너뛰고 이름만 본문에 적는다."""
    multipart, skipped = [], []
    for i, (name, path) in enumerate(files or []):
        p = Path(path)
        if p.stat().st_size > MAX_UPLOAD:
            skipped.append(f"{name} ({p.stat().st_size // (1 << 20)}MB, 로컬: {p})"); continue
        multipart.append((f"files[{i}]", (name, open(p, "rb"))))
    if skipped:
        content += "\n첨부 생략(10MB 초과): " + "; ".join(skipped)
    data = {"payload_json": __import__("json").dumps({"content": content[:1900]})}
    msg = _req("POST", token, f"/channels/{channel_id}/messages", data=data, files=multipart or None)
    for _, f in multipart:
        f[1].close()
    return msg  # id, timestamp


def wait_decision(token, channel_id, after_message_id, episode_id, review_hash, allowed_user_ids, timeout_min, poll=5):
    """반환: {"decision": "approve"|"reject", "by": user_id, "message_id", "reason"} 또는 None(타임아웃)."""
    allowed = {str(u) for u in allowed_user_ids}
    last = after_message_id
    deadline = time.time() + timeout_min * 60
    while time.time() < deadline:
        msgs = _req("GET", token, f"/channels/{channel_id}/messages", params={"after": last, "limit": 50})
        for m in sorted(msgs, key=lambda x: int(x["id"])):
            last = m["id"]
            uid = str((m.get("author") or {}).get("id"))
            parts = (m.get("content") or "").split()
            if uid not in allowed or len(parts) < 2 or parts[1] != episode_id:
                continue
            if parts[0] == "/approve" and len(parts) >= 3 and review_hash.startswith(parts[2]):
                return {"decision": "approve", "by": uid, "message_id": m["id"]}
            if parts[0] == "/reject":
                return {"decision": "reject", "by": uid, "message_id": m["id"], "reason": " ".join(parts[2:])}
        time.sleep(poll)
    return None
