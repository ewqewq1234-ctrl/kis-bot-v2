def get_current_price(self, ticker="069500"):
        """
        나무증권 공식 규격: /krstock/quote/v1/currentPrice (POST)
        KODEX 200 종목코드: 069500
        """
        url = f"{self.BASE_URL}/krstock/quote/v1/currentPrice"
        
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecretkey": self.app_secret
        }
        
        # 나무증권 샘플 규격 반영 (Input_0 구조)
        payload = {
            "Input_0": {
                "market_cd": "KRX",
                "iem_cd": ticker
            }
        }
        
        res = requests.post(url, headers=headers, json=payload, timeout=5)
        
        if res.status_code != 200:
            print(f"[시세 조회 실패] HTTP {res.status_code}: {res.text}")
            res.raise_for_status()
            
        data = res.json()
        
        # 응답 블록에서 현재가 필드 추출
        # 나무증권 응답은 보통 Output_0 또는 data 블록 안에 들어옵니다.
        output = data.get("Output_0") or data.get("output") or data.get("output1") or data
        if isinstance(output, list) and len(output) > 0:
            output = output[0]
            
        # 가격 필드명(prc, now_prc, stck_prpr, cur_prc 등) 유연하게 매핑
        price = (
            output.get("cur_prc") or 
            output.get("prc") or 
            output.get("now_prc") or 
            output.get("stck_prpr") or 
            output.get("clpr")
        )
        
        if price is None:
            # 필드명을 확인할 수 있도록 전체 응답 출력
            print(f"[응답 확인용] 전체 데이터: {data}")
            raise KeyError("현재가 필드를 찾을 수 없습니다. 응답 구조를 확인해주세요.")
            
        return float(str(price).replace(",", "").strip())
