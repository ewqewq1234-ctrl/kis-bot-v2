def get_current_price(self, ticker="069500"):
        """
        나무증권(NH) 실시간 현재가 조회
        나무증권 종목코드는 보통 6자리 앞에 'KR7'이 붙거나 순수 6자리(A069500 등)를 사용합니다.
        가장 일반적인 표준 단축코드(A+6자리 또는 6자리) 대응
        """
        # 나무증권 시세 조회 엔드포인트
        url = f"{self.BASE_URL}/stock/quote/current-price"  # 포털 가이드의 시세 URL
        
        # 헤더 설정
        headers = self._get_headers(tr_id="REALTIME_QUOTE")
        
        # 종목코드 포맷팅 (A069500 형태가 기본 규격인 경우가 많음)
        symbol = f"A{ticker}" if not ticker.startswith("A") else ticker
        
        params = {
            "IsuNo": symbol  # 또는 가이드상 종목코드 필드명
        }
        
        res = requests.get(url, headers=headers, params=params, timeout=5)
        
        # 만약 해당 엔드포인트가 다를 경우 상세 응답 디버깅 출력
        if res.status_code != 200:
            print(f"[API 응답 에러] 상태코드: {res.status_code}, 본문: {res.text}")
            res.raise_for_status()
            
        data = res.json()
        
        # 가이드 응답 필드 중 현재가 추출 (보통 CurPrc, now_prc, stck_prpr 중 하나)
        # 여러 키 형태에 유연하게 대응
        output = data.get("output") or data.get("OutBlock") or data
        price = output.get("CurPrc") or output.get("now_prc") or output.get("stck_prpr") or output.get("close")
        
        return float(str(price).replace(",", ""))
