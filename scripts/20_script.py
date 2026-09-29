#!/usr/bin/env python3
"""2단계 v0.5: 청크 생성 + 언어 강제 + 말하기 기준 길이(리뷰 12·14).
- 개요 1회 → 12블록 청크(캐시) → 조립 → 스키마+정책 검사 → 걸린 블록만 작은 프롬프트로 재작성(≤12) 또는 청크 재작성
- KO: 숫자 읽은 음절 46~52(글자 55~64) / EN: spoken words 14~26(숫자 펼쳐 세기), 블록당 숫자 ≤2, XBRL 원값 금지, 메타문장 금지
- topic 설정의 opening_blocks(훅 고정), required_phrases(경고문 등), title_override, shorts_override 지원"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.state import Episode, load_config, find_topic  # noqa: E402
from lib.llm import ask_json  # noqa: E402
from lib.policy import check_script  # noqa: E402

CHUNK = 12
LANG_NAME = {"ko": "Korean (한국어)", "en": "English"}

COMMON = """당신은 얼굴 없는 데이터 다큐 채널의 작가다. 시청자에게 조언하지 않고, 전문가 행세를 하지 않으며,
검증된 facts 의 사실과 숫자로 서사를 재구성한다. facts 에 없는 수치·사건은 쓰지 않는다.
감정 조작어 금지. 같은 문장·같은 수치 반복 금지. 비유 남발 금지. 메타 문장("this film", "the answer begins", "can that be tested", "마지막 장은") 금지.
문장 규칙: ① 두 번째 문장은 첫 문장을 요약하지 말고 다음 사실을 예고하거나 대비시킨다 ② 요약형 종결 블록은 전체의 1/3 이하
③ 사람과 돈이 등장하는 문장으로 도입, 도입 30초 안에 제목의 핵심 숫자 ④ 시간 점프는 20초 안에 1회 이하
⑤ 출처가 엇갈리는 수치는 범위로 ⑥ 같은 회사를 다룬 기존 히트 영상의 첫 장면·첫 숫자와 다른 사실로 열어라 ⑦ 실존 인물의 의료·사생활 정보 금지
⑧ 숫자 규칙: 블록당 숫자 최대 2개, 소수 한 자리, 큰 수는 반올림("about 216 billion" / "약 1,800억"), 회계 원값("215,938 million")·네 숫자 나열 금지
⑨ 영어: '%'가 아니라 'percent', 첫 언급만 'billion dollars' 이후 'billion', 회계연도는 "its fiscal…"로 회사 소유격, 수사의문문은 편당 3개 이하, 미래 수치는 발화자 귀속
**나레이션 언어는 반드시 {LANG} 이다. 다른 언어 문자를 섞지 마라.**"""

OUTLINE_SYS = COMMON + """
지금은 개요만 만든다. 출력 JSON:
{"title": "제목(핵심 숫자 1개, 낚시 금지, 검색어 포함)", "chapters": [{"start": 1, "end": 12, "beat": "이 구간의 서사 요약 2문장", "key_facts": ["사용할 수치/사건 5~8개 (facts 원문 그대로)"]}, ...],
 "shorts_block_range": [start, end], "description": "영상 설명 3~4문장(메타문장 금지) + 마지막 줄 '출처:' 다음에 URL 목록", "tags": ["..."], "sources": [{"url": "...", "title": "..."}]}"""

BLOCK_SYS = COMMON + """
지금은 지정된 블록 구간만 쓴다. 블록 = 정확히 10초. {LENGTH_RULE}. 각 블록에 fact_refs(사용한 출처 URL).
씬 프롬프트는 영어, 아래 {STYLE} 문구를 그대로 포함, 실존 인물 얼굴·로고·화면 텍스트 금지, AUDIO 는 ambient only.
출력 JSON: {"blocks": [{"n": <번호>, "narration": "...", "scene_prompt": "<STYLE>. SCENE: ... AUDIO: ambient only. NEGATIVE: <NEG>", "fact_refs": ["url"]}]}"""

LENGTH_RULE = {"ko": "한국어는 숫자를 읽었을 때 46~52음절(글자수 55~64자; 숫자는 읽으면 짧아진다)",
               "en": "English: 14~26 SPOKEN words after expanding numbers aloud (e.g. '83.7 billion' = 5 spoken words), i.e. about 16~22 written words"}
META_RE = re.compile(r"(this (film|video) (begins|lines up|starts)|the answer begins|can that be tested|마지막 장은|이 순서를 따라가려면)", re.I)
RAW_XBRL_RE = re.compile(r"\d{1,3},\d{3}\s*million", re.I)
HANGUL_RE = re.compile(r"[가-힣]")
NUMTOK_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")


def _int_words(n):
    if n == 0:
        return 1
    if 1900 <= n <= 2099:
        return 3  # "twenty twenty-six"
    words, groups = 0, 0
    while n > 0:
        g, n = n % 1000, n // 1000
        if g:
            if g >= 100:
                words += 2
            r = g % 100
            if r:
                words += 1 if (r < 20 or r % 10 == 0) else 2
            if groups > 0:
                words += 1  # thousand / million / billion
        groups += 1
    return words


def spoken_words_en(text):
    total = 0
    for tok in NUMTOK_RE.findall(text):
        t = tok.strip("$%")
        if not t:
            continue
        ip, _, fp = t.replace(",", "").partition(".")
        total += _int_words(int(ip) if ip.isdigit() else 0) + (1 + len(fp) if fp else 0)
        total += (1 if tok.startswith("$") else 0) + (1 if tok.endswith("%") else 0)
    rest = NUMTOK_RE.sub(" ", text)
    total += len(re.findall(r"[A-Za-z][A-Za-z'\-]*", rest))
    return total


def count_numbers(text):
    return len([t for t in NUMTOK_RE.findall(text) if not re.fullmatch(r"(19|20)\d\d", t.strip(",.")) and not re.fullmatch(r"10-?[QK]", t)])


def validate_schema(script, n_blocks, bs, shorts_seconds, lang, required_phrases=None):
    errs, blocks = [], script.get("blocks") or []
    if [b.get("n") for b in blocks] != list(range(1, len(blocks) + 1)):
        errs.append("블록 번호가 1..N 연속이 아님")
    if len(blocks) != n_blocks:
        errs.append(f"블록 수 {len(blocks)} ≠ {n_blocks}")
    for b in blocks:
        n, narr = b.get("n"), b.get("narration", "")
        if not narr or not b.get("scene_prompt"):
            errs.append(f"블록 {n} narration/scene_prompt 누락"); continue
        if not b.get("fact_refs"):
            errs.append(f"블록 {n} fact_refs 없음")
        if META_RE.search(narr):
            errs.append(f"블록 {n} 메타 문장")
        if lang == "en":
            if HANGUL_RE.search(narr):
                errs.append(f"블록 {n} 한글 포함(언어 위반)")
            sw = spoken_words_en(narr)
            if not (14 <= sw <= 26):
                errs.append(f"블록 {n} spoken {sw}단어 (14~26)")
            if RAW_XBRL_RE.search(narr):
                errs.append(f"블록 {n} 회계 원값(nnn,nnn million) 사용")
            if count_numbers(narr) > 2:
                errs.append(f"블록 {n} 숫자 {count_numbers(narr)}개 > 2")
            if "%" in narr:
                errs.append(f"블록 {n} '%' 기호 → 'percent'")
        else:
            if not (48 <= len(narr) <= 70):
                errs.append(f"블록 {n} 나레이션 {len(narr)}자 (48~70)")
    text_all = " ".join(b.get("narration", "") for b in blocks)
    for ph in required_phrases or []:
        if ph.lower() not in text_all.lower():
            errs.append(f"필수 문구 누락: '{ph[:40]}'")
    rng, need = script.get("shorts_block_range"), -(-shorts_seconds // bs)
    if shorts_seconds and not (isinstance(rng, list) and len(rng) == 2 and 1 <= rng[0] <= rng[1] <= len(blocks) and rng[1] - rng[0] + 1 >= need):
        errs.append(f"shorts_block_range 무효: {rng} (필요 {need}블록)")
    return errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--lang", default="ko")
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    topic = find_topic(a.topic, a.lang)
    ep = Episode(a.topic, a.lang, smoke=a.smoke)
    if ep.is_done("script"):
        print("script 완료됨 — 건너뜀"); return
    facts = ep.read("facts.json")
    bs = cfg["episode"]["block_seconds"]
    n_blocks = cfg["smoke"]["blocks"] if a.smoke else cfg["episode"]["duration_minutes"] * 60 // bs
    style, neg = cfg["style"]["style_descriptor"], cfg["style"]["negative"]
    lang_name = LANG_NAME.get(a.lang, a.lang)
    common_sys = COMMON.replace("{LANG}", lang_name)
    outline_sys = OUTLINE_SYS.replace("{LANG}", lang_name)
    block_sys = BLOCK_SYS.replace("{LANG}", lang_name).replace("{LENGTH_RULE}", LENGTH_RULE.get(a.lang, LENGTH_RULE["en"]))
    facts_slim = {k: facts.get(k) for k in ("company", "timeline", "figures", "angle_evidence")}
    cache = ep._read("script_parts.json") or {}
    opening = topic.get("opening_blocks") or []
    required = topic.get("required_phrases") or []

    if "outline" not in cache:
        shorts_line = ("shorts_block_range 는 [1, N] 로 두라(smoke)." if a.smoke else
                       f"shorts 구간은 {cfg['episode']['shorts']['seconds']}초 이상(연속 {-(-cfg['episode']['shorts']['seconds'] // bs)}블록 이상).")
        user = f"""언어: {lang_name} / 회사: {topic['company']} / 각도: {topic['angle']} / 총 블록 N={n_blocks}
{shorts_line} chapters 는 {max(1, -(-n_blocks // CHUNK))}개(각 최대 {CHUNK}블록).
{('도입 3블록은 이미 정해져 있다(그대로 사용): ' + json.dumps(opening, ensure_ascii=False)) if opening else ''}
facts: {json.dumps(facts_slim, ensure_ascii=False)}
sources: {json.dumps(facts.get('sources', []), ensure_ascii=False)}"""
        cache["outline"], _ = ask_json(outline_sys, user, ledger=ep, stage="script_outline", timeout=600)
        ep.write("script_parts.json", cache)
    outline = cache["outline"]
    if topic.get("title_override"):
        outline["title"] = topic["title_override"]
    if topic.get("shorts_override"):
        outline["shorts_block_range"] = topic["shorts_override"]
    chapters = outline.get("chapters") or [{"start": i + 1, "end": min(i + CHUNK, n_blocks), "beat": "", "key_facts": []} for i in range(0, n_blocks, CHUNK)]

    blocks = []
    for ch in chapters:
        key = f"blocks_{ch['start']}_{ch['end']}"
        if key not in cache:
            fixed = ""
            if opening and ch["start"] == 1:
                fixed = f"블록 1~{len(opening)} 의 narration 은 다음을 **그대로** 사용(번역·수정 금지): {json.dumps(opening, ensure_ascii=False)}\n"
            req = f"다음 문구가 이 편 어딘가(가능하면 블록 4~12)에 그대로 들어가야 한다: {json.dumps(required, ensure_ascii=False)}\n" if (required and ch["start"] <= 12) else ""
            user = f"""언어: {lang_name} / 회사: {topic['company']} / 제목: {outline.get('title')}
전체 개요: {[c.get('beat') for c in chapters]}
이번 구간: 블록 {ch['start']}~{ch['end']} (정확히 {ch['end'] - ch['start'] + 1}개). 구간 서사: {ch.get('beat')}
이 구간에서 써야 할 사실: {ch.get('key_facts')}
{fixed}{req}{{STYLE}} = "{style}" / <NEG> = "{neg}"
facts(전체): {json.dumps(facts_slim, ensure_ascii=False)}"""
            part, _ = ask_json(block_sys, user, ledger=ep, stage=f"script_{key}", timeout=600)
            cache[key] = part.get("blocks", [])
            ep.write("script_parts.json", cache)
        blocks += cache[key]
    blocks.sort(key=lambda b: b.get("n", 0))
    script = {"title": outline.get("title"), "blocks": blocks, "description": outline.get("description", ""), "tags": outline.get("tags", []),
              "sources": outline.get("sources") or facts.get("sources", []), "shorts_block_range": outline.get("shorts_block_range")}
    shorts_secs = 0 if a.smoke else cfg["episode"]["shorts"]["seconds"]

    def _check(s):
        meta = {"title": s.get("title", ""), "description": s.get("description", ""), "sources": s.get("sources", [])}
        return (validate_schema(s, n_blocks, bs, shorts_secs, a.lang, required),
                check_script(s["blocks"], meta, cfg, people_names=cfg.get("policy", {}).get("real_person_names") or []))

    schema_errs, report = _check(script)
    for round_ in range(2):  # 최대 2회 재작성
        if not (schema_errs or not report["ok"]):
            break
        bad_ns = {int(m.group(1)) for e in schema_errs for m in [re.match(r"블록 (\d+)", e)] if m}
        if not bad_ns and report["issues"]:
            bad_ns = {b["n"] for b in blocks}
        if bad_ns and len(bad_ns) <= 12:
            fix_blocks = [b for b in blocks if b["n"] in bad_ns]
            refs = {u for b in fix_blocks for u in (b.get("fact_refs") or [])}
            figs = [f for f in facts.get("figures", []) if f.get("source") in refs][:40]
            user = f"""언어: {lang_name}. 다음 블록들이 검사에서 걸렸다: {[e for e in schema_errs if any(f'블록 {n} ' in e for n in bad_ns)][:14]} / 정책 {report['issues'][:6]}
각 블록을 **같은 번호·같은 사실·같은 fact_refs** 로 다시 써라. {LENGTH_RULE.get(a.lang)}. scene_prompt 는 그대로, narration 만.
블록: {json.dumps(fix_blocks, ensure_ascii=False)}
관련 수치: {json.dumps(figs, ensure_ascii=False)}
출력 JSON: {{"blocks": [{{"n":..., "narration":"...", "scene_prompt":"...", "fact_refs":[...]}}]}} (이 블록들만)"""
            part, _ = ask_json(block_sys, user, ledger=ep, stage=f"rewrite_blocks_r{round_}", timeout=600)
            fixed = {b["n"]: b for b in part.get("blocks", []) if b.get("n") in bad_ns and b.get("narration")}
            for ch in chapters:
                key = f"blocks_{ch['start']}_{ch['end']}"
                cache[key] = [fixed.get(b["n"], b) for b in cache[key]]
        else:
            for ch in chapters:
                key = f"blocks_{ch['start']}_{ch['end']}"
                if any(ch["start"] <= n <= ch["end"] for n in bad_ns):
                    user = f"""언어: {lang_name}. 이 구간(블록 {ch['start']}~{ch['end']})이 검사에서 걸렸다. 문제: {schema_errs[:10]} / 정책 {report['issues'][:6]}
같은 형식으로 정확히 {ch['end'] - ch['start'] + 1}개 블록을 다시 써라(번호 {ch['start']}부터). {LENGTH_RULE.get(a.lang)}. {{STYLE}} = "{style}" / <NEG> = "{neg}"
기존: {json.dumps(cache[key], ensure_ascii=False)}
관련 사실: {json.dumps(ch.get('key_facts'), ensure_ascii=False)}"""
                    part, _ = ask_json(block_sys, user, ledger=ep, stage=f"rewrite_{key}_r{round_}", timeout=600)
                    cache[key] = part.get("blocks", [])
        ep.write("script_parts.json", cache)
        blocks = sorted(sum((cache[f"blocks_{c['start']}_{c['end']}"] for c in chapters), []), key=lambda b: b.get("n", 0))
        script["blocks"] = blocks
        schema_errs, report = _check(script)

    ep.write("script.json", script)
    ep.write("meta.json", {"title": script["title"], "description": script["description"], "tags": script["tags"], "sources": script["sources"], "shorts_block_range": script["shorts_block_range"]})
    ep.write("policy_check.json", {"schema_errors": schema_errs, **report})
    if schema_errs or not report["ok"]:
        print(f"대본 검사 실패 — 유료 단계 차단. 스키마 {len(schema_errs)}건 {schema_errs[:6]}, 정책 {len(report['issues'])}건 {report['issues'][:4]}"); sys.exit(2)
    ep.mark_done("script", artifact=script)
    print(f"script.json: 블록 {len(blocks)}, 고유 수치 {report['unique_numbers']}, 유효 출처 {report['valid_sources']} — 정책 통과")


if __name__ == "__main__":
    main()
