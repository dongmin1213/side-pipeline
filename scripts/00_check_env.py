#!/usr/bin/env python3
"""0단계: 환경 점검. --paid 이면 유료 단계 요건(Higgsfield 워크스페이스·음성 ID·스타일 키)까지 검사. 실패 시 exit 1."""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.state import load_config, ROOT  # noqa: E402
from lib import hf  # noqa: E402


def ffmpeg_has(filter_name):
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    return any(line.split()[1:2] == [filter_name] for line in out.splitlines() if line.strip())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paid", action="store_true", help="유료 단계(음성·클립·조립) 요건까지 검사")
    ap.add_argument("--publish", action="store_true", help="업로드 요건까지 검사")
    a = ap.parse_args()
    cfg = load_config()
    problems = []

    for cli in ("ffmpeg", "ffprobe", "yt-dlp", "higgsfield", "claude"):
        if shutil.which(cli):
            print(f"OK  {cli}")
        else:
            problems.append(f"{cli} 없음")
    for flt in ("subtitles", "drawtext", "overlay"):
        ok = ffmpeg_has(flt)
        print(("OK " if ok else "MISSING"), f"ffmpeg filter {flt}")
        if flt == "subtitles" and not ok and cfg["languages"]["ko"].get("subtitle"):
            problems.append("ffmpeg 에 subtitles(libass) 필터 없음 → `brew install ffmpeg-full` 또는 subtitle:false")

    if cfg["llm"]["backend"] == "anthropic_api" and not os.environ.get("ANTHROPIC_API_KEY"):
        problems.append("ANTHROPIC_API_KEY 비어 있음 (llm.backend=anthropic_api)")
    if cfg["research"]["sources"].get("dart") and not os.environ.get("DART_API_KEY"):
        problems.append("DART_API_KEY 비어 있음")
    if cfg["qa"]["mode"] == "discord":
        for k in ("DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID"):
            if not os.environ.get(k):
                problems.append(f"{k} 비어 있음 (qa.mode=discord)")
        if not cfg["qa"].get("discord_allowed_user_ids"):
            problems.append("qa.discord_allowed_user_ids 비어 있음 — 승인자 제한 없이는 실행 불가")
    if cfg["qa"]["mode"] == "telegram":
        for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            if not os.environ.get(k):
                problems.append(f"{k} 비어 있음 (qa.mode=telegram)")
        if not cfg["qa"].get("telegram_allowed_user_ids"):
            problems.append("qa.telegram_allowed_user_ids 비어 있음 — 승인자 제한 없이는 실행 불가")

    if a.paid:
        ok, info = hf.workspace_ok()
        print(("OK " if ok else "FAIL"), "higgsfield workspace")
        if not ok:
            problems.append("Higgsfield 미인증/워크스페이스 미선택: `higgsfield auth login` → `hf workspace set <id>`")
        for lang, lc in cfg["languages"].items():
            if lc.get("enabled") and lc.get("voice_mode", "seed_audio") == "seed_audio" and not lc.get("voice_id"):
                problems.append(f"languages.{lang}.voice_id 비어 있음 (`higgsfield voices list --json`)")
        if not cfg["style"].get("style_key_job_id"):
            print("NOTE style_key_job_id 비어 있음 → smoke 가 스타일 키 샘플을 먼저 만들고 승인 요구")

    if a.publish and cfg["publish"]["youtube"]["enabled"]:
        secret = ROOT / os.environ.get("YT_CLIENT_SECRET_JSON", "config/yt_client_secret.json")
        if not secret.exists():
            problems.append(f"YouTube OAuth client secret 없음: {secret}")

    if problems:
        print("\n미해결:")
        for p in problems:
            print(" -", p)
        sys.exit(1)
    print("\n환경 준비 완료")


if __name__ == "__main__":
    main()
