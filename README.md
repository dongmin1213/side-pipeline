# content-engine v0.2 — 자동 콘텐츠 파이프라인

리서치 → 대본(정책 검사) → **사람 승인** → 음성 → 클립 → 조립·자막 → **사람 QA** → 업로드(비공개).

## 단계 (`run_episode.py` 순서)
| # | 스크립트 | 산출물 (`episodes/<topic>-<lang>[-smoke]/`) | 비용 | 게이트 |
|---|---|---|---|---|
| 0 | `00_check_env.py [--paid] [--publish]` | — | 0 | 실행기가 항상 먼저 실행 |
| 1 | `10_research.py` | `facts.json` — 수치마다 **실제로 열어본 URL** + 날짜. URL 없는 수치는 제거 | 0 (ChatGPT 구독, `codex exec` + 웹 검색) | 출처 < 4 또는 수치 < 8 이면 중단 |
| 1.5 | `15_verify_sources.py` | `source_report.json` — 모든 출처 URL GET 검사, dead 출처 의존 수치 제거 | 0 | 살아있는 출처 < 4 이면 중단 |
| 2 | `20_script.py` | `script.json`(10초 블록 N개, fact_refs), `meta.json`, `policy_check.json`. **청크 생성**(개요 1회 + 12블록 단위, `script_parts.json` 캐시) | 0 | 스키마+정책 실패 시 해당 청크만 재작성, 그래도 실패면 **exit 2** |
| 2.2 | `22_patch_facts.py` | 리뷰어 팩트체크 정정(`config/corrections/<topic>.json`) → facts 주석 + 걸리는 청크만 재작성 | 0 | 실행 후 `20_script.py` 재실행으로 재검사 |
| 3 | `25_approve_script.py` | `approval_request.json` → 사람이 `APPROVE_SCRIPT` 에 `<이름> <묶음해시8자리>` | 0 | **유료 단계 진입 게이트** — 묶음 = 대본+meta+음성ID+스타일키+cap+smoke |
| 9 | `90_review_page.py` | `review.html` — 검토본 페이지(대본·수치·출처 상태·정정·승인 명령) → Artifact 발행 | 0 | 승인 전 사람 검토 자료 |
| 4 | `40_voice.py` | `voice.json` — Seed Audio, ffprobe 길이 검증 | 크레딧 | 승인 필요 |
| 5 | `30_visuals.py` | `visuals.json` — 스타일 키 + 클립(블록 상태 pending/running/succeeded/failed) | 크레딧 | 승인 + 음성 완료 필요(스킬 Phase 4→5) |
| 6 | `50_assemble.py` | `final_raw.mp4` → `final.mp4`(자막) → `short.mp4`(세로). 기본 `assembly.mode: local_trim`(블록 = 오디오+0.4초, ffmpeg concat, 크레딧 0) / `server`(explainer_video 고정 10초, 크레딧 1) | 0 또는 1 | 1:1 매핑·길이·Shorts 규격 검증 |
| 7 | `60_qa.py` | `qa_request.json` → `qa.json`(검토본 해시·승인자) | 0 | local: `APPROVE_QA` 파일 / telegram: 허용 user id + 해시 |
| 8 | `70_publish.py` | `publish.json` — 자산별 체크포인트, **비공개 고정** | 0 | qa 해시 = 현재 산출물 |

모든 유료 호출은 `ledger.jsonl`(append-only)에 제출·완료·실패·초·토큰이 남는다 → 기획서 §B3 원가 실측.

## 실행 순서 (리뷰 권장: 무비용 → 2블록 계약 테스트 → 실측)
```bash
cd pipeline && source .venv/bin/activate
python run_episode.py --topic pasteur --lang ko --until 2          # 무비용: 리서치 + 대본 + 정책 검사
# 대본 검토 → echo '홍길동' > episodes/pasteur-ko/APPROVE_SCRIPT
python run_episode.py --topic pasteur --lang ko --smoke --until 6   # 유료 계약 테스트: 음성 2 → 클립 2 → 조립 1 (cap 6회)
python run_episode.py --topic pasteur --lang ko --from 3 --until 7  # 승인 → 전체 제작 → QA
```

