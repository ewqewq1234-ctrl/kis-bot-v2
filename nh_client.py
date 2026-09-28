import os
import json
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime, timezone, timedelta

class NHInvestClient:
    BASE_URL = "https://api.nhplug.com:8443"

    def __init__(self, app_key=None, app_secret=None):
        self.app_key = app_key or os.getenv("NH_APP_KEY")
        self.app_secret = app_secret or os.getenv("NH_APP_SECRET")
        self.token_file = "tokens.json"
        self.access_token = None
        
        # 네트워크 지연 및 일시적 서버 오류 대응 세션 설정
        self.session = requests.Session()
        retries = Retry(
            total=3,
            backoff_factor=1,  # 재시도 대기 시간: 1초, 2초, 4초
            status_forcelist=[500, 502, 503, 504],
            raise_on_status=False
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self._load_or_refresh_token()

    def _load_or_refresh_token(self):
        """1일 1회 발급 원칙: 당일 발급 내역이 유효하면 재발급 생략"""
        kst = timezone(timedelta(hours=9))
        today_str = datetime.now(kst).strftime("%Y-%m-%d")

        if os.path.exists(self.token_file):
            try:
                with open(self.token_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    issued_date = data.get("issued_date", "")
                    expires_at_str = data.get("expires_at", "2000-01-01T00:00:00")
                    expires_at = datetime.fromisoformat(expires_at_str)

                    if issued_date == today_str and datetime.now() < expires_at - timedelta(minutes=30):
                        self.access_token = data.get("access_token")
                        return
            except Exception:
                pass
        self._issue_token()

    def _issue_token(self):
        """나무증권 OAuth2 접근 토큰 신규 발급"""
        url = f"{self.BASE_URL}/oauth2/token"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded"
        }
        data = {
            "appkey": self.app_key,
            "appsecretkey": self.app_secret,
            "grant_type": "client_credentials",
            "scope": "oob"
        }

        try:
            res = self.session.post(url, headers=headers, data=data, timeout=10)
            if res.status_code != 200:
                print(f"[토큰 발급 실패] HTTP {res.status_code} | 본문: {res.text}")
                res.raise_for_status()

            token_info = res.json()
            self.access_token = token_info.get("access_token")
            expires_in = int(token_info.get("expires_in", 86400))
            expires_at = datetime.now() + timedelta(seconds=expires_in)

            kst = timezone(timedelta(hours=9))
            today_str = datetime.now(kst).strftime("%Y-%m-%d")

            with open(self.token_file, "w", encoding="utf-8") as f:
                json.dump({
                    "access_token": self.access_token,
                    "expires_at": expires_at.isoformat(),
                    "issued_date": today_str
                }, f, indent=2)
            print(f"[토큰 1일 1회 발급 완료] 기준일자: {today_str}")
        except Exception as e:
            print(f"[토큰 발급 중 치명적 오류]: {e}")
            raise

    def get_current_price(self, ticker="069500"):
        """나무증권 현재가 체결추이 조회 (/krstock/quote/v1/currentExecution)"""
        url = f"{self.BASE_URL}/krstock/quote/v1/currentExecution"

        for attempt in range(2):
            headers = {
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {self.access_token}",
                "appkey": self.app_key,
                "appsecretkey": self.app_secret
            }
            payload = {
                "Input_0": {
                    "market_cd": "KRX",
                    "iem_cd": str(ticker),
                    "view_main_yn": "N"
                }
            }
            # timeout을 5 -> 10으로 상향하고 세션 통신 적용
            res =
