"""LLM 백엔드 v0.3. 기본 = claude_code (`claude -p`, Max 구독, 키 불필요). 대안 = anthropic_api.
- 원문 응답을 파싱 전에 episodes/<id>/llm_raw/<stage>.txt 로 저장 (I7: 실패해도 출력 유실 없음)
- JSON 추출: ```json 펜스 → 균형 괄호 후보(큰 것부터) 순으로 시도 (프롬프트 속 '{STYLE}' 같은 괄호에 안 걸림)
- 호출별 timeout 명시"""
import json
import os
import re
import subprocess
import time

from .state import env, load_config


def _candidates(text):
    fences = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    for f in fences:
        yield f
    # 균형 괄호 스캔: 모든 '{' 시작점에서 짝이 맞는 '}' 까지
    starts = [i for i, c in enumerate(text) if c == "{"]
    spans = []
    for s in starts:
        depth, in_str, esc = 0, False, False
        for j in range(s, len(text)):
            c = text[j]
            if in_str:
                if esc: esc = False
                elif c == "\\": esc = True
                elif c == '"': in_str = False
                continue
            if c == '"': in_str = True
            elif c == "{": depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    spans.append((s, j + 1)); break
    for s, e in sorted(spans, key=lambda p: -(p[1] - p[0])):
        yield text[s:e]


def extract_json(text):
    last_err = None
    for cand in _candidates(text):
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError as e:
            last_err = e
    raise RuntimeError(f"JSON 추출 실패 ({last_err}): {text[:300]!r}")


def _claude_code(system, user, tools=None, max_turns=1, timeout=900, model=None):
    no_tools = "" if tools else " 도구(파일 읽기·검색 등)는 사용하지 말고 주어진 자료만으로 바로 답하라."
    prompt = f"{system}\n\n---\n{user}\n\n반드시 결과 JSON 하나만 ```json 펜스 안에 출력하라. 펜스 밖 설명 금지.{no_tools}"
    args = ["claude", "-p", prompt, "--output-format", "json", "--max-turns", str(max_turns)]
    if model:
        args += ["--model", model]
    if tools:
        args += ["--allowedTools", ",".join(tools)]
    else:  # 도구 호출 시도로 max-turns 에 걸려 rc=1 나는 문제(stop_reason=tool_use) 차단
        args += ["--disallowedTools", "Bash,Read,Write,Edit,MultiEdit,Glob,Grep,WebSearch,WebFetch,Agent,NotebookEdit,TodoWrite"]
    envv = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    t0 = time.time()
    last = None
    limit_hits = 0
    for attempt in range(1, 20):
        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=envv, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"claude -p 타임아웃 {timeout}s")
        out = None
        if p.stdout.strip():
            try:
                out = json.loads(p.stdout)
            except json.JSONDecodeError:
                out = None
        if p.returncode == 0 and out and not out.get("is_error"):
            break
        # 사용량 한도 신호: 출력 토큰 0 으로 즉시 종료(stop_sequence). duration 과 무관하게 한도로 간주 → 10분 간격으로 최대 2시간 대기
        usage = (out or {}).get("usage") or {}
        if out is not None and usage.get("output_tokens", 0) == 0 and (out.get("duration_api_ms", 0) < 5000 or out.get("stop_reason") == "stop_sequence"):
            limit_hits += 1
            last = f"usage-limit 추정 (즉시 종료, result={str(out.get('result'))[:200]!r})"
            if limit_hits > 12:
                raise RuntimeError(f"claude -p 사용량 한도 2시간 초과 대기 후 포기: {last}")
            print(f"[llm] 사용량 한도 추정 — 10분 대기 후 재시도 ({limit_hits}/12)", flush=True)
            time.sleep(600); continue
        last = f"rc={p.returncode} stderr={p.stderr[:300]!r} stdout={p.stdout[:300]!r}"
        if attempt >= 3:
            raise RuntimeError(f"claude -p 실패(3회): {last}")
        time.sleep(30 * attempt)
    usage = out.get("usage", {})
    meta = {"backend": "claude_code", "model": model or "default", "seconds": round(time.time() - t0, 1),
            "input_tokens": usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0), "cost_usd": out.get("total_cost_usd"),
            "web_searches": (usage.get("server_tool_use") or {}).get("web_search_requests", 0), "session_id": out.get("session_id")}
    return out.get("result", ""), meta


def _anthropic_api(system, user, model, max_tokens):
    import anthropic
    t0 = time.time()
    c = anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"))
    msg = c.messages.create(model=model, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": user}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    meta = {"backend": "anthropic_api", "model": model, "seconds": round(time.time() - t0, 1),
            "input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens}
    return text, meta


def ask_json(system, user, tools=None, max_turns=1, max_tokens=12000, ledger=None, stage="llm", timeout=900):
    cfg = load_config().get("llm", {})
    backend = cfg.get("backend", "claude_code")
    if backend == "claude_code":
        text, meta = _claude_code(system, user, tools=tools, max_turns=max_turns, timeout=timeout, model=cfg.get("claude_code_model"))
    else:
        text, meta = _anthropic_api(system, user, cfg.get("model", "claude-sonnet-5"), max_tokens)
    if ledger is not None:
        ledger.add_cost(stage, **meta)
        raw_dir = ledger.dir / "llm_raw"; raw_dir.mkdir(exist_ok=True)
        (raw_dir / f"{stage}_{time.strftime('%H%M%S')}.txt").write_text(text, encoding="utf-8")
    return extract_json(text), meta
