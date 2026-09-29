#!/usr/bin/env python3
"""검토본 페이지 생성: episodes/<id>/review.html — 승인 게이트(25_approve_script)용 사람 검토 자료.
facts·script·policy_check·source_report·facts_corrections·ledger 를 한 페이지에. Artifact 로 발행하거나 로컬에서 연다."""
import argparse
import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.state import Episode, load_config  # noqa: E402
from lib.gate import bundle_hash  # noqa: E402

CSS = """
:root{--ground:#EEF0F3;--surface:#FFFFFF;--ink:#14213D;--ink-2:#4B5A73;--line:#D5DAE3;--accent:#C88A1F;--accent-ink:#7A5210;
--good:#2E7D5B;--warn:#B8621B;--bad:#B23A3A;--mono:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
--serif:'Noto Serif KR','Apple SD Gothic Neo',serif;--sans:'Noto Sans KR','Apple SD Gothic Neo',system-ui,sans-serif}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--ground:#0F1521;--surface:#171F2E;--ink:#E6E9EF;--ink-2:#A4AFC2;--line:#2B3548;--accent:#E0A23A;--accent-ink:#F2C878;--good:#5FBF93;--warn:#E09A55;--bad:#E07070}}
:root[data-theme="dark"]{--ground:#0F1521;--surface:#171F2E;--ink:#E6E9EF;--ink-2:#A4AFC2;--line:#2B3548;--accent:#E0A23A;--accent-ink:#F2C878;--good:#5FBF93;--warn:#E09A55;--bad:#E07070}
*{box-sizing:border-box}body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--sans);font-size:15px;line-height:1.6;padding-block:0 64px;padding-inline:20px}
.wrap{max-width:860px;margin:0 auto}
header.top{position:sticky;top:0;background:var(--ground);border-bottom:1px solid var(--line);padding-block:12px;z-index:2;display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}
header.top .id{font-family:var(--mono);font-size:12px;color:var(--ink-2)}
.pill{display:inline-block;font-family:var(--mono);font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--ink-2)}
.pill.good{border-color:var(--good);color:var(--good)}.pill.bad{border-color:var(--bad);color:var(--bad)}.pill.warn{border-color:var(--warn);color:var(--warn)}
h1{font-family:var(--serif);font-weight:600;font-size:clamp(24px,3.4vw,34px);line-height:1.3;text-wrap:balance;margin:28px 0 8px}
h2{font-family:var(--serif);font-weight:600;font-size:20px;margin:40px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h3{font-size:15px;margin:22px 0 8px;color:var(--ink-2);text-transform:uppercase;letter-spacing:.06em}
.lead{color:var(--ink-2);margin:0 0 20px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:20px 0}
.stat{background:var(--surface);border:1px solid var(--line);padding:12px 14px}
.stat b{display:block;font-family:var(--mono);font-size:26px;font-variant-numeric:tabular-nums;line-height:1.1}
.stat span{font-size:12px;color:var(--ink-2)}
.chapter{margin-top:22px}.chapter .beat{color:var(--ink-2);font-size:14px;margin:0 0 10px}
.block{display:grid;grid-template-columns:64px 1fr;gap:12px;padding:8px 0;border-top:1px solid var(--line)}
.block:first-of-type{border-top:0}
.block .t{font-family:var(--mono);font-size:12px;color:var(--ink-2);font-variant-numeric:tabular-nums;padding-top:3px}
.block.shorts .t{color:var(--accent-ink)}.block.shorts{background:color-mix(in srgb,var(--accent) 8%,transparent);margin-inline:-8px;padding-inline:8px}
.block .n{font-size:16px;line-height:1.55}
.block .refs{font-family:var(--mono);font-size:11px;color:var(--ink-2);margin-top:3px}
.block .scene{font-family:var(--mono);font-size:11px;color:var(--ink-2);margin-top:4px;white-space:pre-wrap;display:none}
body.show-scenes .block .scene{display:block}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--ink-2);font-weight:500}
.tbl{overflow-x:auto}td.mono,th.mono{font-family:var(--mono);font-size:12px}
.status-live{color:var(--good)}.status-blocked{color:var(--warn)}.status-dead{color:var(--bad)}
ul.plain{padding-left:18px}ul.plain li{margin:4px 0}
.cmd{font-family:var(--mono);font-size:13px;background:var(--surface);border:1px solid var(--line);padding:10px 12px;overflow-x:auto}
.note{border-left:3px solid var(--accent);padding:6px 12px;background:var(--surface);margin:10px 0}
button.toggle{font:inherit;font-size:13px;background:var(--surface);border:1px solid var(--line);color:var(--ink);padding:6px 10px;cursor:pointer}
button.toggle:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
a{color:var(--accent-ink)}@media (max-width:480px){.block{grid-template-columns:52px 1fr}}
"""


