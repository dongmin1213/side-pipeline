"""정책 QA 게이트 v0.2 (리뷰 I2 반영).
정규식은 '보조 경고'가 아니라 하드 실패 조건이며, 사람 QA 가 각 항목을 확인한 기록(qa.json)이 있어야 게시된다.
검사 대상: narration + scene_prompt + title + description + sources 실재성 + 수치 '고유' 개수."""
import re
from urllib.parse import urlparse

ADVICE = [
    r"(사세요|사야 합니다|사십시오|넣으십시오|매수하|매도하|투자하세요|투자해야|추천합니다|추천드립니다|지금 사면|팔아야|전 재산)",
    r"(진단|처방|복용하세요|치료법|법률 자문|소송하세요)",
    r"\b(buy now|you should invest|investment tip|my advice|you should buy|sell now|wealth management)\b",
]
EXPERT = [
    r"(저는|제가) .{0,12}(애널리스트|전문가|변호사|의사|펀드매니저|회계사)(입니다|로서)",
    r"\b(as (an|a) (analyst|expert|lawyer|doctor|financial advisor|cpa))\b",
]
REAL_PERSON_VISUAL = [
    r"\b(realistic|photorealistic|lifelike)\b.{0,60}\b(ceo|chairman|founder|president|celebrity)\b",
    r"\b(mimic|imitate|impersonat|clone).{0,20}\b(voice|face|likeness)\b",
    r"(얼굴|목소리).{0,10}(모방|흉내|복제)",
]
MANIP_DEFAULT = ["충격", "경악", "소름", "긴급", "폭탄", "멸망", "끔찍"]
NUM_RE = re.compile(r"(?:\$\s?\d[\d,\.]*\s*(?:billion|million|trillion|B|M|T)?|\d[\d,\.]*\s*(?:억|조|만|%|원|명|배|년|달러|개|호|위|건|billion|million|trillion|percent|points?|quarters?|years?|x))", re.I)


def _valid_source(s):
    if not isinstance(s, dict):
        return False
    u = urlparse(str(s.get("url", "")))
    return u.scheme in ("http", "https") and bool(u.netloc)


def check_script(blocks, meta, cfg, people_names=None):
    issues = []
    pol = cfg.get("policy", {})
    narr = "\n".join(b.get("narration", "") for b in blocks)
    scenes = "\n".join(b.get("scene_prompt", "") for b in blocks)
    text_all = meta.get("title", "") + "\n" + narr + "\n" + meta.get("description", "")
    # 씬 프롬프트에 실존 인물 이름(설정 목록) 등장 금지 — 사실적 초상 우회 차단 (리뷰 11 I2)
    for name in people_names or []:
        if name and name.lower() in scenes.lower():
            issues.append({"type": "real_person_in_scene", "match": name})

    if pol.get("forbid_advice", True):
        for pat in ADVICE:
            for m in re.finditer(pat, text_all, re.I):
                issues.append({"type": "advice", "match": m.group(0)})
    if pol.get("forbid_expert_persona", True):
        for pat in EXPERT:
            for m in re.finditer(pat, text_all, re.I):
                issues.append({"type": "expert_persona", "match": m.group(0)})
    if pol.get("forbid_real_person_voice", True):
        for pat in REAL_PERSON_VISUAL:
            for m in re.finditer(pat, scenes + "\n" + narr, re.I):
                issues.append({"type": "real_person_likeness", "match": m.group(0)})
    for term in pol.get("forbid_emotional_manipulation_terms") or MANIP_DEFAULT:
        if term in text_all:
            issues.append({"type": "emotional_term", "match": term})

    srcs = [s for s in (meta.get("sources") or []) if _valid_source(s)]
    uniq_urls = {s.get("url") for s in srcs}
    dup = len(srcs) - len(uniq_urls)
    if len(uniq_urls) < cfg.get("research", {}).get("min_sources", 3):  # 중복 제거 후 계수 (리뷰 11)
        issues.append({"type": "sources", "match": f"고유 URL 출처 {len(uniq_urls)}개 < 최소 기준"})
    desc = meta.get("description", "")
    if pol.get("require_sources_in_description", True) and not re.search(r"(출처|Sources)[:：]?\s*\n?.*https?://", desc, re.S):
        issues.append({"type": "sources", "match": "설명란에 '출처:' + URL 목록 없음"})

    uniq_numbers = {m.group(0).replace(" ", "") for m in NUM_RE.finditer(narr)}
    if len(uniq_numbers) < pol.get("min_unique_numbers", 10):
        issues.append({"type": "originality", "match": f"고유 수치 {len(uniq_numbers)}개 < {pol.get('min_unique_numbers', 10)}"})

    # 반복 문장(동일 문장 2회 이상)
    sents = [s.strip() for s in re.split(r"[.!?。]\s*", narr) if len(s.strip()) > 12]
    rep = len(sents) - len(set(sents))
    if rep > 0:
        issues.append({"type": "repetition", "match": f"동일 문장 반복 {rep}회"})

    return {"ok": not issues, "issues": issues, "unique_numbers": len(uniq_numbers), "valid_sources": len(srcs), "duplicate_sources": dup}
