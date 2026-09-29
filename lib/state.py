"""에피소드 상태·설정·비용 ledger. v0.2: 원자적 저장, revision 해시로 다운스트림 무효화(I6), ledger 는 append-only(I7)."""
import hashlib
import json
import os
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
EPISODES = ROOT / "episodes"
load_dotenv(CONFIG / "secrets.env")

# ffmpeg-full(libass 자막 필터 포함)이 keg-only 로 설치된 경우 우선 사용 (C3)
_FFMPEG_FULL = Path("/opt/homebrew/opt/ffmpeg-full/bin")
if _FFMPEG_FULL.exists():
    os.environ["PATH"] = f"{_FFMPEG_FULL}:{os.environ.get('PATH', '')}"

# 단계 의존 관계: 상류가 바뀌면 하류 완료 표시를 지운다
DOWNSTREAM = {
    "research": ["verify_sources", "script", "approve_script", "voice", "visuals", "assemble", "qa", "publish"],
    "verify_sources": ["script", "approve_script", "voice", "visuals", "assemble", "qa", "publish"],
    "script": ["approve_script", "voice", "visuals", "assemble", "qa", "publish"],
    "approve_script": ["voice", "visuals", "assemble", "qa", "publish"],
    "voice": ["assemble", "qa", "publish"],
    "visuals": ["assemble", "qa", "publish"],
    "assemble": ["qa", "publish"],
    "qa": ["publish"],
}


def load_config():
    with open(CONFIG / "pipeline.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_topics(lang="ko"):
    """언어별 주제 큐: topics_en.yaml / topics_kr.yaml (ko). 없으면 kr 로 폴백."""
    name = {"ko": "topics_kr.yaml", "en": "topics_en.yaml"}.get(lang, f"topics_{lang}.yaml")
    p = CONFIG / name
    if not p.exists():
        p = CONFIG / "topics_kr.yaml"
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)["topics"]


def find_topic(topic_id, lang="ko"):
    for t in load_topics(lang):
        if t["id"] == topic_id:
            return t
    for t in load_topics("ko"):
        if t["id"] == topic_id:
            return t
    raise SystemExit(f"topic '{topic_id}' not in config/topics_{lang}.yaml or topics_kr.yaml")


def sha(obj):
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def _atomic_write(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class Episode:
    def __init__(self, topic_id, lang, smoke=False):
        self.topic_id, self.lang, self.smoke = topic_id, lang, smoke
        self.id = f"{topic_id}-{lang}" + ("-smoke" if smoke else "")
        self.dir = EPISODES / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state = self._read("state.json") or {"done": {}, "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
        if isinstance(self.state.get("done"), list):  # v0 → v0.2 마이그레이션
            self.state["done"] = {s: "legacy" for s in self.state["done"]}

    def _read(self, name):
        p = self.dir / name
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return None

    def read(self, name):
        data = self._read(name)
        if data is None:
            raise SystemExit(f"{self.dir/name} 없음 — 이전 단계를 먼저 실행하세요")
        return data

    def write(self, name, data):
        _atomic_write(self.dir / name, json.dumps(data, ensure_ascii=False, indent=2))

    def mark_done(self, stage, artifact=None):
        """artifact 해시를 함께 저장. 같은 단계가 새 해시로 완료되면 하류 완료 표시를 무효화."""
        h = sha(artifact) if artifact is not None else "ok"
        prev = self.state["done"].get(stage)
        if prev and prev != h:
            for d in DOWNSTREAM.get(stage, []):
                self.state["done"].pop(d, None)
            self.state.setdefault("invalidations", []).append({"stage": stage, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "reason": "upstream changed"})
        self.state["done"][stage] = h
        self.state["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.write("state.json", self.state)

    def is_done(self, stage):
        return stage in self.state["done"]

    def add_cost(self, stage, **fields):
        """append-only ledger (jsonl). 크레딧·토큰·초·job id·실패까지 전부."""
        with open(self.dir / "ledger.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"stage": stage, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **fields}, ensure_ascii=False) + "\n")

    def ledger(self):
        p = self.dir / "ledger.jsonl"
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


def env(name, required=True):
    v = os.environ.get(name, "")
    if required and not v:
        raise SystemExit(f"환경변수 {name} 가 비어 있음 — config/secrets.env 확인")
    return v
