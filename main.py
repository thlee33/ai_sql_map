# main.py (Render.com 배포용 - 'restaurants' 유의어 추가)
import os
import psycopg
import google.generativeai as genai
import json 
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

# .env 파일 로드 (로컬 테스트용. Render.com에서는 이 파일 안 씀)
load_dotenv()

# --- 1. API 키 설정 (환경 변수에서 읽기) ---
try:
    genai.configure(api_key=os.environ.get("GOOGLE_API_KEY"))
except KeyError:
    print("❌ 에러: GOOGLE_API_KEY 환경 변수가 없습니다.")
except Exception as e:
    print(f"❌ Gemini 설정 에러: {e}")

# --- 2. DB 접속 정보 (환경 변수에서 읽기) ---
DB_HOST = os.environ.get("DB_HOST")
DB_PORT = os.environ.get("DB_PORT", "5432") # 기본값 5432
DB_NAME = os.environ.get("DB_NAME")
DB_USER = os.environ.get("DB_USER")
DB_PASS = os.environ.get("DB_PASS")

# --- [수정] 3. 'restaurants' 테이블 스키마 추가 ---
DATABASE_SCHEMA = """
[데이터베이스 스키마]
1.  buildings (서울시 건물)
    - "BJDONG_NM" (TEXT): 법정동명 (예: '녹번동')
    - "BLD_NM" (TEXT): 건물명
    - "USE_APR_DAY" (TEXT): 사용승인일 (예: '19900101')
    - "MAIN_PURPS_CD_NM" (TEXT): 건물 주용도 (예: '단독주택', '아파트')
    - geom (GEOMETRY(Point, 4326)): 위치 (EPSG:4326)

2.  subway_stations (서울시 지하철역)
    - "kor_sta_nm" (TEXT): 역이름 (예: '녹번')
    - geom (GEOMETRY(Point, 4326)): 위치 (EPSG:4326)

3.  restaurant (서울시 음식점)
    - "사업장명" (TEXT): 가게 이름 (예: '부어치킨')
    - "업태구분명" (TEXT): 업종 (예: '한식', '중식', '분식', '일반음식점')
    - "소재지전체주소" (TEXT): 주소
    - "영업상태명" (TEXT): 영업 상태 (예: '영업/정상')
    - geom (GEOMETRY(Point, 4326)): 위치 (EPSG:4326)

[PostGIS 주요 함수]
* [중요!] 모든 거리/미터(meters) 단위 계산은 `geography` 타입으로 변환해야 합니다.
* ST_DWithin (거리 내 검색): `ST_DWithin(a.geom::geography, b.geom::geography, 500)`
* ST_Buffer (반경 영역): `ST_Buffer(geom::geography, 50)::geometry`
"""
# --- [수정 끝] ---

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class VoiceQuery(BaseModel):
    text: str

def execute_postgis_query(sql_query: str):
    if not all([DB_HOST, DB_NAME, DB_USER, DB_PASS]):
        print("❌ 쿼리 실행 에러: DB 환경 변수가 설정되지 않았습니다.")
        return {"error": "서버의 데이터베이스 연결 정보가 설정되지 않았습니다.", "query": sql_query}
        
    conn_info = f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} user={DB_USER} password={DB_PASS}"
    try:
        with psycopg.connect(conn_info) as conn:
            with conn.cursor() as cur:
                geojson_query = f"""
                WITH analysis_result AS (
                    {sql_query} 
                )
                SELECT json_build_object(
                    'type', 'FeatureCollection',
                    'features', json_agg(
                        json_build_object(
                            'type', 'Feature',
                            'geometry', ST_AsGeoJSON(geom)::json,
                            'properties', row_to_json(analysis_result)::jsonb - 'geom'
                        )
                    )
                )
                FROM analysis_result
                WHERE geom IS NOT NULL;
                """
                cur.execute(geojson_query)
                result = cur.fetchone()
                if result and result[0]:
                    return result[0]
                else:
                    return {"type": "FeatureCollection", "features": []}
    except Exception as e:
        print(f"❌ 쿼리 실행 에러: {e}")
        return {"error": str(e), "query": sql_query}

