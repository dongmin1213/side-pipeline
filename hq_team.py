#!/usr/bin/env python3
"""hq 팀 러너: hq 데몬이 N분마다 실행. 주제 선택·대본 승인을 hq 카드로 받고, 무료 단계(리서치·대본)만 자동 실행한다.
계약: `STATUS: <text>` 출력(마지막 줄이 펫 말풍선), 종료코드 0=대기/완료, 3=승인 대기, 75=사용량 한도, 그 외=오류.
유료 단계(음성·클립·조립)는 절대 실행하지 않는다 — 첫 유료 실행은 사용자가 직접 --smoke 로."""
import datetime as dt
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ACTIVE = ("queued", "running", "scripted", "waiting_paid")
EXPIRES = 10080
EXIT_OK, EXIT_ERR, EXIT_WAIT, EXIT_LIMIT = 0, 1, 3, 75


class HQError(Exception):
    """hq 연결 실패(네트워크·비정상 응답)."""


class HQClient:
    def __init__(self, base_url, token, timeout=10):
        self.base = (base_url or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout

    def _req(self, method, path, body=None):
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("content-type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode() or "null")
            except Exception:
                payload = None
            finally:
                e.close()
            return e.code, payload
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise HQError(str(e)) from e

    def quota(self):
        code, data = self._req("GET", "/api/quota")
        if code != 200 or not isinstance(data, dict):
            raise HQError(f"quota {code}")
        return data

    def get_approval(self, card_id):
        """없으면 None, 있으면 카드 JSON(decision 포함)."""
        code, data = self._req("GET", "/api/approvals/" + urllib.request.quote(card_id, safe=""))
        if code == 404:
            return None
        if code != 200 or not isinstance(data, dict):
            raise HQError(f"approval {code}")
        if "error" in data and "decision" not in data:
            return None
        return data

    def post_approval(self, card):
        code, data = self._req("POST", "/api/approvals", card)
        if code not in (200, 201):
            raise HQError(f"post approval {code}: {data}")
        return data


def subprocess_runner(root, log_path):
    """실제 실행기: repo 루트에서 실행, 출력은 logs/hq_team.log 에 덧붙이고 텍스트로 반환."""
    def run(cmd):
        p = subprocess.run(cmd, cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = p.stdout.decode("utf-8", errors="replace")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n$ {' '.join(cmd)}  [{dt.datetime.now().isoformat(timespec='seconds')}]\n{out}\n[rc={p.returncode}]\n")
        return p.returncode, out
    return run


def _default_load_topics(lang):
    from lib.state import load_topics
    return load_topics(lang)


def last_line(text, limit=200):
    for line in reversed((text or "").splitlines()):
        if line.strip():
            return line.strip()[:limit]
    return ""


class Team:
    def __init__(self, hq, run, root=ROOT, episodes_dir=None, state_dir=None, now=None,
                 load_topics=None, team=None, out=None):
        self.hq, self.run_cmd, self.root = hq, run, Path(root)
        self.episodes = Path(episodes_dir) if episodes_dir else self.root / "episodes"
        self.state_dir = Path(state_dir) if state_dir else self.root / "state"
        self.now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self.load_topics = load_topics or _default_load_topics
        self.team = team or os.environ.get("HQ_TEAM", "pipeline")
        self.out = out or sys.stdout
        self.state_path = self.state_dir / "hq_team.json"
        self.lock_path = self.state_dir / "hq_team.lock"
        self.st = None

    # ---- 출력·상태 ----
    def status(self, text):
        print(f"STATUS: {text}", file=self.out, flush=True)

    def load_state(self):
        st = {"queue": [], "pending_card": None, "pending_labels": {}, "card_seq": 0, "snooze_until": None}
        if self.state_path.exists():
            st.update(json.loads(self.state_path.read_text(encoding="utf-8")))
        return st

    def save(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.st, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    # ---- 보조 ----
    def ep_dir(self, item):
        return self.episodes / f"{item['topic']}-{item['lang']}"

    def script_done(self, topic_id, lang="ko"):
        p = self.episodes / f"{topic_id}-{lang}" / "state.json"
        if not p.exists():
            return False
        try:
            done = json.loads(p.read_text(encoding="utf-8")).get("done", {})
        except ValueError:
            return False
        return "script" in done

    def company(self, item):
        try:
            for t in self.load_topics(item["lang"]):
                if t.get("id") == item["topic"]:
                    return t.get("company") or item["topic"]
        except Exception:
            pass
        return item["topic"]

    def card_id(self, suffix):
        return f"team:{self.team}:{suffix}"

    # ---- 진입점 ----
    def main(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        lock = open(self.lock_path, "w")
        try:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.status("이미 실행 중이에요")
                return EXIT_OK
            self.st = self.load_state()
            try:
                return self.step()
            except HQError:
                self.status("hq에 연결할 수 없어요")
                return EXIT_ERR
        finally:
            lock.close()

    def step(self):
        # 1. 사용량 모드
        mode = self.hq.quota().get("mode")
        if mode not in ("normal", "unobserved"):
            self.status(f"사용량 절약 중이라 쉬어요 (hq 모드: {mode})")
            return EXIT_OK
        # 2. 주제 카드 결정 확인
        if self.st.get("pending_card"):
            rc = self.check_topic_card()
            if rc is not None:
                return rc
        # 3. 활성 항목
        item = next((q for q in self.st["queue"] if q.get("status") in ACTIVE), None)
        if item is None:
            return self.ask_topic()
        # 4. 무료 단계
        if item["status"] in ("queued", "running"):
            if self.script_done(item["topic"], item["lang"]):
                item["status"] = "scripted"; self.save()
            else:
                rc = self.run_free(item)
                if rc is not None:
                    return rc
        # 5. 승인 게이트
        return self.approve(item)

    def check_topic_card(self):
        card = self.hq.get_approval(self.st["pending_card"])
        if card is None:
            self.st["pending_card"] = None; self.st["pending_labels"] = {}; self.save()
            return None
        decision = card.get("decision")
        if decision is None:
            self.status("주제 선택을 기다려요")
            return EXIT_WAIT
        labels = self.st.get("pending_labels") or {}
        self.st["pending_card"] = None; self.st["pending_labels"] = {}
        if decision == "보류" or decision not in labels:
            self.st["snooze_until"] = (self.now() + dt.timedelta(hours=24)).isoformat()
            self.save()
            self.status("주제 선택을 보류했어요 (24시간 뒤 다시 물어요)")
            return EXIT_OK
        self.st["queue"].append({"topic": labels[decision], "lang": "ko", "status": "queued", "note": None})
        self.save()
        return None

    def ask_topic(self):
        snooze = self.st.get("snooze_until")
        if snooze and dt.datetime.fromisoformat(snooze) > self.now():
            self.status("다음 주제를 기다려요")
            return EXIT_OK
        used = {q["topic"] for q in self.st["queue"]}
        cands = [t for t in self.load_topics("ko")
                 if t.get("status") == "queued" and t["id"] not in used and not self.script_done(t["id"], "ko")][:3]
        if not cands:
            self.status("후보 주제가 없어요 (config/topics_kr.yaml에 추가 필요)")
            return EXIT_OK
        self.st["card_seq"] = int(self.st.get("card_seq") or 0) + 1
        cid = self.card_id(f"topic-{self.st['card_seq']}")
        labels = {f"{t['company']} ({t['id']})": t["id"] for t in cands}
        self.hq.post_approval({
            "id": cid, "teamId": self.team, "title": "다음 영상 주제를 골라 주세요",
            "body": "\n".join(f"- {t['company']}: {t.get('angle', '')}" for t in cands),
            "options": list(labels) + ["보류"],
            "subjectHash": hashlib.sha256(",".join(t["id"] for t in cands).encode()).hexdigest(),
            "expiresInMinutes": EXPIRES,
        })
        self.st["pending_card"] = cid; self.st["pending_labels"] = labels; self.st["snooze_until"] = None
        self.save()
        self.status("다음 주제 선택을 요청했어요")
        return EXIT_WAIT

    def run_free(self, item):
        """리서치·대본. 성공이면 None(같은 실행에서 5단계로)."""
        name = self.company(item)
        item["status"] = "running"; self.save()
        self.status(f"리서치·대본 작성 중: {name}")
        rc, out = self.run_cmd([sys.executable, "run_episode.py", "--topic", item["topic"], "--lang", item["lang"], "--until", "2"])
        if rc == 0:
            item["status"] = "scripted"; item["note"] = None; self.save()
            return None
        if "사용량 한도" in (out or ""):
            self.status(f"Codex 사용 한도라 나중에 이어서 해요: {name}")
            return EXIT_LIMIT
        item["status"] = "failed"; item["note"] = last_line(out); self.save()
        self.status(f"대본 단계 실패: {name} — {item['note']}")
        return EXIT_ERR

    def approve_script(self, item):
        return self.run_cmd([sys.executable, "scripts/25_approve_script.py", "--topic", item["topic"], "--lang", item["lang"]])

    def mark_approved(self, item, name):
        item["status"] = "approved"; item["note"] = None; self.save()
        self.status(f"대본 승인됨: {name} · 유료 제작은 직접 실행해요 (run_episode.py --from 4 --smoke)")
        return EXIT_OK

    def approve(self, item):
        name = self.company(item)
        self.run_cmd([sys.executable, "scripts/90_review_page.py", "--topic", item["topic"], "--lang", item["lang"]])
        rc, out = self.approve_script(item)
        out = out or ""
        if rc == 0:
            return self.mark_approved(item, name)
        if rc == 2:
            if "voice_id 미선택" in out or "style_key_job_id" in out:
                item["status"] = "waiting_paid"; item["note"] = last_line(out); self.save()
                self.status(f"대본 준비 완료: {name} · 유료 단계 준비가 필요해요 (음성·스타일 선택, Higgsfield 로그인)")
                return EXIT_OK
            item["status"] = "failed"; item["note"] = last_line(out); self.save()
            self.status(f"승인 묶음을 만들 수 없어요: {name} — {item['note']}")
            return EXIT_ERR
        if rc != 3:
            item["status"] = "failed"; item["note"] = last_line(out) or f"종료코드 {rc}"; self.save()
            self.status(f"승인 묶음을 만들 수 없어요: {name} — {item['note']}")
            return EXIT_ERR
        # rc 3: 승인 카드
        edir = self.ep_dir(item)
        req = json.loads((edir / "approval_request.json").read_text(encoding="utf-8"))
        h8 = req["bundle_hash"][:8]
        cid = self.card_id(f"script-{item['topic']}-{h8}")
        card = self.hq.get_approval(cid)
        if card is None:
            rel = f"episodes/{item['topic']}-{item['lang']}"
            body = [f"블록 {req.get('blocks')}개 · 예상 유료 호출 {req.get('hf_calls_estimate')}회 (한도 {req.get('cap')})",
                    f"검토본: {rel}/review.html"] + [f"- {c}" for c in req.get("checklist", [])]
            self.hq.post_approval({
                "id": cid, "teamId": self.team, "title": f"대본 승인: {req.get('title', '')}",
                "body": "\n".join(body), "options": ["승인", "반려"],
                "subjectHash": req["bundle_hash"], "expiresInMinutes": EXPIRES,
            })
            if item["status"] != "scripted":
                item["status"] = "scripted"
            self.save()
            self.status(f"대본 승인을 요청했어요: {name}")
            return EXIT_WAIT
        decision = card.get("decision")
        if decision is None:
            self.status(f"대본 승인을 기다려요: {name}")
            return EXIT_WAIT
        if decision == "승인":
            (edir / "APPROVE_SCRIPT").write_text(f"회장 {h8}\n", encoding="utf-8")
            rc2, out2 = self.approve_script(item)
            if rc2 == 0:
                return self.mark_approved(item, name)
            item["status"] = "failed"; item["note"] = last_line(out2) or f"종료코드 {rc2}"; self.save()
            self.status(f"대본 승인 반영 실패: {name} — {item['note']}")
            return EXIT_ERR
        item["status"] = "rejected"; self.save()
        self.status(f"대본이 반려됐어요: {name}")
        return EXIT_OK


def main():
    hq = HQClient(os.environ.get("HQ_URL", "http://127.0.0.1:7777"), os.environ.get("HQ_TOKEN", ""))
    team = Team(hq, subprocess_runner(ROOT, ROOT / "logs" / "hq_team.log"), root=ROOT)
    return team.main()


if __name__ == "__main__":
    sys.exit(main())
