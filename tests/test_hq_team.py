"""hq_team.py 테스트: 가짜 hq HTTP 서버 + 가짜 실행기. 실제 hq·Codex·유료 단계는 호출하지 않는다."""
import datetime as dt
import fcntl
import hashlib
import io
import json
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hq_team  # noqa: E402

TOPICS = [
    {"id": "a", "company": "에이사", "angle": "각도A", "status": "queued"},
    {"id": "b", "company": "비사", "angle": "각도B", "status": "queued"},
    {"id": "c", "company": "씨사", "angle": "각도C", "status": "published"},
    {"id": "d", "company": "디사", "angle": "각도D", "status": "queued"},
    {"id": "e", "company": "이사", "angle": "각도E", "status": "queued"},
    {"id": "f", "company": "에프사", "angle": "각도F", "status": "queued"},
]
HASH = "abcdef1234567890"
NOW = dt.datetime(2026, 9, 30, 12, 0, tzinfo=dt.timezone.utc)


class FakeHQ:
    def __init__(self):
        self.quota = {"mode": "normal"}
        self.approvals = {}
        self.posts = []
        hq = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path == "/api/quota":
                    return self._send(200, hq.quota)
                if self.path.startswith("/api/approvals/"):
                    cid = unquote(self.path[len("/api/approvals/"):])
                    if cid in hq.approvals:
                        return self._send(200, hq.approvals[cid])
                    return self._send(404, {"error": "not found"})
                self._send(404, {"error": "no route"})

            def do_POST(self):
                n = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(n).decode())
                body["_auth"] = self.headers.get("authorization")
                hq.posts.append(body)
                hq.approvals[body["id"]] = {**body, "decision": None}
                self._send(201, {"ok": True})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def decide(self, cid, decision):
        self.approvals[cid]["decision"] = decision

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeRun:
    """스크립트별 (rc, output) 큐. 마지막 응답은 반복."""
    def __init__(self, tc):
        self.tc = tc
        self.calls = []
        self.responses = {"run_episode.py": [(0, "완료")], "90_review_page.py": [(0, "ok")], "25_approve_script.py": [(0, "승인됨")]}
        self.on_research = None

    def key(self, cmd):
        return Path(cmd[1]).name

    def __call__(self, cmd):
        k = self.key(cmd)
        self.calls.append(k)
        if k == "run_episode.py" and self.on_research:
            self.on_research(cmd)
        q = self.responses[k]
        return q.pop(0) if len(q) > 1 else q[0]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.episodes = self.root / "episodes"
        self.state_dir = self.root / "state"
        self.episodes.mkdir()
        self.hq = FakeHQ()
        self.run = FakeRun(self)
        self.now = NOW

    def tearDown(self):
        self.hq.close()
        self.tmp.cleanup()

    def invoke(self, url=None):
        out = io.StringIO()
        team = hq_team.Team(hq_team.HQClient(url or self.hq.url, "tok"), self.run, root=self.root,
                            episodes_dir=self.episodes, state_dir=self.state_dir, now=lambda: self.now,
                            load_topics=lambda lang: TOPICS, team="pipe", out=out)
        rc = team.main()
        self.out = out.getvalue()
        return rc

    def state(self):
        return json.loads((self.state_dir / "hq_team.json").read_text(encoding="utf-8"))

    def set_state(self, **kw):
        st = {"queue": [], "pending_card": None, "pending_labels": {}, "card_seq": 0, "snooze_until": None}
        st.update(kw)
        self.state_dir.mkdir(exist_ok=True)
        (self.state_dir / "hq_team.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")

    def mark_script_done(self, tid, lang="ko"):
        d = self.episodes / f"{tid}-{lang}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(json.dumps({"done": {"research": "x", "script": "y"}}), encoding="utf-8")

    def write_request(self, tid, lang="ko"):
        d = self.episodes / f"{tid}-{lang}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "approval_request.json").write_text(json.dumps({
            "bundle_hash": HASH, "title": "제목X", "blocks": 5, "hf_calls_estimate": 11, "cap": 20,
            "checklist": ["조언 없음", "출처 확인"]}, ensure_ascii=False), encoding="utf-8")

    def last_status(self):
        return [l for l in self.out.splitlines() if l.startswith("STATUS: ")][-1][len("STATUS: "):]


