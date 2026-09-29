# SETUP — 사용자가 직접 해야 하는 것 (2026-09-17 기준)

Claude가 대신 못 하는 것만 모았다. **단계별로 필요한 것만** 하면 된다. 각 항목에 "어디에 넣는지"를 적었다.

| 단계 | 필요한 것 | 지금 상태 |
|---|---|---|
| 0. 무비용(리서치·대본·정책 검사) | 없음 — Claude Max 구독의 `claude -p` 사용 | ✅ 이미 돌아감 |
| 1. 유료 2블록 테스트(smoke) / 전체 제작 | Higgsfield 결제 + 로그인 + 워크스페이스 + 음성 ID | ❌ 미완 |
| 2. 업로드(비공개) | YouTube OAuth 자격증명 | ❌ 미완 |
| 3. 폰에서 QA 승인 (선택) | Telegram 봇 | 선택 (없으면 파일 승인) |
| 4. 상장사 공시 자동 수집 (선택) | OpenDART 키 | 선택 |
| 5. 법·회사 규정 | 취업규칙 겸업 조항 확인, 면세사업자 등록 수용 여부 | 결정 필요 (기획서 §E) |

---

## 1. Higgsfield (유료 단계 전 필수)

1. **결제**: https://higgsfield.ai/pricing 에서 플랜 선택. 첫 2블록 테스트는 Starter 로도 되지만, 스킬 문서상 Seedance 등 프리미엄 영상 모델은 Plus 이상. 실가격은 결제창에서 확인(공식 페이지가 프로그램으로 안 읽혀 문서에 가격을 못 박지 못했음).
2. **로그인** (브라우저 OAuth라 사용자만 가능). Claude Code 프롬프트에 `!` 붙여 실행:
   ```
   ! higgsfield auth login
   ```
3. **워크스페이스 선택**:
   ```
   ! higgsfield workspace list --json
   ! hf workspace set <workspace_id>
   ```
4. 확인: `! higgsfield account status --json` 이 오류 없이 나오면 됨. 이후는 Claude가 진행:
   - 한국어 음성 후보를 `higgsfield voices list --json` 로 뽑아 샘플 3개 생성 → 사용자가 하나 고르면 `config/pipeline.yaml` → `languages.ko.voice_id` 에 고정
   - 스타일 키 샘플 3장 생성 → 하나 고르면 `style.style_key_job_id` 에 고정

## 2. YouTube 업로드 (비공개 업로드 테스트 전 필수)

1. https://console.cloud.google.com → 프로젝트 새로 만들기(예: `side-content-engine`)
2. **API 및 서비스 → 라이브러리 → "YouTube Data API v3" 사용 설정**
3. **OAuth 동의 화면**: 외부(External), 앱 이름 아무거나, 테스트 사용자에 본인 Gmail(kimddong8.1213@gmail.com) 추가, 범위에 `.../auth/youtube.upload`, `.../auth/youtube` 추가
4. **사용자 인증 정보 → OAuth 클라이언트 ID → 데스크톱 앱** → JSON 다운로드
5. 파일을 `pipeline/config/yt_client_secret.json` 로 저장 (gitignore 됨)
6. `config/pipeline.yaml` → `publish.youtube.enabled: true` (private_only 는 true 유지)
7. 첫 업로드 실행 때 브라우저가 한 번 열려 동의 → 이후 토큰은 `config/yt_token.json` 에 저장돼 무인 갱신됨

**주의**: 심사 전 API 프로젝트로 올린 영상은 **비공개 고정**(YouTube 공식 제한). 공개하려면 Google Cloud 콘솔에서 "YouTube API 준수 감사(compliance audit)" 신청이 필요 — 채널·앱 설명을 요구하니 1편이 만들어진 뒤 신청하는 게 맞음. 그 전까지는 Studio에서 직접 공개 전환도 막힐 수 있음.

## 3-A. Discord QA 봇 (선택 — 폰에서 승인, 텔레그램 대신)

1. https://discord.com/developers/applications → New Application → **Bot** 탭 → Reset Token → 토큰 복사. 같은 탭에서 **MESSAGE CONTENT INTENT** 켜기(메시지 본문을 읽어야 `/approve` 인식).
2. **OAuth2 → URL Generator**: scope `bot`, 권한 `Send Messages`, `Attach Files`, `Read Message History` → 생성된 URL로 내 개인 서버(없으면 새로 하나)에 봇 초대.
3. 검토용 채널 하나 만들고, Discord 설정 → 고급 → **개발자 모드** 켠 뒤 채널 우클릭 → **채널 ID 복사**, 내 프로필 우클릭 → **사용자 ID 복사**.
4. `config/secrets.env`:
   ```
   DISCORD_BOT_TOKEN=...
   DISCORD_CHANNEL_ID=...
   ```
5. `config/pipeline.yaml` → `qa.mode: discord`, `qa.discord_allowed_user_ids: [<내 사용자 ID>]`
6. 동작: 봇이 채널에 제목·체크리스트·프레임 3장·대본 json·영상(10MB 이하일 때)을 올림 → 폰에서 `/approve pasteur-ko <해시8자리>` 입력 → 승인 기록. 영상이 10MB를 넘으면 로컬 경로만 표시(부스트 없는 서버 봇 업로드 한도).

## 3-B. Telegram QA 봇 (선택 — Discord 대신)

1. 텔레그램에서 `@BotFather` → `/newbot` → 토큰 복사
2. 봇에게 아무 메시지 보낸 뒤 `https://api.telegram.org/bot<토큰>/getUpdates` 열어 `chat.id` 와 `from.id` 확인
3. `config/secrets.env`:
   ```
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_CHAT_ID=...
   ```
4. `config/pipeline.yaml` → `qa.mode: telegram`, `qa.telegram_allowed_user_ids: [<from.id>]` (이 목록의 사용자만 `/approve` 가능)

없으면 기본 `qa.mode: local`: Claude가 검토본을 보여주면 파일 하나 만들어 승인
```
echo '홍길동 <해시8자리>' > episodes/pasteur-ko/APPROVE_QA
```

## 4. OpenDART (선택 — 상장사 편에서 공시 수치를 API로 자동 수집)

1. https://opendart.fss.or.kr → 인증키 신청(무료, 즉시)
2. `config/secrets.env` → `DART_API_KEY=...`
3. `config/pipeline.yaml` → `research.sources.dart: true`
파스퇴르(비상장)처럼 공시가 없는 회사는 필요 없음. 웅진·팬택·한진해운·대우 편부터 의미 있음.

## 5. 결정 사항 (기획서 09 §E)

- **취업규칙**: 겸업 금지·사전 승인 조항이 있는지 확인. 있으면 시작 전에 처리.
- **면세사업자 등록**: 계속·반복 광고수익은 국세청이 등록(면세, 부가세 없음, 무료)을 안내함. "수익화 신청 시점에 등록" 을 수용할지 결정. 불수용이면 유료 제작 전에 멈추는 게 맞음.
- **주간 시간 상한**(4/6/8h) → KR 편수 결정. **월 예산 상한** → Higgsfield 플랜.

## 셋업 완료 확인
```
cd pipeline && source .venv/bin/activate
python scripts/00_check_env.py --paid          # 유료 단계 요건
python scripts/00_check_env.py --paid --publish # 업로드 요건까지
```
"환경 준비 완료" 가 나오면 Claude가 `run_episode.py --smoke --until 6` 로 2블록 테스트를 돌린다.