# --- [수정] ---
def get_llm_response(user_question: str):
    
    safety_settings = {
        'HATE_SPEECH': 'BLOCK_NONE',
        'HARASSMENT': 'BLOCK_NONE',
        'SEXUALLY_EXPLICIT': 'BLOCK_NONE'
    }

    model = genai.GenerativeModel(
        # [수정!] Flash 모델로 변경 (속도 향상)
        model_name='models/gemini-flash-latest',
        system_instruction=f"""
        당신은 최고의 GIS 전문가이자 범용 AI 비서입니다.
        사용자의 질문을 받고, [데이터베이스 스키마]를 참고하여 질문의 의도를 3가지로 분류합니다.
        
        [규칙]
        1.  **공간 분석/지도 표시 질문 (SPATIAL_QUERY)**:
            - "건물 찾아줘", "녹번역 주변", "500미터 이내", "맛집" 등 지도에 표시해야 하는 질문.
            - 반드시 PostGIS SQL 쿼리를 생성해야 합니다.
            - [중요!] 팝업에 모든 속성을 표시할 수 있도록, 원본 테이블의 **모든 컬럼을 선택 (`SELECT * ...`)** 해야 합니다.
            - [중요!] 지도 시각화를 위해 `data_type` 컬럼을 꼭 포함해야 합니다.
            
            # --- [수정] 새 테이블(restaurants) 예시 추가 ---
            - (일반 건물 조회): `SELECT *, 'building' as data_type FROM buildings...`
            - (지하철역 조회): `SELECT *, 'station' as data_type FROM subway_stations...`
            - (음식점 조회): "맛집", "음식점", "한식", "중식", "분식" 등은 `restaurants` 테이블을 사용합니다.
            - (음식점 필터링): `WHERE "업태구분명" = '한식'` 또는 `WHERE "업태구분명" = '중식'` 또는 `WHERE "업태구분명" = '분식'` 또는 `WHERE "업태구분명" = '일반음식점'`을 사용하세요.
            - "카페"는 이 데이터에 없다고 `GENERAL_ANSWER`로 응답하세요.
            - (예: "녹번역 300m 이내 한식 맛집"): `SELECT T1.*, 'restaurant' AS data_type FROM restaurants AS T1 JOIN subway_stations AS T2 ON ST_DWithin(T1.geom::geography, T2.geom::geography, 300) WHERE T2."kor_sta_nm" = '녹번' AND T1."업태구분명" = '한식'`
            - (예: "녹번역 근처 음식점"): `SELECT T1.*, 'restaurant' AS data_type FROM restaurants AS T1 JOIN subway_stations AS T2 ON ST_DWithin(T1.geom::geography, T2.geom::geography, 500) WHERE T2."kor_sta_nm" = '녹번' `
            - (단계구분도): "10년 단위로" 같은 요청 시, `data_type` 컬럼에 'building'이 아닌 **분류 값**을 넣어야 합니다.
            - (복합 쿼리): "A를 그리고 B를 찾아줘" 같은 요청 시, `UNION ALL`을 사용해 두 쿼리를 합쳐야 합니다. **(컬럼 개수와 순서를 정확히 맞춰야 합니다!)**
            - (복합 쿼리 예): `SELECT *, 'building' AS data_type FROM buildings WHERE NOT ST_DWithin(...) UNION ALL SELECT NULL::integer AS gid, NULL AS "BJDONG_NM", 'search_area' AS "BLD_NM", ... (컬럼 개수 맞추기) ... , ST_Buffer(...) AS geom, 'search_area' AS data_type FROM subway_stations ...`
            # --- [수정 끝] ---
            
            - 응답 형식: {{"type": "SPATIAL_QUERY", "content": "SELECT ..."}}

        2.  **클라이언트 제어 명령 (CLIENT_COMMAND)**:
            - "지도 확대/축소", "이동", "지도 스타일 변경", "3D 뷰" 등 **지도 자체를 조작**하는 명령.
            - (지도 조작): `ZOOM_IN`, `ZOOM_OUT`, `PAN_TO_BASE`
            - (지도 이동): `PAN_EAST`, `PAN_WEST`, `PAN_NORTH`, `PAN_SOUTH`
            - (시점 변경): `SET_PITCH_3D`, `SET_PITCH_2D`
            - (지도 스타일): `SET_STYLE_STREETS`, `SET_STYLE_DARK`, `SET_STYLE_SATELLITE`
            - 응답 형식: {{"type": "CLIENT_COMMAND", "content": "ZOOM_OUT"}}

        3.  **일반/메타데이터 질문 (GENERAL_ANSWER)**:
            - "네가 가진 데이터 목록 보여줘", "PostGIS가 뭐야?" 등.
            - SQL을 생성하면 안 됩니다.
            - 응답 형식: {{"type": "GENERAL_ANSWER", "content": "제가 가진 데이터는..."}}

        4.  오직 JSON 객체 하나만 응답해야 합니다. (설명, 마크다운 ```json ... ``` 금지)
        5.  만약 질문을 분류하기 애매하다면, 무조건 {{"type": "GENERAL_ANSWER", "content": "질문을 이해하지 못했습니다."}} 를 반환하십시오.
        6.  절대로 빈 문자열이나 null을 반환하지 마십시오.

        {DATABASE_SCHEMA}
        """,
        safety_settings=safety_settings
    )

    print(f"--- Gemini에게 보낼 질문: {user_question} ---")
    try:
        response = model.generate_content(user_question)
        print(f"--- Gemini가 생성한 JSON 응답 (Raw) ---")
        
        if not response.parts:
            print("❌ Gemini 응답이 비어있습니다 (안전 필터에 의해 차단됨).")
            return {"type": "GENERAL_ANSWER", "content": "AI가 응답을 거부했습니다. (안전 필터)"}

        response_text = response.parts[0].text
        print(response_text) # (마크다운 포함된 원본 텍스트)
        
        # JSON 파싱 로직 (이전 수정본과 동일)
        start_index = response_text.find('{')
        end_index = response_text.rfind('}')
        
        if start_index != -1 and end_index != -1 and end_index > start_index:
            json_string = response_text[start_index:end_index+1]
            print(f"--- 파싱할 JSON 문자열 ---")
            print(json_string)
            return json.loads(json_string)
        else:
            print("❌ AI 응답에서 JSON 객체를 찾을 수 없습니다.")
            return {"type": "GENERAL_ANSWER", "content": "AI가 유효한 JSON을 반환하지 않았습니다."}

    except Exception as e:
        print(f"❌ Gemini API 또는 JSON 파싱 에러: {e}")
        return {"type": "GENERAL_ANSWER", "content": f"AI 응답 처리 중 오류가 발생했습니다: {e}"}

# (@app.post("/analyze") ... 이하 파일 하단은 기존과 동일)
@app.post("/analyze")
async def analyze_voice_query(query: VoiceQuery):
    
    llm_response = get_llm_response(query.text)
    
    response_type = llm_response.get("type")
    response_content = llm_response.get("content")

    if response_type == "SPATIAL_QUERY":
        if not response_content:
            return {"error": "LLM이 SQL을 생성하지 못했습니다."}
        
        cleaned_sql = response_content.strip().rstrip(';')
        geojson_result = execute_postgis_query(cleaned_sql)
        return geojson_result

    elif response_type == "CLIENT_COMMAND":
        return {"type": "CLIENT_COMMAND", "content": response_content}
    
    elif response_type == "GENERAL_ANSWER":
        return {"answer_text": response_content}
    
    else:
        error_message = llm_response.get("content", "알 수 없는 오류")
        return {"answer_text": f"오류가 발생했습니다: {error_message}"}