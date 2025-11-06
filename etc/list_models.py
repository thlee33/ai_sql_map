# list_models.py
import google.generativeai as genai
import os
from dotenv import load_dotenv

try:
    # .env 파일에서 API 키 로드
    load_dotenv()
    genai.configure(api_key=os.environ["GOOGLE_API_KEY"])

    print("--- 1.5 Pro/Flash 모델 (최신) ---")
    for m in genai.list_models():
        if 'generateContent' in m.supported_generation_methods and "1.5" in m.name:
            print(f"모델 이름: {m.name}")

    print("\n--- 1.0 Pro 모델 (안정) ---")
    for m in genai.list_models():
         if 'generateContent' in m.supported_generation_methods and "1.0" in m.name:
            print(f"모델 이름: {m.name}")
            
    print("\n--- 기타 사용 가능 모델 ---")
    for m in genai.list_models():
         if 'generateContent' in m.supported_generation_methods and "1.0" not in m.name and "1.5" not in m.name:
            print(f"모델 이름: {m.name}")


except Exception as e:
    print(f"❌ 에러 발생: {e}")
    print("GOOGLE_API_KEY가 .env 파일에 올바르게 설정되었는지 확인하세요.")