def esc(s):
    return html.escape(str(s if s is not None else ""))


def mmss(sec):
    return f"{sec // 60:02d}:{sec % 60:02d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--lang", default="ko")
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    ep = Episode(a.topic, a.lang, smoke=a.smoke)
    facts, script, meta, pol = ep.read("facts.json"), ep.read("script.json"), ep.read("meta.json"), ep.read("policy_check.json")
    srep = ep._read("source_report.json") or {}
    corr = ep._read("facts_corrections.json") or {}
    parts = ep._read("script_parts.json") or {}
    chapters = (parts.get("outline") or {}).get("chapters") or []
    bs = cfg["episode"]["block_seconds"]
    blocks = script["blocks"]
    rng = script.get("shorts_block_range") or [0, 0]
    try:
        bh = bundle_hash(ep, cfg, a.lang)
    except SystemExit:
        bh = "—"
    led = ep.ledger()
    llm_secs = sum(e.get("seconds", 0) for e in led if e["stage"].startswith(("research", "script", "rewrite", "patch")))
    llm_out = sum(e.get("output_tokens", 0) for e in led if "output_tokens" in e)
    urlmap = {s.get("url"): s for s in facts.get("sources", [])}
    status_ok = pol.get("ok") and not pol.get("schema_errors")
    title = esc(script["title"])

    out = ['<meta charset="utf-8">', f"<title>{title}</title>",
           '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Serif+KR:wght@500;600&family=Noto+Sans+KR:wght@400;500&family=IBM+Plex+Mono:wght@400;500&display=swap">',
           f"<style>{CSS}</style>", '<div class="wrap">',
           f'<header class="top"><span class="id">{esc(ep.id)} · 묶음 {esc(bh[:8])}</span>',
           f'<span class="pill {"good" if status_ok else "bad"}">{"정책·스키마 통과" if status_ok else "검사 미통과"}</span>',
           f'<span class="pill">{len(blocks)}블록 · {len(blocks) * bs // 60}분</span><span class="pill warn">승인 대기</span></header>',
           f"<h1>{title}</h1>",
           f'<p class="lead">{esc(a.topic)} · {esc(facts.get("company"))} · 각도: {esc((facts.get("_meta") or {}).get("topic", {}).get("angle", ""))}</p>',
           '<div class="stats">',
           f'<div class="stat"><b>{len(facts.get("figures", []))}</b><span>검증된 수치 (출처 URL 첨부)</span></div>',
           f'<div class="stat"><b>{srep.get("live", "—")}/{srep.get("checked", "—")}</b><span>출처 URL 실제 열림 (GET)</span></div>',
           f'<div class="stat"><b>{pol.get("unique_numbers", "—")}</b><span>대본에 인용된 고유 수치</span></div>',
           f'<div class="stat"><b>{len(corr.get("applied", []))}</b><span>팩트체크 정정 반영</span></div>',
           f'<div class="stat"><b>{int(llm_secs // 60)}분</b><span>생성 소요 (LLM {llm_out:,} 출력 토큰)</span></div>',
           "</div>"]

    if pol.get("issues") or pol.get("schema_errors"):
        out.append('<div class="note"><b>검사 미통과</b><ul class="plain">' + "".join(f"<li>{esc(i)}</li>" for i in (pol.get("schema_errors") or []) + [f"{i['type']}: {i['match']}" for i in pol.get("issues", [])]) + "</ul></div>")

    out.append('<h2>대본 <button class="toggle" type="button" onclick="document.body.classList.toggle(\'show-scenes\')">씬 프롬프트 보기/숨기기</button></h2>')
    out.append(f'<p class="lead">타임코드는 {bs}초 블록 기준. 강조된 구간(블록 {rng[0]}–{rng[1]})이 세로 컷(Shorts/TikTok) 후보.</p>')
    ch_i = 0
    for ch in chapters or [{"start": 1, "end": len(blocks), "beat": ""}]:
        ch_i += 1
        out.append(f'<section class="chapter"><h3>챕터 {ch_i} · 블록 {ch["start"]}–{ch["end"]}</h3><p class="beat">{esc(ch.get("beat", ""))}</p>')
        for b in blocks:
            if not (ch["start"] <= b["n"] <= ch["end"]):
                continue
            cls = "block shorts" if rng[0] <= b["n"] <= rng[1] else "block"
            refs = b.get("fact_refs") or []
            out.append(f'<div class="{cls}"><div class="t">{mmss((b["n"] - 1) * bs)}<br>#{b["n"]:02d}</div><div><div class="n">{esc(b["narration"])}</div>'
                       f'<div class="refs">출처 {len(refs)}건 · {len(b["narration"])}자</div><div class="scene">{esc(b.get("scene_prompt", ""))}</div></div></div>')
        out.append("</section>")

    if corr.get("applied"):
        out.append("<h2>팩트체크 정정 (Codex 원문 대조 → 반영)</h2><ul class=\"plain\">" + "".join(f"<li>{esc(c['note'])}</li>" for c in corr["applied"]) + "</ul>")

    out.append("<h2>근거 수치</h2><div class=\"tbl\"><table><tr><th>항목</th><th class=\"mono\">값</th><th>날짜</th><th>출처</th></tr>")
    for f in facts.get("figures", []):
        u = f.get("source", ""); st = urlmap.get(u, {}).get("url_status", f.get("source_status", ""))
        host = u.split("/")[2] if u.startswith("http") and u.count("/") >= 2 else u
        corr_note = " ⚠ " + esc("; ".join(f.get("corrections", []))) if f.get("corrections") else ""
        out.append(f'<tr><td>{esc(f.get("label"))}{corr_note}</td><td class="mono">{esc(f.get("value"))} {esc(f.get("unit", ""))}</td><td class="mono">{esc(f.get("date", ""))}</td>'
                   f'<td><a href="{esc(u)}" rel="noopener">{esc(host)}</a> <span class="status-{esc(st)}">{esc(st)}</span></td></tr>')
    out.append("</table></div>")

    out.append("<h2>출처</h2><div class=\"tbl\"><table><tr><th>제목</th><th>종류</th><th>상태</th></tr>")
    for s in facts.get("sources", []):
        out.append(f'<tr><td><a href="{esc(s.get("url"))}" rel="noopener">{esc(s.get("title") or s.get("url"))}</a></td><td class="mono">{esc(s.get("kind", ""))}</td><td class="status-{esc(s.get("url_status", ""))}">{esc(s.get("url_status", ""))}</td></tr>')
    out.append("</table></div>")

    if facts.get("open_questions"):
        out.append("<h2>확인 못 한 것 (대본에 단정으로 쓰지 않음)</h2><ul class=\"plain\">" + "".join(f"<li>{esc(q)}</li>" for q in facts["open_questions"]) + "</ul>")

    out.append("<h2>승인</h2><p>검토 항목: 조언·전문가 행세 없음 / 실존 인물 모방 없음 / 수치·출처 원문 대조 / 감정 조작 없음 / 반복·AI 티 문장 없음. 이상 없으면:</p>")
    out.append(f'<div class="cmd">echo \'이름 {esc(bh[:8])}\' &gt; pipeline/episodes/{esc(ep.id)}/APPROVE_SCRIPT</div>')
    out.append('<p class="lead">승인 묶음 = 대본 + 설명 + 음성 ID + 스타일 키 + 예산 cap. 이 중 하나라도 바뀌면 해시가 바뀌어 재승인이 필요합니다. 음성·스타일 키가 아직 비어 있으면 승인 전에 먼저 정해야 합니다.</p>')
    out.append("</div>")
    path = ep.dir / "review.html"
    path.write_text("\n".join(out), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
