#!/usr/bin/env python3
"""5단계 v0.5: 조립. 두 모드
- server: Higgsfield explainer_video (블록당 정확히 10초 — 짧은 음성은 무음 패딩, 긴 음성은 압축)
- local_trim (기본, 리뷰 12/14 §5): 클립·음성 URL 을 받아 블록 길이 = 오디오 길이 + 0.4초(≤ 10초)로 트림 후 concat → 공백·초과 둘 다 해소
공통: 1:1 매핑·성공 상태·job id 중복 검증, 자막 번인(libass), 세로 컷은 raw 에서 crop 후 세로 자막(마지막 블록까지 포함)."""
import argparse
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.state import Episode, load_config  # noqa: E402
from lib import hf  # noqa: E402
from lib.gate import require_approval, reserve_call  # noqa: E402

FONT_KO, FONT_EN = "AppleGothic", "Helvetica"


def ffmpeg_has(f):
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    return any(line.split()[1:2] == [f] for line in out.splitlines() if line.strip())


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True).stdout
    j = json.loads(out or "{}"); s = (j.get("streams") or [{}])[0]
    return {"width": s.get("width"), "height": s.get("height"), "duration": float((j.get("format") or {}).get("duration", 0) or 0)}


def audio_len(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip()
    return float(out) if out else None


def download(url, path):
    if path.exists() and path.stat().st_size > 0:
        return path
    with requests.get(url, stream=True, timeout=900) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for c in r.iter_content(1 << 20):
                f.write(c)
    return path


def ts(sec):
    ms = int(round(sec * 1000)); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def srt_from_timeline(timeline, max_chars=None):
    """timeline: [(start, end, text)]"""
    lines = []
    for i, (st, en, text) in enumerate(timeline, 1):
        if max_chars and len(text) > max_chars:
            text = "\n".join(text[j:j + max_chars] for j in range(0, len(text), max_chars))
        lines += [str(i), f"{ts(st + 0.15)} --> {ts(max(st + 0.5, en - 0.15))}", text, ""]
    return "\n".join(lines)


def burn(src, srt_path, dst, font, size=20, margin=40):
    style = f"FontName={font},FontSize={size},Outline=1,MarginV={margin}"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vf", f"subtitles='{srt_path}':force_style='{style}'", "-c:a", "copy", str(dst)], check=True)


def local_trim_assemble(ep, pairs, bs, pad=0.4):
    """각 블록: 클립을 (오디오 길이+pad, 최대 bs) 로 자르고 오디오를 얹어 세그먼트 생성 → concat. 반환 timeline."""
    seg_dir = ep.dir / "segments"; seg_dir.mkdir(exist_ok=True)
    parts, timeline, t = [], [], 0.0
    for b, v, au in pairs:
        n = b["n"]
        clip = download(v["url"], seg_dir / f"clip_{n}.mp4")
        audio = download(au["url"], seg_dir / f"voice_{n}.mp3") if au.get("url") else Path(au["file"])
        alen = audio_len(audio)
        if alen is None:
            raise SystemExit(f"블록 {n} 오디오 길이 측정 실패")
        dur = min(bs, round(alen + pad, 2))
        seg = seg_dir / f"seg_{n}.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(clip), "-i", str(audio), "-t", str(dur), "-map", "0:v:0", "-map", "1:a:0",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-ar", "48000", "-shortest", str(seg)], check=True)
        timeline.append((t, t + dur, b["narration"])); t += dur; parts.append(seg)
        if alen > bs:
            ep.add_cost("assemble_warn", block=n, note=f"audio {alen:.1f}s > {bs}s (잘림)")
    lst = ep.dir / "concat.txt"; lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    raw = ep.dir / "final_raw.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(raw)], check=True)
    ep.write("timeline.json", [{"n": b["n"], "start": s, "end": e} for (s, e, _), (b, _, _) in zip(timeline, pairs)])
    return raw, timeline


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
    if ep.is_done("assemble"):
        print("assemble 완료됨 — 건너뜀"); return
    if lc.get("subtitle") and not ffmpeg_has("subtitles"):
        print("ffmpeg 에 subtitles(libass) 필터 없음 — 중단"); sys.exit(2)
    script, vis, voice = ep.read("script.json"), ep.read("visuals.json"), ep.read("voice.json")
    if vis.get("bundle_hash") != h or voice.get("bundle_hash") != h:
        print("클립/음성이 현재 승인 묶음과 다름 — 재생성 필요"); sys.exit(2)
    bs = cfg["episode"]["block_seconds"]
    mode = (cfg.get("assembly") or {}).get("mode", "local_trim")

    pairs, errs = [], []
    for b in script["blocks"]:
        n = str(b["n"]); v, au = vis["clips"].get(n, {}), voice["takes"].get(n, {})
        if v.get("status") != "succeeded" or not v.get("job_id"):
            errs.append(f"clip {n}")
        if au.get("status") != "succeeded" or not (au.get("job_id") or au.get("file")):
            errs.append(f"voice {n}")
        pairs.append((b, v, au))
    if errs or len(pairs) < 2:
        print(f"조립 불가: {errs or '블록 2개 미만'}"); sys.exit(2)
    if len({v["job_id"] for _, v, _ in pairs}) != len(pairs):
        print("클립 job id 중복"); sys.exit(2)

    if mode == "server":
        if lc.get("voice_mode", "seed_audio") != "seed_audio":
            print("server 조립은 audio_job 만 받음 — human 음성은 local_trim"); sys.exit(2)
        reserve_call(ep, cfg)
        blocks = [{"video": {"id": v["job_id"], "type": "video_job"}, "audio": {"id": au["job_id"], "type": "audio_job"}} for _, v, au in pairs]
        bj = ep.dir / "blocks.json"; bj.write_text(json.dumps(blocks))
        w, hh = (1280, 720) if cfg["episode"]["aspect"] == "16:9" else (720, 1280)
        jid, url, _ = hf.assemble(bj, w, hh, ep)
        raw = download(url, ep.dir / "final_raw.mp4")
        timeline = [((i) * bs, (i + 1) * bs, b["narration"]) for i, (b, _, _) in enumerate(pairs)]
    else:
        raw, timeline = local_trim_assemble(ep, pairs, bs)

    info = probe(raw)
    expect = sum(e - s for s, e, _ in timeline)
    if abs(info["duration"] - expect) > 1.5:
        print(f"길이 불일치: {info['duration']:.1f}s vs {expect:.1f}s"); sys.exit(2)

    font = FONT_KO if a.lang == "ko" else FONT_EN
    final = ep.dir / "final.mp4"
    if lc.get("subtitle"):
        s = ep.dir / "subs.srt"; s.write_text(srt_from_timeline(timeline), encoding="utf-8"); burn(raw, s, final, font)
    else:
        shutil.copy(raw, final)
    outputs = {"final": str(final), "final_probe": probe(final), "mode": mode}

    sh = cfg["episode"]["shorts"]
    if sh.get("enabled") and not a.smoke:
        rng = script["shorts_block_range"]
        sel = [(s, e, txt) for (s, e, txt), (b, _, _) in zip(timeline, pairs) if rng[0] <= b["n"] <= rng[1]]
        start, end = sel[0][0], sel[-1][1]
        length = min(sh["seconds"], end - start)
        if length < 60:
            print("Shorts 구간 60초 미만 — 세로 컷 생략"); outputs["short"] = None
        else:
            v_raw = ep.dir / "short_raw.mp4"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(start), "-t", str(length), "-i", str(raw), "-vf", "crop=ih*9/16:ih,scale=1080:1920", "-c:a", "aac", str(v_raw)], check=True)
            vt = [(s - start, min(e, start + length) - start, txt) for s, e, txt in sel if s < start + length]
            s2 = ep.dir / "subs_vertical.srt"; s2.write_text(srt_from_timeline(vt, max_chars=16 if a.lang == "ko" else 28), encoding="utf-8")
            short = ep.dir / "short.mp4"; burn(v_raw, s2, short, font, size=30, margin=260)
            outputs["short"] = str(short); outputs["short_probe"] = probe(short)
            if not (60 <= outputs["short_probe"]["duration"] <= 90.5) or outputs["short_probe"]["width"] != 1080:
                print(f"Shorts 규격 불일치: {outputs['short_probe']}"); sys.exit(2)
    ep.write("outputs.json", outputs)
    ep.mark_done("assemble", artifact=outputs)
    print("완료:", json.dumps(outputs, ensure_ascii=False))


if __name__ == "__main__":
    main()