## hq 팀으로 실행
hq 데몬이 `python hq_team.py`를 N분마다 실행한다 (env: `HQ_URL`, `HQ_TOKEN`, `HQ_TEAM`). 상태는 `state/hq_team.json`, 실행 로그는 `logs/hq_team.log`.
- 대기열이 비면 `config/topics_kr.yaml`의 `queued` 주제 중 최대 3개로 **주제 선택 카드**를 올린다 (`보류` → 24시간 뒤 다시 묻기).
- 고른 주제는 무료 단계(`run_episode.py --until 2`: 리서치·대본)를 자동 실행하고, 검토본(`review.html`)을 만든 뒤 `25_approve_script.py` 묶음으로 **대본 승인 카드**를 올린다.
- 카드에서 `승인`하면 `APPROVE_SCRIPT`를 대신 써서 승인 게이트를 통과시킨다. `반려`하면 그 편은 멈춘다.
- 음성·스타일 키가 아직 없으면 `waiting_paid`로 두고 준비가 끝날 때까지 매번 다시 확인한다.
- 종료코드: 0 = 대기/완료, 3 = 승인 대기, 75 = Codex 사용 한도(다음 실행에서 이어서), 그 외 = 오류. 마지막 `STATUS:` 줄이 펫 말풍선에 뜬다.
- hq 사용량 모드가 `normal` 또는 `unobserved`이면 실행한다. Codex가 사용률을 제공하지 않는 상태는 제한으로 간주하지 않으며, `save`·`hold` 또는 알 수 없는 모드에서는 쉰다.
- **유료 단계는 팀이 절대 실행하지 않는다.** 승인 뒤 첫 유료 실행은 직접: `python run_episode.py --topic <id> --lang ko --from 4 --smoke`.

## 준비물 (사용자) — 단계별 상세 절차는 **`SETUP.md`**
| 항목 | 방법 | 상태 |
|---|---|---|
| Higgsfield 로그인·워크스페이스 | `higgsfield auth login` → `hf workspace set <id>` (브라우저 OAuth, 사용자 직접) | **미완** |
| 한국어 음성 선택 | `higgsfield voices list --json` → `config/pipeline.yaml languages.ko.voice_id` | 미완 |
| 스타일 키 | smoke 첫 실행이 샘플 생성 → 마음에 들면 `style.style_key_job_id` 고정 | 미완 |
| Codex CLI | `codex login`으로 ChatGPT 로그인 — API 키 불필요 | 실행 확인 |
| ffmpeg-full | 설치됨(libass 자막, 한글 렌더 확인) — `lib/state.py` 가 PATH 우선 적용 | 완료 |
| YouTube OAuth | Google Cloud 프로젝트 → 데스크톱 앱 자격증명 → `config/yt_client_secret.json`; **API 심사 전엔 비공개 업로드만** | 미완 |
| Telegram(선택) | 봇 토큰·chat id·`qa.telegram_allowed_user_ids`; 없으면 `qa.mode: local` | 선택 |
| DART(선택) | opendart.fss.or.kr 키 → `research.sources.dart: true` (상장사만) | 선택 |

## 아직 안 된 것 (리뷰 10 기준, 정직하게)
- EN human-voice 로컬 조립(I10), D7/D28 성과 측정(M3), YouTube resumable 세션 URI 재개(I13 일부), 네이버 클립·TikTok 수동 게시 체크리스트, 15분 초과 편의 파트 분할 조립.
- **실제 계정으로 end-to-end 유료 실행은 아직 0회.** 첫 실행은 반드시 `--smoke`.

## LLM 실행 설정
- `config/pipeline.yaml`: `llm.backend: codex`, `llm.model: gpt-6-astra`.
- 리서치는 `research.sources.web: true`일 때 Codex의 실시간 웹 검색·페이지 열람을 사용합니다. 대본·팩트 정정은 주어진 자료로만 JSON을 만듭니다.
- CLI는 읽기 전용으로 실행하며 셸 도구·개인 설정·플러그인·훅을 사용하지 않습니다. 결과 파일은 Python 파이프라인이 기록합니다.
- 단독 실행은 현재 Codex 로그인, HQ 팀 실행은 HQ가 제공하는 팀 전용 `CODEX_HOME`을 사용합니다. 별도 API 키는 전달하지 않습니다.
- 원시 응답은 JSON 파싱 전에 `llm_raw/`에 저장합니다. 실패·시간 초과의 부분 출력도 보존합니다. 구독 사용량은 토큰으로 기록하며 API 달러 비용을 추정하지 않습니다.
- 명시적 사용량 한도 오류는 바로 HQ에 반환합니다. 일반 오류는 최대 3회 재시도하고, 각 호출은 기존 900초(리서치)·600초(대본) 시간 제한을 사용합니다. 이전 CLI의 턴 수 제한 설정은 제거했습니다.
- 검증: `.venv/bin/python -m unittest discover -s tests -v`. 실제 팀 샌드박스 통합 시험은 HQ 저장소에서 `HQ_LIVE_PIPELINE=1 node --test --test-timeout=180000 test/unit/team-live.test.ts`.
- CLI 계약: [공식 비대화형 실행 문서](https://learn.chatgpt.com/docs/non-interactive-mode).
