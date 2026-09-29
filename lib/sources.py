"""출처 URL 실재 검증 (리뷰 I1/I2). HEAD 가 아니라 GET(범위 요청)으로 확인하고, 봇 차단(403/405/429)은 'blocked' 로 분류해 사람 확인 대상으로 남긴다."""
import concurrent.futures as cf

import requests

UA = "Mozilla/5.0 (Macintosh) content-engine/0.2 source-check"


def check_url(url, timeout=12):
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Range": "bytes=0-2048"}, timeout=timeout, allow_redirects=True, stream=True)
        code = r.status_code
        r.close()
    except requests.RequestException as e:
        return {"url": url, "status": None, "verdict": "dead", "error": type(e).__name__}
    if code < 400 or code in (206,):
        return {"url": url, "status": code, "verdict": "live"}
    if code in (401, 403, 405, 429, 503):
        return {"url": url, "status": code, "verdict": "blocked"}
    return {"url": url, "status": code, "verdict": "dead"}


def check_urls(urls, workers=8):
    uniq = list(dict.fromkeys(u for u in urls if u))
    with cf.ThreadPoolExecutor(workers) as ex:
        results = list(ex.map(check_url, uniq))
    return {r["url"]: r for r in results}


def apply_to_facts(facts):
    """facts 의 sources/figures/timeline 에 url_status 를 붙이고, dead 출처에만 기대는 수치·사건은 제거한다. 반환: (facts, report)"""
    urls = [s.get("url") for s in facts.get("sources", [])] + [f.get("source") for f in facts.get("figures", [])] + [t.get("source") for t in facts.get("timeline", [])]
    status = check_urls(urls)
    for s in facts.get("sources", []):
        s["url_status"] = status.get(s.get("url"), {}).get("verdict", "unknown")
    kept_f, dropped_f = [], []
    for f in facts.get("figures", []):
        v = status.get(f.get("source"), {}).get("verdict", "unknown")
        f["source_status"] = v
        (kept_f if v != "dead" else dropped_f).append(f)
    kept_t, dropped_t = [], []
    for t in facts.get("timeline", []):
        v = status.get(t.get("source"), {}).get("verdict", "unknown")
        t["source_status"] = v
        (kept_t if v != "dead" else dropped_t).append(t)
    facts["figures"], facts["timeline"] = kept_f, kept_t
    facts["sources"] = [s for s in facts["sources"] if s["url_status"] != "dead"]
    report = {"checked": len(status), "live": sum(1 for r in status.values() if r["verdict"] == "live"),
              "blocked": sum(1 for r in status.values() if r["verdict"] == "blocked"), "dead": sum(1 for r in status.values() if r["verdict"] == "dead"),
              "dropped_figures": [f.get("label") for f in dropped_f], "dropped_timeline": [t.get("event") for t in dropped_t],
              "dead_urls": [u for u, r in status.items() if r["verdict"] == "dead"], "blocked_urls": [u for u, r in status.items() if r["verdict"] == "blocked"]}
    return facts, report