class TestHQTeam(Base):
    def test_1_quota_save(self):
        self.hq.quota = {"mode": "save"}
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.run.calls, [])
        self.assertIn("hq 모드: save", self.last_status())

    def test_1b_unobserved_quota_still_runs(self):
        self.hq.quota = {"mode": "unobserved"}
        self.set_state(queue=[{"topic": "a", "lang": "ko", "status": "queued", "note": None}])
        self.run.responses["run_episode.py"] = [(1, "검증된 자료 부족")]
        self.assertEqual(self.invoke(), 1)
        self.assertEqual(self.run.calls, ["run_episode.py"])
        self.assertEqual(self.state()["queue"][0]["status"], "failed")
        self.assertNotIn("사용량 절약", self.last_status())

    def test_1c_hold_and_unknown_quota_stay_blocked(self):
        for mode in ("hold", "unknown", None):
            self.hq.quota = {"mode": mode}
            self.assertEqual(self.invoke(), 0)
            self.assertEqual(self.run.calls, [])

    def test_2_topic_card(self):
        self.mark_script_done("b")
        self.assertEqual(self.invoke(), 3)
        self.assertEqual(len(self.hq.posts), 1)
        p = self.hq.posts[0]
        self.assertEqual(p["id"], "team:pipe:topic-1")
        self.assertEqual(p["teamId"], "pipe")
        self.assertEqual(p["_auth"], "Bearer tok")
        self.assertEqual(p["options"], ["에이사 (a)", "디사 (d)", "이사 (e)", "보류"])
        self.assertEqual(p["body"], "- 에이사: 각도A\n- 디사: 각도D\n- 이사: 각도E")
        self.assertEqual(p["subjectHash"], hashlib.sha256(b"a,d,e").hexdigest())
        self.assertEqual(p["expiresInMinutes"], 10080)
        st = self.state()
        self.assertEqual(st["pending_card"], "team:pipe:topic-1")
        self.assertEqual(st["card_seq"], 1)
        self.assertEqual(self.last_status(), "다음 주제 선택을 요청했어요")
        self.assertEqual(self.run.calls, [])
        # 결정 전 재실행 → 대기
        self.assertEqual(self.invoke(), 3)
        self.assertEqual(self.last_status(), "주제 선택을 기다려요")
        self.assertEqual(len(self.hq.posts), 1)

    def test_3_topic_decided_runs_research_and_approve(self):
        self.invoke()
        self.hq.decide("team:pipe:topic-1", "디사 (d)")
        self.run.on_research = lambda cmd: self.mark_script_done("d")
        rc = self.invoke()
        self.assertEqual(rc, 0)
        self.assertEqual(self.run.calls, ["run_episode.py", "90_review_page.py", "25_approve_script.py"])
        st = self.state()
        self.assertIsNone(st["pending_card"])
        self.assertEqual(st["queue"], [{"topic": "d", "lang": "ko", "status": "approved", "note": None}])
        self.assertIn("리서치·대본 작성 중: 디사", self.out)
        self.assertIn("대본 승인됨: 디사", self.last_status())

    def test_4_usage_limit(self):
        self.set_state(queue=[{"topic": "a", "lang": "ko", "status": "queued", "note": None}])
        self.run.responses["run_episode.py"] = [(1, "Codex 사용량 한도: rate limit\n단계 1 종료코드 1 — 중단")]
        self.assertEqual(self.invoke(), 75)
        self.assertEqual(self.state()["queue"][0]["status"], "running")
        self.assertEqual(self.last_status(), "Codex 사용 한도라 나중에 이어서 해요: 에이사")

    def test_4b_research_failure(self):
        self.set_state(queue=[{"topic": "a", "lang": "ko", "status": "queued", "note": None}])
        self.run.responses["run_episode.py"] = [(1, "뭔가\n단계 1 종료코드 1 — 중단\n\n")]
        self.assertEqual(self.invoke(), 1)
        q = self.state()["queue"][0]
        self.assertEqual((q["status"], q["note"]), ("failed", "단계 1 종료코드 1 — 중단"))

    def test_5_waiting_paid_then_card(self):
        self.mark_script_done("a")
        self.set_state(queue=[{"topic": "a", "lang": "ko", "status": "scripted", "note": None}])
        self.run.responses["25_approve_script.py"] = [(2, "voice_id 미선택 — 음성이 정해져야 승인 묶음을 만들 수 있음"), (3, "승인 요청")]
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.state()["queue"][0]["status"], "waiting_paid")
        self.assertIn("유료 단계 준비가 필요해요", self.last_status())
        self.write_request("a")
        self.assertEqual(self.invoke(), 3)
        self.assertEqual(len(self.hq.posts), 1)
        p = self.hq.posts[0]
        self.assertEqual(p["id"], f"team:pipe:script-a-{HASH[:8]}")
        self.assertEqual(p["subjectHash"], HASH)
        self.assertEqual(p["title"], "대본 승인: 제목X")
        self.assertEqual(p["options"], ["승인", "반려"])
        self.assertEqual(p["body"].splitlines(), ["블록 5개 · 예상 유료 호출 11회 (한도 20)",
                                                   "검토본: episodes/a-ko/review.html", "- 조언 없음", "- 출처 확인"])
        self.assertEqual(self.last_status(), "대본 승인을 요청했어요: 에이사")
        self.assertNotIn("run_episode.py", self.run.calls)

    def test_6_script_approved(self):
        self.mark_script_done("a")
        self.write_request("a")
        self.set_state(queue=[{"topic": "a", "lang": "ko", "status": "scripted", "note": None}])
        self.run.responses["25_approve_script.py"] = [(3, "승인 요청")]
        self.assertEqual(self.invoke(), 3)
        cid = f"team:pipe:script-a-{HASH[:8]}"
        self.assertEqual(self.invoke(), 3)
        self.assertEqual(self.last_status(), "대본 승인을 기다려요: 에이사")
        self.hq.decide(cid, "승인")
        seen = {}
        self.run.responses["25_approve_script.py"] = [(3, "승인 요청"), (0, "승인 기록")]
        orig = self.run.__call__

        def spy(cmd):
            f = self.episodes / "a-ko" / "APPROVE_SCRIPT"
            if Path(cmd[1]).name == "25_approve_script.py" and f.exists():
                seen["content"] = f.read_text(encoding="utf-8")
            return orig(cmd)
        team_run, self.run = self.run, spy
        try:
            self.assertEqual(self.invoke(), 0)
        finally:
            self.run = team_run
        self.assertEqual(seen["content"], f"회장 {HASH[:8]}\n")
        self.assertEqual(self.state()["queue"][0]["status"], "approved")
        self.assertIn("대본 승인됨: 에이사", self.last_status())
        self.assertEqual(len(self.hq.posts), 1)

    def test_7a_script_rejected(self):
        self.mark_script_done("a")
        self.write_request("a")
        self.set_state(queue=[{"topic": "a", "lang": "ko", "status": "scripted", "note": None}])
        self.run.responses["25_approve_script.py"] = [(3, "승인 요청")]
        self.invoke()
        self.hq.decide(f"team:pipe:script-a-{HASH[:8]}", "반려")
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.state()["queue"][0]["status"], "rejected")
        self.assertEqual(self.last_status(), "대본이 반려됐어요: 에이사")
        self.assertFalse((self.episodes / "a-ko" / "APPROVE_SCRIPT").exists())

    def test_7b_topic_snooze(self):
        self.invoke()
        self.hq.decide("team:pipe:topic-1", "보류")
        self.assertEqual(self.invoke(), 0)
        st = self.state()
        self.assertIsNone(st["pending_card"])
        self.assertEqual(dt.datetime.fromisoformat(st["snooze_until"]), NOW + dt.timedelta(hours=24))
        self.now = NOW + dt.timedelta(hours=23)
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.last_status(), "다음 주제를 기다려요")
        self.assertEqual(len(self.hq.posts), 1)
        self.now = NOW + dt.timedelta(hours=25)
        self.assertEqual(self.invoke(), 3)
        self.assertEqual(self.hq.posts[-1]["id"], "team:pipe:topic-2")

    def test_7c_pending_card_404_cleared(self):
        self.set_state(pending_card="team:pipe:topic-9", pending_labels={"x (a)": "a"}, card_seq=9)
        self.assertEqual(self.invoke(), 3)
        self.assertEqual(self.hq.posts[-1]["id"], "team:pipe:topic-10")

    def test_8_lock_held(self):
        self.state_dir.mkdir()
        f = open(self.state_dir / "hq_team.lock", "w")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.invoke(), 0)
            self.assertEqual(self.last_status(), "이미 실행 중이에요")
            self.assertEqual(self.hq.posts, [])
        finally:
            f.close()

    def test_9_unreachable(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        self.assertEqual(self.invoke(url=f"http://127.0.0.1:{port}"), 1)
        self.assertEqual(self.last_status(), "hq에 연결할 수 없어요")
        self.assertEqual(self.run.calls, [])


if __name__ == "__main__":
    unittest.main()
