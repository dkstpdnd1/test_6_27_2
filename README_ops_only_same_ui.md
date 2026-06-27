# ICN T2 운영 추천 전용 Streamlit 페이지

이 버전은 기존 `ICN T2 혼잡 예측 관리자 대시보드`의 **운영 추천 UI와 그래프 스타일만 유지**하고, 학습/예측/검증/원본 혼잡 화면은 제거한 단독 페이지입니다.

## 입력 데이터

기존 날짜별 집계 CSV를 그대로 사용합니다.

```text
data/
  area_count_time_full_2025-09-01.csv
  area_count_time_full_2025-09-02.csv
  ...
  area_count_time_full_2025-10-31.csv
```

필수 컬럼은 아래 중 하나의 구조입니다.

```text
time_index, area, num_people
```

또는

```text
timestamp, area, num_people
```

날짜 컬럼이 없으면 파일명에서 `2025-09-01` 같은 날짜를 자동으로 읽습니다.

## 실행

```powershell
py -m pip install -r .\requirements_ops_only_same_ui.txt
```

```powershell
py -m streamlit run .\streamlit_icn_t2_ops_only_same_ui.py
```

처음 실행 후 왼쪽 사이드바에서 **운영 추천 데이터 생성 / 새로고침** 버튼을 누르세요.
생성 결과는 `outputs_ops_same_ui` 폴더에 저장되고, 다음 실행부터는 캐시 CSV를 바로 읽습니다.

## 기능

- 날짜 선택: 2025-09-01 ~ 2025-10-31
- 체크인 카운터 A~N 개방 추천
- IM1/IM2 빠른 출국장 추천
- 보안검색대 개방 추천
- 기존 운영 추천 화면과 같은 선 그래프, 히트맵, TOP 표
- 선택 날짜 추천 CSV 다운로드

## 고정 운영 기준

| 항목 | 기준 |
|---|---:|
| 운영 시간 단위 | 10분 |
| 운영 기준값 | 10분 구간 내 85% 분위값 |
| 체크인 처리량 | 25명/시간/카운터 |
| 체크인 목표 대기시간 | 10분 |
| 체크인 권장 단계 | 10개 / 20개 / 30개 |
| 보안검색 처리량 | 120명/시간/대 |
| 보안검색 목표 대기시간 | 3분 |
| 보안검색 권장 단계 | 12대 / 15대 / 17대 |
| IM1/IM2 유사 판정 | 대기시간 차이 1분 이하 |
