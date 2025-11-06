# main.py (Gemini 버전)
import os
import psycopg
import google.generativeai as genai  # 1. Gemini 라이브러리 임포트
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

# .env 파일에서 API 키 로드
load_dotenv()

# --- 1. Google (Gemini) 클라이언트 설정 ---
try:
    # 2. Google API 키 사용
    genai.configure(api_key=os.environ["GOOGLE_API_KEY"])
except KeyError:
    print("="*50)
    print("❌ 에러: GOOGLE_API_KEY를 찾을 수 없습니다.")
    print("'.env' 파일에 API 키를 정확히 입력했는지 확인하세요.")
    print("="*50)
    exit()


# --- 2. PostGIS 연결 정보 (사용자 환경에 맞게 수정!) ---
DB_HOST = "localhost"
DB_PORT = "5432"
DB_NAME = "postgres"
DB_USER = "postgres"
DB_PASS = "postgres" # (이전에 'postgres'로 바꾸신 것 확인)

# --- 3. LLM에게 알려줄 DB 스키마 (메타데이터) ---
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
* ST_DWithin(geom1, geom2, distance_meters): 두 지점 사이의 거리가 미터 단위로 일정 거리 내에 있는지 확인 (예: ST_DWithin(b.geom, s.geom, 500) -> 500m 이내)
* ST_AsGeoJSON(geom): PostGIS의 geom 컬럼을 GeoJSON 형식으로 변환.
* (now() - interval '30 years'): 현재 시간 기준 30년 전. (build_year < (extract(year from now()) - 30)) 와 동일.
"""

# --- 4. FastAPI 앱 설정 ---
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

# --- 5. PostGIS 쿼리 실행 함수 (GeoJSON 반환) ---
def execute_postgis_query(sql_query: str):
    """
    LLM이 생성한 SQL 쿼리를 PostGIS에 실행하고 결과를 GeoJSON FeatureCollection으로 반환합니다.
    """
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
                    return result[0] # GeoJSON 객체 반환
                else:
                    return {"type": "FeatureCollection", "features": []} # 결과 없으면 빈 GeoJSON

    except Exception as e:
        print(f"❌ 쿼리 실행 에러: {e}")
        return {"error": str(e), "query": sql_query}


# --- 6. LLM (Gemini) 호출 함수 ---
def get_sql_from_gemini(user_question: str):
    """
    사용자의 질문(텍스트)을 Gemini API로 보내 PostGIS SQL 쿼리를 받아옵니다.
    """
    
    # 3. Gemini용 모델 및 프롬프트 설정
    model = genai.GenerativeModel(
        model_name='models/gemini-pro-latest', # 넉넉한 무료 티어 모델
        system_instruction=f"""
        당신은 최고의 PostGIS 데이터베이스 전문가입니다.
        사용자의 자연어 질문을 받으면, 아래 [데이터베이스 스키마]를 참고하여 
        PostGIS에서 실행 가능한 단 하나의 SQL 쿼리를 생성해야 합니다.

        [규칙]
        1.  결과는 반드시 GeoJSON으로 변환할 수 있도록 `geom` 컬럼을 SELECT 절에 포함해야 합니다.
        2.  `ST_AsGeoJSON` 같은 GeoJSON 변환 함수는 쿼리에 포함하지 마십시오. (파이썬에서 처리함)
        3.  오직 SQL 쿼리만 응답해야 하며, 어떠한 설명이나 인사말, 마크다운(` ```sql ... ``` `)도 포함해서는 안 됩니다.
        4.  테이블 이름이나 컬럼 이름이 스키마에 없는 경우, 가장 유사한 것을 사용하십시오.
        5.  주소(address)를 검색할 때는 `LIKE`와 `%`를 사용하십시오. (예: `address LIKE '녹번동%'`)
        6.  날짜 계산 시 `extract(year from now())`를 사용하십시오. (예: `build_year < (extract(year from now()) - 30)`)
        
        {DATABASE_SCHEMA}
        
        [예시 질문]
        "녹번동 30년 넘은 건물 찾아줘"
        [예시 SQL 응답]
        SELECT geom, id, address, build_year FROM buildings WHERE address LIKE '녹번동%' AND build_year < (extract(year from now()) - 30)
        
        [예시 질문]
        "녹번역 500미터 안에 있는 건물 보여줘"
        [예시 SQL 응답]
        SELECT b.geom, b.id, b.address, s.station_name FROM buildings b JOIN subway_stations s ON ST_DWithin(b.geom, s.geom, 500) WHERE s.station_name = '녹번역'
        """
    )

    print(f"--- Gemini에게 보낼 질문: {user_question} ---")

    try:
        # 4. Gemini API 호출
        response = model.generate_content(user_question)
        sql_query = response.text.strip()
        
        print(f"--- Gemini가 생성한 SQL ---")
        print(sql_query)
        return sql_query

    except Exception as e:
        print(f"❌ Gemini API 에러: {e}")
        return None

# --- 7. 메인 API 엔드포인트 ---
@app.post("/analyze")
async def analyze_voice_query(query: VoiceQuery):
    
    # 5. Gemini 함수로 변경
    sql_query = get_sql_from_gemini(query.text)
    
    if not sql_query:
        return {"error": "Gemini API로부터 쿼리를 생성하는 데 실패했습니다."}
    
    geojson_result = execute_postgis_query(sql_query)
    
    return geojson_result