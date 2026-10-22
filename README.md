# AI IDE 규칙 템플릿

AI 코딩 도구가 NH투자증권 Open API 를 **정확하게** 다루도록, 프로젝트에 넣어두는 규칙 파일입니다.
규칙이 없으면 AI 가 필드명·성공코드·환경을 **추측**해서 틀린 코드를 만듭니다.

## 어느 파일을 쓰나요?

| 쓰는 도구 | 넣을 파일 | 프로젝트 내 위치 |
|---|---|---|
| **Antigravity** · OpenAI Codex | `AGENTS.md` | 루트 |
| **Claude Code** | `CLAUDE.md` | 루트 |
| **Cursor** | `cursor/nhplug.mdc` | **`.cursor/rules/nhplug.mdc`** |

> 여러 도구를 쓴다면 해당 파일을 모두 두면 됩니다. 서로 충돌하지 않습니다.

## 설치

```bash
# 프로젝트 폴더에서 (Antigravity·Codex)
curl -O https://raw.githubusercontent.com/PLUG-OpenAPI/nhplug-sdk/main/templates/AGENTS.md

# Claude Code
curl -O https://raw.githubusercontent.com/PLUG-OpenAPI/nhplug-sdk/main/templates/CLAUDE.md

# Cursor
mkdir -p .cursor/rules
curl -o .cursor/rules/nhplug.mdc https://raw.githubusercontent.com/PLUG-OpenAPI/nhplug-sdk/main/templates/cursor/nhplug.mdc
```

Windows PowerShell:

```powershell
iwr -useb https://raw.githubusercontent.com/PLUG-OpenAPI/nhplug-sdk/main/templates/AGENTS.md -OutFile AGENTS.md

New-Item -ItemType Directory -Force .cursor\rules | Out-Null
iwr -useb https://raw.githubusercontent.com/PLUG-OpenAPI/nhplug-sdk/main/templates/cursor/nhplug.mdc -OutFile .cursor\rules\nhplug.mdc
```

> 파일을 브라우저로 열어 복사해 붙여넣어도 됩니다.

## ⚠️ Cursor 사용자 주의

**레거시 `.cursorrules` 파일은 Cursor Agent 모드에서 무시됩니다.**
반드시 **`.cursor/rules/nhplug.mdc`** 경로로 두세요. 예전 방식으로 두면 규칙이 조용히 적용되지 않습니다.

## 무엇이 들어 있나요

세 파일 모두 같은 내용을 각 도구 형식에 맞춰 담았습니다.

- **SDK 우선** — `pip install nhplug` 로 인증·토큰캐시·에러판정을 맡기고, `requests` 로 직접 짜지 않게
- **명세 정본** — 도메인 `llms.txt` · 자산군 `openapi.json` 을 보게 (필드 추측 방지)
- **성공 판정** — SDK 는 판정하지 않습니다(HTTP 200 이면 본문 그대로). `rsp_msg` 를 읽게 하고, `rsp_cd` 코드값 하드코딩을 막습니다
- **계좌구분** — `01`·`02` 운영 / `03` 모의. 첫 계좌를 그대로 쓰지 않게
- **브랜드** — N2 는 세 줄 모두 전환(하나라도 빠지면 조용히 나무로)
- **주문 필드** — `iem_cd` 6자리(A 없음) · `orr_pr` 정수
- **안전** — 키·토큰 미출력 · 기본은 운영이라 실제 체결 · `dry_run` 기본
- **MCP 혼동 방지** — SDK 는 URI 경로, MCP 는 operationId

## 프로젝트에 맞게 고쳐 쓰세요

이 템플릿은 **출발점**입니다. 프로젝트 고유 규칙(폴더 구조·로깅 방식·테스트 규칙)을 아래에 덧붙이면 AI 가 함께 지킵니다.

## 관련 문서

- [Antigravity·Cursor 로 바이브코딩하기](../guides/antigravity.md) — 설치부터 첫 실행까지
- [SDK README](../README.md) · [저장소 AGENTS.md](../AGENTS.md)
- 명세 정본: [llms.txt](https://www.nhplug.com/llms.txt) (N2: [n2plug](https://www.n2plug.com/llms.txt))


## 자산조회 안전 수정 (2026-10-22)

- 원인: `assetStatus.Output_1`에는 ETF 외에 발행어음도 포함된다. 실제 조회에서
  발행어음은 수량이 양수이고 `phs_pr=0`이었다. 이를 주식 평단으로 검증하던 오류를 수정했다.
- 총자산·주문가능금액은 기존 `assetStatus.Output_0`를 유지한다. 주식 보유 수량과
  매입가격은 공식 `/krstock/inquiry/v1/balance`의 전체 페이지에서 검증한다.
  특정 종목/자산명 예외 목록이나 현재가·평가금액을 이용한 평단 추정은 사용하지 않는다.
- 공식 필드: `phs_pr` = 매입가격, `itg_bnc_qty` = 통합잔고수량.
  자산현황의 가격 스키마는 number(15.2), 주식잔고의 가격 스키마는 integer(18)이다.
  SDK 0.4.1은 응답을 그대로 반환한다. 숫자 문자열은 파싱하지만 null·비유한값·음수·0원은
  양수 보유 주식의 유효한 매입가격으로 인정하지 않는다. 다른 가격 필드로 대체하지 않는다.
- 잔고 조회 실패·불완전한 연속조회·중복잔고·양수 보유의 평단 불명확 시 전체 주문을 차단한다.
  빈 주식잔고는 최종 총매수금액과 총평가금액이 모두 0일 때만 인정한다.
- 장외에는 계좌·시세·주문 조회 전에 대기하며 보유·평단·pending을 보존한다.
  15:35 예약 보고는 저장 기록임을 표시하고 실계좌/체결 확인으로 사용하지 않는다.
  장중 조회 오류는 실패 종료를 유지한다. 전략 B, 32주 상한 및 운영 workflow는 변경하지 않는다.
- `scripts/diagnose_assets.py`와 별도 검증 workflow는 조회만 수행하며 전략을 실행하거나
  상태파일을 저장하지 않는다. 원본 응답·자격증명·계좌번호·평단 숫자는 출력하지 않는다.
- 명세에는 계좌유형별 별도 응답 스키마, 야간 평단 누락 규칙, 숫자 null 허용 규칙이 없다.
  이번 야간 실조회는 성공했으며 오류 원인은 시간대 제한이 아닌 발행어음 행의 오해석이었다.
  실패 당시 원본 응답은 남아 있지 않아 당시 전체 내용 자체를 복원한 것은 아니다.

공식 명세: https://www.nhplug.com/openapi-docs/krstock/openapi.json
(261015 기준), https://www.nhplug.com/llms-full.txt
SDK: https://github.com/PLUG-OpenAPI/nhplug-sdk
