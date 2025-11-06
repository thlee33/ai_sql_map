# setup_db.py
import psycopg

# 1. PostGIS 연결 정보 (Docker에서 설정한 값)
DB_HOST = "localhost"
DB_PORT = "5432"
DB_NAME = "postgres"
DB_USER = "postgres"
DB_PASS = "postgres"

# 2. 샘플 데이터 (녹번동 주변 좌표)
sql_commands = [
    # PostGIS 확장 기능 활성화
    "CREATE EXTENSION IF NOT EXISTS postgis;",
    
    # --- 1. 건물 테이블 ---
    "DROP TABLE IF EXISTS buildings;",
    "CREATE TABLE buildings ( id SERIAL PRIMARY KEY, address TEXT, build_year INT, geom GEOMETRY(Point, 4326) );",
    # (샘플) 30년 넘은 노후 건물 2개
    "INSERT INTO buildings (address, build_year, geom) VALUES ('녹번동 11-1', 1990, ST_GeomFromText('POINT(126.9377 37.5991)', 4326));",
    "INSERT INTO buildings (address, build_year, geom) VALUES ('녹번동 11-2', 1985, ST_GeomFromText('POINT(126.9378 37.5992)', 4326));",
    # (샘플) 신축 건물 1개
    "INSERT INTO buildings (address, build_year, geom) VALUES ('녹번동 12-1', 2020, ST_GeomFromText('POINT(126.9385 37.5995)', 4326));",
    
    # --- 2. 지하철역 테이블 ---
    "DROP TABLE IF EXISTS subway_stations;",
    "CREATE TABLE subway_stations ( id SERIAL PRIMARY KEY, station_name TEXT, geom GEOMETRY(Point, 4326) );",
    # (샘플) 녹번역
    "INSERT INTO subway_stations (station_name, geom) VALUES ('녹번역', ST_GeomFromText('POINT(126.9380 37.6000)', 4326));",

    # --- 3. 공간 인덱스 생성 (필수!) ---
    "CREATE INDEX IF NOT EXISTS buildings_geom_idx ON buildings USING GIST (geom);",
    "CREATE INDEX IF NOT EXISTS subway_stations_geom_idx ON subway_stations USING GIST (geom);",
]

def setup_database():
    try:
        # DB 연결
        with psycopg.connect(
            f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} user={DB_USER} password={DB_PASS}"
        ) as conn:
            with conn.cursor() as cur:
                print("DB 연결 성공. 테이블 생성 및 데이터 입력을 시작합니다...")
                for command in sql_commands:
                    cur.execute(command)
                    print(f"실행: {command[:40]}...") # SQL 일부만 출력
                conn.commit()
        print("✅ 데이터베이스 설정이 완료되었습니다.")
        
    except Exception as e:
        print(f"❌ 데이터베이스 설정 실패: {e}")
        print("PostGIS가 Docker에서 정상 실행 중인지, 포트와 비밀번호가 맞는지 확인하세요.")

if __name__ == "__main__":
    setup_database()