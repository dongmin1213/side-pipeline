"""LLM 백엔드: Codex CLI (ChatGPT 로그인, API 키 불필요).
- 원문 응답을 파싱 전에 episodes/<id>/llm_raw/<stage>.txt 로 저장 (I7: 실패해도 출력 유실 없음)
- JSON 추출: ```json 펜스 → 균형 괄호 후보(큰 것부터) 순으로 시도 (프롬프트 속 '{STYLE}' 같은 괄호에 안 걸림)
- 호출별 timeout 명시"""
import json
import os
import re
import subprocess
import time

from .state import load_config


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


DEFAULT_MODEL = "gpt-6-astra"


class LLMError(RuntimeError):
    def __init__(self, message, raw="", meta=None):
        super().__init__(message)
        self.raw, self.meta = raw, meta or {}


def codex_command(model, web_search=False):
    args = [os.environ.get("HQ_CODEX_BIN", "codex"), "exec", "--json", "--ephemeral",
            "--ignore-user-config", "--ignore-rules", "--sandbox", "read-only",
            "--model", model, "-c", 'approval_policy="never"',
            "-c", 'web_search="live"' if web_search else 'web_search="disabled"',
            "-c", "project_doc_max_bytes=0"]
    # This is a text/research backend, not a coding agent. The pipeline owns all file writes.
    for feature in ("shell_tool", "shell_snapshot", "memories", "apps", "plugins", "hooks",
                    "multi_agent", "browser_use", "computer_use", "image_generation", "skill_search"):
        args += ["-c", f"features.{feature}=false"]
    return args + ["-"]


def _decode_events(raw):
    text, session, usage, errors = "", None, {}, []
    complete, searches = False, set()
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        item = event.get("item") or {}
        if kind == "thread.started":
            session = event.get("thread_id")
        elif kind == "turn.started":
            text, complete, errors = "", False, []
        elif kind == "item.completed" and isinstance(item, dict):
            if item.get("type") == "agent_message":
                text = str(item.get("text", ""))
            elif item.get("type") == "web_search":
                searches.add(item.get("id", str(len(searches))))
        elif kind == "turn.completed":
            usage, complete = event.get("usage") or {}, True
        elif kind in ("turn.failed", "error"):
            err = event.get("error") or event
            errors.append(str(err.get("message", err)) if isinstance(err, dict) else str(err))
    return text, session, usage, complete, len(searches), errors


def _codex_exec(system, user, web_search=False, timeout=900, model=DEFAULT_MODEL):
    instruction = ("웹 검색과 페이지 열람으로 원문을 확인하라." if web_search else
                   "도구를 사용하지 말고 주어진 자료만으로 바로 답하라.")
    prompt = f"{system}\n\n---\n{user}\n\n반드시 결과 JSON 객체 하나만 출력하라. 설명 금지. {instruction}"
    args = codex_command(model, web_search)
    # Always use the saved ChatGPT login, including HQ's dedicated CODEX_HOME.
    envv = {k: v for k, v in os.environ.items()
            if k not in ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "HQ_TOKEN", "HQ_TOKEN_FILE")}
    t0 = time.monotonic()
    for attempt in range(1, 4):
        try:
            p = subprocess.run(args, input=prompt, capture_output=True, text=True, timeout=timeout, env=envv)
        except subprocess.TimeoutExpired as exc:
            raw = exc.stdout or ""
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            raise LLMError(f"Codex 타임아웃 {timeout}s", raw) from exc
        except OSError as exc:
            raise LLMError(f"Codex 실행 불가: {exc}") from exc
        text, session, usage, complete, searches, errors = _decode_events(p.stdout)
        meta = {"backend": "codex", "model": model, "seconds": round(time.monotonic() - t0, 1),
                "input_tokens": usage.get("input_tokens", 0), "cached_input_tokens": usage.get("cached_input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0), "cost_usd": None,
                "web_searches": searches, "session_id": session}
        if p.returncode == 0 and complete and text.strip() and not errors:
            return text, meta
        # Only explicit failures mean quota exhaustion. Zero output is not quota evidence.
        detail = "\n".join(errors) or p.stderr.strip() or "완료 이벤트 또는 최종 응답 없음"
        if re.search(r"usage.?limit|rate.?limit|quota|\b429\b", detail, re.I):
            # HQ will schedule the retry (exit 75); don't hold a worker for two hours.
            raise LLMError(f"Codex 사용량 한도: {detail[:500]}", p.stdout, meta)
        if attempt == 3:
            raise LLMError(f"Codex 실패(3회, rc={p.returncode}): {detail[:500]}", p.stdout, meta)
        time.sleep(30 * attempt)


def ask_json(system, user, web_search=False, ledger=None, stage="llm", timeout=900):
    cfg = load_config().get("llm", {})
    if cfg.get("backend", "codex") != "codex":
        raise LLMError("llm.backend는 codex만 지원합니다. config/pipeline.yaml을 확인하세요")
    try:
        text, meta = _codex_exec(system, user, web_search=web_search, timeout=timeout,
                                 model=cfg.get("model", DEFAULT_MODEL))
    except LLMError as exc:
        if ledger is not None:
            _record(ledger, stage, exc.raw, {**exc.meta, "backend": "codex", "error": str(exc)})
        raise
    if ledger is not None:
        _record(ledger, stage, text, meta)
    return extract_json(text), meta


def _record(ledger, stage, text, meta):
    ledger.add_cost(stage, **meta)
    raw_dir = ledger.dir / "llm_raw"
    raw_dir.mkdir(exist_ok=True)
    (raw_dir / f"{stage}_{time.time_ns()}.txt").write_text(text, encoding="utf-8")
