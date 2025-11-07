# main.py (Render.com 배포용 - JSON 파싱 버그 수정)
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

# DB 스키마 정보 ('geography' 타입 힌트 포함)
DATABASE_SCHEMA = """
[데이터베이스 스키마]
1.  buildings (건물 테이블)
    - id (INT, Primary Key)
    - address (TEXT): 주소 (예: '녹번동 11-1')
    - build_year (INT): 건축 연도 (예: 1990)
    - geom (GEOMETRY(Point, 4326)): 위치 (EPSG:4326 - 단위: '도')

2.  subway_stations (지하철역 테이블)
    - id (INT, Primary Key)
    - station_name (TEXT): 역 이름 (예: '녹번역')
    - geom (GEOMETRY(Point, 4326)): 위치 (EPSG:4326 - 단위: '도')

[PostGIS 주요 함수]
* [중요!] 모든 거리/미터(meters) 단위 계산은 `geography` 타입으로 변환해야 합니다.
* ST_DWithin (거리 내 검색): `ST_DWithin(geom::geography, (SELECT geom FROM ...)::geography, 500)`
* ST_Buffer (반경 영역): `ST_Buffer(geom::geography, 50)::geometry` (결과는 `::geometry`로 다시 변환)
"""

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
        model_name='models/gemini-pro-latest',
        system_instruction=f"""
        당신은 최고의 GIS 전문가이자 범용 AI 비서입니다.
        사용자의 질문을 받고, [데이터베이스 스키마]를 참고하여 질문의 의도를 3가지로 분류합니다.
        
        [규칙]
        1.  **공간 분석/지도 표시 질문 (SPATIAL_QUERY)**:
            - "건물 찾아줘", "녹번역 주변", "500미터 이내" 등 지도에 표시해야 하는 질문.
            - 반드시 PostGIS SQL 쿼리를 생성해야 합니다.
            - [중요!] 팝업에 모든 속성을 표시할 수 있도록, 원본 테이블의 **모든 컬럼을 선택 (`SELECT * ...`)** 해야 합니다.
            - [중요!] 지도 시각화를 위해 `data_type` 컬럼을 꼭 포함해야 합니다.
              (예: `SELECT *, 'building' as data_type FROM buildings...`)
            - 응답 형식: {{"type": "SPATIAL_QUERY", "content": "SELECT ..."}}

        2.  **클라이언트 제어 명령 (CLIENT_COMMAND)**:
            - "지도 확대/축소", "줌인", "줌아웃", "이동", "위성 지도로 변경", "기본 지도로" 등 **지도 자체를 조작**하는 명령.
            - `content` 필드에 표준화된 명령어를 반환합니다.
            - (예: `ZOOM_IN`, `ZOOM_OUT`, `PAN_TO_BASE`, `SET_STYLE_SATELLITE`, `SET_STYLE_STREETS`)
            # --- [수정] ---
            - (지도 이동 표준 명령어: `PAN_EAST`, `PAN_WEST`, `PAN_NORTH`, `PAN_SOUTH`)
            - 응답 형식: {{"type": "CLIENT_COMMAND", "content": "ZOOM_OUT"}}
            - (예: "지도를 오른쪽으로 이동해줘" 또는 "동쪽으로" -> {{"type": "CLIENT_COMMAND", "content": "PAN_EAST"}})
            - (예: "지도를 위로 이동해줘" 또는 "북쪽으로" -> {{"type": "CLIENT_COMMAND", "content": "PAN_NORTH"}})
            # --- [수정 끝] ---

        3.  **일반/메타데이터 질문 (GENERAL_ANSWER)**:
            - "네가 가진 데이터 목록 보여줘", "PostGIS가 뭐야?" 등.
            - SQL을 생성하면 안 됩니다.
            - 사용자의 질문에 대한 친절한 텍스트 답변을 생성합니다.
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
