"""Higgsfield CLI 래퍼 v0.4 (리뷰 11 I4/I7 반영):
- 제출(create, --wait 없음)로 job id 를 **먼저** 받아 ledger 에 저장한 뒤 `generate wait` 로 완료 대기 → 타임아웃/파싱 실패에도 ID 보존
- cap 예약은 호출자가 제출 전에 한다(reserve_call)
- 응답 검증: UUID id + 성공 상태 + http URL; 배열이면 정확히 1개여야 성공"""
import json
import re
import subprocess
import time

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
SUCCESS_STATES = {"completed", "succeeded", "success", "done", "finished"}
FAIL_STATES = {"failed", "error", "cancelled", "canceled", "rejected"}


class HFError(RuntimeError):
    pass


class HFPending(HFError):
    def __init__(self, job_id, msg):
        super().__init__(msg); self.job_id = job_id


def _run(args, timeout):
    p = subprocess.run(["higgsfield", *args, "--json"], capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise HFError(f"higgsfield {' '.join(args[:3])}: {p.stderr.strip()[:600]}")
    out = p.stdout.strip()
    try:
        return json.loads(out) if out else {}
    except json.JSONDecodeError:
        raise HFError(f"non-JSON output: {out[:300]}")


def _one(data):
    if isinstance(data, list):
        if len(data) != 1:
            raise HFError(f"job 배열 길이 {len(data)} ≠ 1")
        return data[0]
    return data


def job_id(job):
    for k in ("id", "job_id", "jobId"):
        v = job.get(k) if isinstance(job, dict) else None
        if isinstance(v, str) and UUID_RE.match(v):
            return v
    return None


def job_status(job):
    return str(job.get("status") or job.get("state") or "").lower()


def result_url(job, kind=None):
    keys = ["result_url", "url", "output_url"] + ([f"{kind}_url"] if kind else ["video_url", "audio_url", "image_url"])
    for key in keys:
        v = job.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v
    for key in ("results", "outputs", "result"):
        r = job.get(key)
        if isinstance(r, list):
            r = r[0] if r else None
        if isinstance(r, dict):
            for k in keys:
                if isinstance(r.get(k), str) and r[k].startswith("http"):
                    return r[k]
        if isinstance(r, str) and r.startswith("http"):
            return r
    return None


def validate_done(job, kind=None):
    jid = job_id(job)
    if not jid:
        raise HFError(f"job id 없음: {json.dumps(job)[:300]}")
    st = job_status(job)
    if st in FAIL_STATES:
        raise HFError(f"job {jid} 실패 '{st}'")
    url = result_url(job, kind)
    if st not in SUCCESS_STATES or not url:
        raise HFPending(jid, f"job {jid} 상태 '{st or '미표기'}' url={'있음' if url else '없음'}")
    return jid, url


def get_job(jid, timeout=60):
    return _one(_run(["generate", "get", jid], timeout))


def wait_job(jid, minutes=20, kind=None):
    job = _one(_run(["generate", "wait", jid, "--wait-timeout", f"{minutes}m"], timeout=minutes * 60 + 60))
    return validate_done(job, kind)


def submit(model, args, ledger, stage, **fields):
    """제출만. job id 를 즉시 ledger 에 기록해 돌려준다."""
    data = _run(["generate", "create", model, *args], timeout=180)
    job = _one(data)
    jid = job_id(job)
    if not jid:
        ledger.add_cost(f"{stage}_submit_unknown", model=model, raw=json.dumps(job)[:200], **fields)
        raise HFError(f"{model}: 제출 응답에 job id 없음 — `higgsfield generate list --json` 확인")
    ledger.add_cost(f"{stage}_submitted", model=model, job=jid, **fields)
    return jid


def create(model, args, ledger, stage, kind=None, wait_minutes=20, **fields):
    """submit → wait. 반환 (job_id, url, seconds)."""
    t0 = time.time()
    jid = submit(model, args, ledger, stage, **fields)
    try:
        jid, url = wait_job(jid, wait_minutes, kind)
    except HFPending as e:
        ledger.add_cost(f"{stage}_pending", model=model, job=jid, **fields)
        raise
    except subprocess.TimeoutExpired:
        ledger.add_cost(f"{stage}_timeout", model=model, job=jid, **fields)
        raise HFPending(jid, f"{model} job {jid} 로컬 타임아웃 — resume 로 재접속")
    secs = round(time.time() - t0, 1)
    ledger.add_cost(f"{stage}_done", model=model, job=jid, seconds=secs, **fields)
    return jid, url, secs


def resume(jid, ledger, stage, kind=None, wait_minutes=20):
    """이미 제출된 job 재접속. 실패 상태면 HFError."""
    try:
        jid, url = validate_done(get_job(jid), kind)
    except HFPending:
        jid, url = wait_job(jid, wait_minutes, kind)
    ledger.add_cost(f"{stage}_resumed", job=jid)
    return jid, url


def workspace_ok():
    try:
        return True, _run(["account", "status"], timeout=60)
    except (HFError, subprocess.TimeoutExpired) as e:
        return False, str(e)


def list_voices():
    return _run(["voices", "list"], timeout=120)


def estimate_cost(model, **params):
    args = ["generate", "cost", model]
    for k, v in params.items():
        args += [f"--{k}", str(v)]
    try:
        return _run(args, timeout=60)
    except HFError:
        return None


# --- 스킬 문서 규격 ---
def generate_image(model, prompt, aspect_ratio, ledger):
    return create(model, ["--prompt", prompt, "--aspect_ratio", aspect_ratio], ledger, "style_key", kind="image")


def generate_clip(model, prompt, style_key_id, block, duration, resolution, aspect_ratio, ledger):
    return create(model, ["--prompt", prompt, "--image", style_key_id, "--duration", str(duration), "--resolution", resolution,
                          "--aspect_ratio", aspect_ratio], ledger, "clip", kind="video", block=block)


def generate_voice(text, voice_id, voice_type, block, speech_rate, ledger):
    args = ["--prompt", text, "--voice_type", voice_type, "--voice_id", voice_id]
    if speech_rate and speech_rate != 1.0:
        args += ["--speech_rate", str(speech_rate)]
    return create("seed_audio", args, ledger, "voice", kind="audio", block=block, chars=len(text))


def assemble(blocks_json_path, width, height, ledger, subtitles_font=None):
    args = ["--items", f"@{blocks_json_path}", "--width", str(width), "--height", str(height)]
    if subtitles_font:
        args += ["--subtitles", json.dumps({"font": subtitles_font})]
    return create("explainer_video", args, ledger, "assemble", kind="video", wait_minutes=40)
