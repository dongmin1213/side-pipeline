#!/usr/bin/env python3
"""7단계 v0.2 (I13~I16): private_only 고정 옵션, 토큰 refresh, 자산별 체크포인트(재실행 시 중복 업로드 방지),
UTF-8 바이트 제한·태그 총길이·금지문자 검증, qa.json 의 review_hash 와 현재 산출물 대조."""
import argparse
import hashlib
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.state import Episode, load_config, ROOT, sha  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube"]


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()[:16]


def youtube_client():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    secret = ROOT / os.environ.get("YT_CLIENT_SECRET_JSON", "config/yt_client_secret.json")
    token = secret.with_name("yt_token.json")
    creds = Credentials.from_authorized_user_file(str(token), SCOPES) if token.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds or not creds.valid:
        creds = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES).run_local_server(port=0)
    token.write_text(creds.to_json()); os.chmod(token, 0o600)
    return build("youtube", "v3", credentials=creds)


def clamp_meta(title, description, tags, sources, lang="ko"):
    """YouTube 한도: 제목 100자, 설명 5000바이트, 태그 합계 500자. 출처 목록은 URL 단위로만 자른다(중간 절단 금지)."""
    bad = "<>"
    title = "".join(c for c in title if c not in bad)[:100]
    label = "Sources:" if lang == "en" else "출처:"
    body = re.split(r"\n?\s*(?:출처|Sources):", description)[0].rstrip()
    urls = list(dict.fromkeys(s.get("url", "") for s in sources if s.get("url")))
    src_lines, used = [], len(f"\n\n{label}".encode("utf-8"))
    body_budget = 5000 - min(len(body.encode("utf-8")), 2500)  # 본문 최대 2500바이트 보장, 나머지를 출처에
    for u in urls:
        n = len(("\n" + u).encode("utf-8"))
        if used + n > body_budget:
            break
        src_lines.append(u); used += n
    src_block = f"\n\n{label}\n" + "\n".join(src_lines) if src_lines else ""
    body = body.encode("utf-8")[:max(0, 5000 - len(src_block.encode("utf-8")))].decode("utf-8", "ignore")
    description = body + src_block
    out, total = [], 0
    for t in tags:
        t = "".join(c for c in t if c not in bad)
        if total + len(t) + 2 > 480:
            break
        out.append(t); total += len(t) + 2
    return title, description, out


def shorts_title(title, suffix=" #Shorts"):
    """접미사 포함 100자 이내. 넘치면 단어 경계에서 자른다."""
    room = 100 - len(suffix)
    if len(title) > room:
        title = title[:room].rsplit(" ", 1)[0].rstrip(" .,:;")
    return title + suffix


def upload(yt, path, title, description, tags, category_id):
    from googleapiclient.http import MediaFileUpload
    body = {"snippet": {"title": title, "description": description, "tags": tags, "categoryId": category_id},
            "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False}}
    req = yt.videos().insert(part="snippet,status", body=body, media_body=MediaFileUpload(path, chunksize=8 * 1024 * 1024, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--lang", default="ko")
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    ep = Episode(a.topic, a.lang, smoke=a.smoke)
    yc = cfg["publish"]["youtube"]
    if not yc.get("enabled"):
        print("publish.youtube.enabled=false — 업로드 안 함"); return
    qa, meta, outputs, script = ep.read("qa.json"), ep.read("meta.json"), ep.read("outputs.json"), ep.read("script.json")
    if qa.get("decision") != "approve":
        print("QA 미승인"); sys.exit(2)
    current = sha({"video": file_hash(outputs["final"]), "script": sha(script), "short": file_hash(outputs["short"]) if outputs.get("short") else None})
    if qa.get("review_hash") != current:
        print("승인된 검토본과 현재 산출물이 다름 — 재QA 필요"); sys.exit(2)

    pub = ep._read("publish.json") or {"assets": {}}
    yt = youtube_client()
    title, desc, tags = clamp_meta(meta["title"], meta["description"], list(dict.fromkeys(meta.get("tags", []) + yc.get("default_tags", []))), meta.get("sources", []), lang=a.lang)
    for key, t in (("final", title), ("short", shorts_title(title))):
        path = outputs.get(key)
        if not path or pub["assets"].get(key, {}).get("id"):
            continue
        resp = upload(yt, path, t, desc, tags, yc["category_id"])
        pub["assets"][key] = {"id": resp["id"], "url": f"https://youtu.be/{resp['id']}", "privacy": (resp.get("status") or {}).get("privacyStatus"),
                              "upload_status": (resp.get("status") or {}).get("uploadStatus"), "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        ep.write("publish.json", pub)  # 자산별 체크포인트
    pub["private_only"] = yc.get("private_only", True)
    pub["note"] = "미심사 API 프로젝트 업로드는 비공개 고정. 공개 전환은 심사 통과 + 별도 게시 승인 후 수동/후속 단계."
    ep.write("publish.json", pub)
    ep.mark_done("publish", artifact=pub["assets"])
    print(pub)


if __name__ == "__main__":
    main()
