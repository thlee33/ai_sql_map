# main.py (최종 수정본 - SELECT * 포함)
import os
import psycopg
import google.generativeai as genai
import json 
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

try:
    genai.configure(api_key=os.environ["GOOGLE_API_KEY"])
except KeyError:
    print("❌ 에러: GOOGLE_API_KEY를 찾을 수 없습니다.")
    exit()

DB_HOST = "localhost"
DB_PORT = "5432"
DB_NAME = "postgres"
DB_USER = "postgres"
DB_PASS = "postgres"

DATABASE_SCHEMA = """
[데이터베이스 스키마]
1.  buildings (건물 테이블)
    - id (INT, Primary Key)
    - address (TEXT): 주소 (예: '녹번동 11-1')
    - build_year (INT): 건축 연도 (예: 1990)
    - geom (GEOMETRY(Point, 4326)): 위치 (EPSG:4326 위경도)

2.  subway_stations (지하철역 테이블)
    - id (INT, Primary Key)
    - station_name (TEXT): 역 이름 (예: '녹번역')
    - geom (GEOMETRY(Point, 4326)): 위치 (EPSG:4326 위경도)

[PostGIS 주요 함수]
* ST_DWithin(geom1, geom2, distance_meters): 거리 내 검색
* ST_Buffer(geom, distance_meters): 영역 생성
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
                print("--- 실행될 GeoJSON 쿼리 ---")
                print(geojson_query)
                cur.execute(geojson_query)
                result = cur.fetchone()
                if result and result[0]:
                    return result[0]
                else:
                    return {"type": "FeatureCollection", "features": []}
    except Exception as e:
        print(f"❌ 쿼리 실행 에러: {e}")
        return {"error": str(e), "query": sql_query}

# --- 2. [수정] LLM 함수 (SELECT * 지시가 핵심) ---
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
        사용자의 질문을 받고, [데이터베이스 스키마]를 참고하여 질문의 의도를 2가지로 분류합니다.
        
        [규칙]
        1.  **공간 분석/지도 표시 질문 (SPATIAL_QUERY)**:
            - "건물 찾아줘", "녹번역 주변", "500미터 이내" 등 지도에 표시해야 하는 질문.
            - 반드시 PostGIS SQL 쿼리를 생성해야 합니다.
            
            - [중요!] 팝업에 모든 속성을 표시할 수 있도록, 원본 테이블의 **모든 컬럼을 선택 (`SELECT * ...`)** 해야 합니다.
            - [중중요!] 지도 시각화를 위해 `data_type` 컬럼을 꼭 포함해야 합니다.
              (예: `SELECT *, 'building' as data_type FROM buildings...`)
              (예: `SELECT *, 'station' as data_type FROM subway_stations...`)
            - (버퍼 생성 시): `SELECT ST_Buffer(...) AS geom, 'search_area' AS data_type ...`
            
            - `geom` 컬럼은 필수입니다. (이미 `*`에 포함되어 있거나 AS geom으로 지정됨)
            - 응답 형식: {{"type": "SPATIAL_QUERY", "content": "SELECT ..."}}

        2.  **일반/메타데이터 질문 (GENERAL_ANSWER)**:
            - "네가 가진 데이터 목록 보여줘", "PostGIS가 뭐야?", "안녕" 등 SQL과 관련 없는 질문.
            - SQL을 생성하면 안 됩니다.
            - 사용자의 질문에 대한 친절한 텍스트 답변을 생성합니다.
            - 응답 형식: {{"type": "GENERAL_ANSWER", "content": "제가 가진 데이터는 건물과 지하철역 정보입니다."}}

        3.  오직 JSON 객체 하나만 응답해야 합니다. (설명, 마크다운 ```json ... ``` 금지)
        4.  만약 질문을 1 또는 2로 분류하기 애매하다면, 무조건 {{"type": "GENERAL_ANSWER", "content": "질문을 이해하지 못했습니다."}} 를 반환하십시오.
        5.  절대로 빈 문자열이나 null을 반환하지 마십시오.

        {DATABASE_SCHEMA}
        """,
        safety_settings=safety_settings
    )

    print(f"--- Gemini에게 보낼 질문: {user_question} ---")
    try:
        response = model.generate_content(user_question)
        print(f"--- Gemini가 생성한 JSON 응답 ---")
        
        if not response.parts:
            print("❌ Gemini 응답이 비어있습니다 (안전 필터에 의해 차단됨).")
            return {"type": "GENERAL_ANSWER", "content": "AI가 응답을 거부했습니다. (안전 필터)"}

        response_text = response.parts[0].text
        print(response_text)
        
        # 마크다운 제거
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0].strip()
        
        return json.loads(response_text)

    except Exception as e:
        print(f"❌ Gemini API 또는 JSON 파싱 에러: {e}")
        return {"type": "GENERAL_ANSWER", "content": f"AI 응답 처리 중 오류가 발생했습니다: {e}"}

# --- 3. 메인 API 엔드포인트 (세미콜론 제거 포함) ---
@app.post("/analyze")
async def analyze_voice_query(query: VoiceQuery):
    
    llm_response = get_llm_response(query.text)
    
    response_type = llm_response.get("type")
    response_content = llm_response.get("content")

    if response_type == "SPATIAL_QUERY":
        if not response_content:
            return {"error": "LLM이 SQL을 생성하지 못했습니다."}
        
        # 세미콜론 제거
        cleaned_sql = response_content.strip().rstrip(';')
        
        geojson_result = execute_postgis_query(cleaned_sql)
        return geojson_result
    
    elif response_type == "GENERAL_ANSWER":
        return {"answer_text": response_content}
    
    else:
        error_message = llm_response.get("content", "알 수 없는 오류")
        return {"answer_text": f"오류가 발생했습니다: {error_message}"}