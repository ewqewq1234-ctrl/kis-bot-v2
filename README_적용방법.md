# 나무증권 KODEX 200 전략 B — 안전 수정본

**참고 원본:** PLUG-OpenAPI/nhplug-mcp, PLUG-OpenAPI/nhplug-sdk 공식 규칙 및 국내주식 OpenAPI 명세. 기존 매매전략(10/20/30/40% 4단계, -2.8%, TS +2%/-1%, 탈출 +0.05%)은 유지하고, **주문 안정성** 위주로 고쳤습니다.

## 포함된 파일

- `nh_client.py`: 공식 `nhplug` SDK 단일 호출, 계좌·주문 응답 검증, 당일 체결/미체결 조회, 주문번호 확인.
- `run_cloud.py`: 실제 보유 32주를 4단계로 복구, 미체결·부분체결·잔고불일치 시 주문 중단, 주문접수와 완전체결을 구분, 32주 상한 검사.
- `.github/workflows/trade.yml`: `git diff --cached` 저장 버그 해결, 실패 시에도 상태 저장 시도, 중복 스케줄 실행 직렬화.
- `requirements.txt`: 공식 SDK 설치.
- `tests/test_safety.py`: 실제 NH API에 접속하지 않는 테스트 (11건).

**기존 `state.json` 및 `tokens.json`을 이 ZIP으로 덮어쓰지 마세요.**

## 적용 전 필수 순서

1. GitHub Actions의 기존 봇 워크플로를 잠시 비활성화합니다. 보유주식 32주는 그대로 유지됩니다. 현재 미체결 주문이 있는지 앱에서 확인합니다.
2. 기존 저장소 파일 4개(`nh_client.py`, `run_cloud.py`, `.github/workflows/trade.yml`, `requirements.txt`)를 새 버전으로 교체하고, `tests/test_safety.py`를 추가합니다. GitHub에서 새로운 브랜치를 만든 뒤 PR로 검토해 적용하는 것이 권장됩니다.
3. 기존 시크릿 `NH_APP_KEY`, `NH_APP_SECRET`, `NH_ACCOUNT_NO`, 텔레그램 2종은 그대로 사용합니다. 코드에 인증값을 직접 넣지 않습니다.
4. 검증 전에는 GitHub **Settings → Secrets and variables → Actions → Variables**에서 `ENABLE_LIVE_ORDERS=NO`로 유지합니다. 값이 없어도 기본값 NO입니다.
5. 테스트 실행: `python -m pip install -r requirements.txt` 및 `python -m unittest discover -s tests -v`.
6. GitHub Actions를 수동 실행해 **실제 잔고 32주 / 4단계 동기화**, 주문 접수 없음, `state.json` 자동 커밋 성공을 확인합니다.
7. 모의투자 또는 주문 없는 검증을 거친 뒤에만 `ENABLE_LIVE_ORDERS=YES`로 변경하고 운영 스케줄을 활성화합니다.

## 달라지는 안전 규칙

- 당일 같은 종목의 주문 기록이 1건이라도 있으면 그날은 추가 주문을 하지 않습니다. 전략적 거래 횟수가 줄어들 수 있습니다.
- 주문 전 상태를 `pending_order`로 기록하고, 다음 조회에서 실제 잔고가 **정확히 예상 수량으로 변경**돼야 단계가 확정됩니다.
- 미체결·부분체결·조회 오류·상태 불일치 시에는 신규 주문을 멈추고 수동 확인을 요구합니다.
- 보유 32주면 4단계로 동기화하며, 32주 초과 포지션과 32주를 넘길 신규 매수를 막습니다.
- 탈출 조건 +0.05%는 시장가 매도 접수 조건일 뿐 **순손익 0원이나 수익을 보장하지 않습니다.**
- 가격·주문 감시는 여전히 한 시간 간격입니다. 즉시 손절 시스템이 아닙니다.
- `nhplug` SDK는 실행 중 `~/.nhplug/` 파일에 토큰을 캐시합니다. 다만 GitHub Actions 러너는 매번 초기화되므로 실행 간 재사용은 **아직 해결되지 않았습니다**. API 키로 접근 가능한 토큰을 공개 저장소 Actions 캐시에 평문 보관하지 않기 위해 이 버전은 토큰을 GitHub 캐시에 올리지 않습니다. 별도 암호화된 캐시 설계가 필요합니다.

## 실계좌 코드 검증 범위

공식 `openapi.json`의 `assetStatus`, `currentExecution`, `cashBuy`, `cashSell`, `dailyOrderExecution` 요청/응답 필드에 맞춰 작성했습니다. **실제 NH 계좌 API 응답과 실주문/취소/미체결까지는 실행하지 않았습니다.** 일부 API에서 `Output_1` 또는 `Output_0`이 비어 있으면 안전을 위해 거래를 차단할 수 있습니다. 운영 전에 로그와 모의계좌로 검증해 주세요.

GitHub 연결에서 쓰기 권한이 403으로 거부되어 ChatGPT가 원본 GitHub 저장소에 직접 커밋하지는 못했습니다. ZIP 내용만 다운로드용으로 작성했습니다.
