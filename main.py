# main.py (Render.com 배포용 - 'UNION ALL' 복합 쿼리 추가)
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
            - (일반 조회): `SELECT *, 'building' as data_type FROM buildings...`
            - (단계구분도/주제도): "10년 단위로" 같은 요청 시, `data_type` 컬럼에 'building'이 아닌 **분류 값**을 넣어야 합니다.
              (예: `SELECT *, CASE WHEN build_year < 1990 THEN '1990년 이전' ELSE '1990년 이후' END AS data_type FROM buildings...`)
            
            # --- [수정] UNION ALL 예시를 더 정확하게 수정 ---
            - (복합 쿼리): "A를 그리고 B를 찾아줘" 같은 요청 시, `UNION ALL`을 사용해 두 쿼리를 합쳐야 합니다. **이때 컬럼 개수와 순서를 정확히 맞춰야 합니다.**
            - (buildings 컬럼 순서: id, address, build_year, geom, data_type)
            - (예: "녹번역 250m 영역을 그리고 그 바깥 건물")
            - (답변 예): `SELECT *, 'building' AS data_type FROM buildings WHERE NOT ST_DWithin(geom::geography, (SELECT geom FROM subway_stations WHERE station_name = '녹번역')::geography, 250) UNION ALL SELECT id, station_name AS address, NULL::integer AS build_year, ST_Buffer((SELECT geom FROM subway_stations WHERE station_name = '녹번역')::geography, 250)::geometry AS geom, 'search_area' AS data_type FROM subway_stations WHERE station_name = '녹번역'`
            # --- [수정 끝] ---
            
            - 응답 형식: {{"type": "SPATIAL_QUERY", "content": "SELECT ..."}}

        2.  **클라이언트 제어 명령 (CLIENT_COMMAND)**:
            - (기존과 동일)

        3.  **일반/메타데이터 질문 (GENERAL_ANSWER)**:
            - (기존과 동일)

        (이하 프롬프트 규칙 4, 5, 6 기존과 동일)
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