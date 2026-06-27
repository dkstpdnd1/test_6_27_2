r"""
ICN T2 운영 추천 전용 Streamlit 대시보드 - 기존 관리자 UI/그래프 유지 버전

목적
- 기존 "ICN T2 혼잡 예측 관리자 대시보드"의 운영 추천 화면 스타일만 분리
- 학습/예측/검증 화면 제거
- 2025-09-01 ~ 2025-10-31 실측 area_count_time_full CSV 기반 운영 추천
- 10분 단위 권장 개방 수 계산

실행
    py -m streamlit run .\streamlit_icn_t2_ops_only_same_ui.py

필수 입력
    data/area_count_time_full*.csv

생성 캐시
    outputs_ops_same_ui/operation_base_10min.csv
    outputs_ops_same_ui/checkin_counter_recommendations_10min.csv
    outputs_ops_same_ui/im_gate_recommendations_10min.csv
    outputs_ops_same_ui/security_lane_recommendations_10min.csv
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st


# ============================================================
# 0. 기본 설정
# ============================================================

st.set_page_config(
    page_title="ICN T2 혼잡 예측 관리자 대시보드",
    layout="wide",
)

APP_TITLE = "ICN T2 혼잡 예측 관리자 대시보드"
APP_SUBTITLE = "운영 추천 전용 페이지 · 기존 관리자 UI/그래프 유지"

DEFAULT_DATA_DIR = "data"
DEFAULT_OUTPUT_DIR = "outputs_ops_same_ui"
DEFAULT_INPUT_GLOB = "area_count_time_full*.csv"

START_DATE = "2025-09-01"
END_DATE = "2025-10-31"

TIME_INDEX_COL = "time_index"
AREA_COL = "area"
VALUE_COL = "num_people"
DATE_COL = "data_date"

CHECKIN_AREAS = set(list("ABCDEFGHIJKLMN"))

# 운영 기준값: UI에서 변경하지 않고 고정 적용
CHECKIN_SERVICE_RATE_PAX_PER_COUNTER_HOUR = 25.0
CHECKIN_TARGET_WAIT_MIN = 10.0
CHECKIN_STAGE_COUNTERS = (10, 20, 30)
CHECKIN_MAX_COUNTERS_PER_AREA = 30

# 줄을 최대한 세우지 않는 보수 운영 기준
SECURITY_SERVICE_RATE_PAX_PER_LANE_HOUR = 120.0
SECURITY_TARGET_WAIT_MIN = 3.0
SECURITY_STAGE_LANES = (12, 15, 17)
SECURITY_MAX_LANES_PER_GATE = 17

IM_TIE_THRESHOLD_MIN = 1.0
OPERATION_INTERVAL_MINUTES = 10
OPERATION_VALUE_QUANTILE = 0.85

WAIT_PARAMS = {
    "checkin": {"alpha": 4.0, "gamma": 0.09, "R": 6.0, "beta": 1.5, "wmax": 120.0},
    "security": {"alpha": 5.0, "gamma": 0.11, "R": 8.0, "beta": 2.0, "wmax": 120.0},
    "transit": {"alpha": 1.5, "gamma": 0.11, "R": 20.0, "beta": 4.0, "wmax": 60.0},
}


# ============================================================
# 1. 공통 유틸
# ============================================================


def normalize_area_name(area: object) -> str:
    return str(area).strip().upper()


def classify_area(area: object) -> str:
    a = normalize_area_name(area)
    if a in CHECKIN_AREAS:
        return "checkin"
    if re.fullmatch(r"\d+", a):
        return "security"
    security_keywords = [
        "IM1", "IM2", "IM 1", "IM 2", "IMMIGRATION", "SECURITY", "SCREEN",
        "SEARCH", "GATE", "출국", "보안", "검색", "심사",
    ]
    if any(k in a for k in security_keywords):
        return "security"
    return "transit"


def map_im_gate(area: object) -> str | None:
    a = normalize_area_name(area).replace(" ", "")
    if a in {"1", "IM1", "IMMIGRATION1", "출국장1", "출국1"} or "IM1" in a:
        return "IM1"
    if a in {"2", "IM2", "IMMIGRATION2", "출국장2", "출국2"} or "IM2" in a:
        return "IM2"
    return None


def extract_date_from_filename(path: Path) -> str | None:
    m = re.search(r"(20\d{2})[-_\. ]?(\d{2})[-_\. ]?(\d{2})", path.stem)
    if not m:
        return None
    y, mo, d = m.groups()
    return f"{y}-{mo}-{d}"


def format_metric(value: float | int | None, suffix: str = "") -> str:
    if value is None or pd.isna(value):
        return "-"
    if isinstance(value, (int, np.integer)):
        return f"{value:,}{suffix}"
    return f"{float(value):,.2f}{suffix}"


def compute_wait_time_min(area_type: str, people: float) -> float:
    p = WAIT_PARAMS.get(area_type, WAIT_PARAMS["transit"])
    n_eff = max(float(people), 0.0)
    wait_min = p["beta"] + p["alpha"] * (math.exp(p["gamma"] * (n_eff / p["R"])) - 1.0)
    return round(float(min(max(wait_min, 0.0), p["wmax"])), 2)


def congestion_grade(series: pd.Series) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce").fillna(0).clip(lower=0)
    mx = float(s.max())
    if mx <= 0:
        return pd.Series(1, index=s.index, dtype=int)
    grade = np.ceil((s / mx) * 10).clip(1, 10)
    return pd.Series(grade, index=s.index).astype(int)


def read_csv_robust(path: Path) -> pd.DataFrame:
    # 불필요한 컬럼은 읽지 않아서 Streamlit 메모리 사용량을 줄인다.
    required_candidates = {TIME_INDEX_COL, AREA_COL, VALUE_COL, DATE_COL, "timestamp"}
    encodings = ["utf-8-sig", "utf-8", "cp949", "euc-kr"]
    last_err: Exception | None = None
    for enc in encodings:
        try:
            return pd.read_csv(path, encoding=enc, usecols=lambda c: c in required_candidates)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"CSV 읽기 실패: {path.name} / {last_err}")


def prepare_time_columns(df: pd.DataFrame, file_date: str | None) -> pd.DataFrame:
    d = df.copy()
    if AREA_COL not in d.columns or VALUE_COL not in d.columns:
        return pd.DataFrame()

    d[AREA_COL] = d[AREA_COL].astype(str).str.strip().str.upper()
    d[VALUE_COL] = pd.to_numeric(d[VALUE_COL], errors="coerce").fillna(0.0).clip(lower=0)

    if "timestamp" in d.columns:
        d["timestamp"] = pd.to_datetime(d["timestamp"], errors="coerce")
        d = d.dropna(subset=["timestamp"])
        d[DATE_COL] = d["timestamp"].dt.strftime("%Y-%m-%d")
        d["minute_index"] = d["timestamp"].dt.hour * 60 + d["timestamp"].dt.minute
    else:
        if DATE_COL in d.columns:
            d[DATE_COL] = pd.to_datetime(d[DATE_COL], errors="coerce").dt.strftime("%Y-%m-%d")
        elif file_date is not None:
            d[DATE_COL] = file_date
        else:
            return pd.DataFrame()

        if TIME_INDEX_COL not in d.columns:
            return pd.DataFrame()
        ti = pd.to_numeric(d[TIME_INDEX_COL], errors="coerce").fillna(0).astype(int)
        # 기존 집계 파일은 10초 time_index가 일반적이다. 이미 분 단위면 그대로 사용한다.
        if ti.max() <= 1440:
            minute_index = ti.clip(lower=0, upper=1439)
        else:
            minute_index = (ti * 10 // 60).clip(lower=0, upper=1439)
        d["minute_index"] = minute_index.astype(int)
        base = pd.to_datetime(d[DATE_COL], errors="coerce")
        d["timestamp"] = base + pd.to_timedelta(d["minute_index"], unit="m")
        d = d.dropna(subset=["timestamp"])

    d = d[(d[DATE_COL] >= START_DATE) & (d[DATE_COL] <= END_DATE)].copy()
    if d.empty:
        return pd.DataFrame()

    d["window_minute"] = (d["minute_index"] // OPERATION_INTERVAL_MINUTES) * OPERATION_INTERVAL_MINUTES
    d["time_window_start"] = pd.to_datetime(d[DATE_COL]) + pd.to_timedelta(d["window_minute"], unit="m")
    d["time_window_end"] = d["time_window_start"] + pd.Timedelta(minutes=OPERATION_INTERVAL_MINUTES)
    return d[[DATE_COL, "time_window_start", "time_window_end", "window_minute", AREA_COL, VALUE_COL]]


# ============================================================
# 2. 운영 추천 계산
# ============================================================


def raw_required_servers(queue_people: pd.Series, service_per_hour: float, target_wait_min: float, max_open: int) -> pd.Series:
    denom = max(service_per_hour * target_wait_min, 1e-9)
    need = np.ceil(queue_people.clip(lower=0) * 60.0 / denom)
    need = need.where(queue_people > 0.5, 0)
    return need.clip(lower=0, upper=max_open).fillna(0).astype(int)


def snap_to_stages(required: pd.Series, stages: Iterable[int]) -> pd.Series:
    stage_values = np.array(sorted({int(s) for s in stages if int(s) > 0}), dtype=int)
    need = pd.to_numeric(required, errors="coerce").fillna(0).clip(lower=0)
    out = pd.Series(0, index=need.index, dtype=int)
    for stage in stage_values:
        mask = (need > 0) & (need <= stage) & (out == 0)
        out.loc[mask] = int(stage)
    if len(stage_values) > 0:
        out.loc[need > stage_values[-1]] = int(stage_values[-1])
    return out.astype(int)


def operation_stage_label(open_count: pd.Series, stages: Iterable[int], unit: str) -> pd.Series:
    s1, s2, s3 = [int(x) for x in stages]
    return pd.Series(
        np.select(
            [open_count <= 0, open_count <= s1, open_count <= s2, open_count <= s3],
            ["미운영", f"1단계 기본 운영({s1}{unit})", f"2단계 확대 운영({s2}{unit})", f"3단계 최대 운영({s3}{unit})"],
            default=f"3단계 최대 운영({s3}{unit})",
        ),
        index=open_count.index,
    )


def build_operation_base(data_dir: str, input_glob: str) -> pd.DataFrame:
    files = sorted(Path(data_dir).glob(input_glob))
    if not files:
        return pd.DataFrame()

    frames: list[pd.DataFrame] = []
    progress = st.progress(0.0, text="입력 CSV를 읽는 중입니다.")
    total = len(files)

    for i, path in enumerate(files, start=1):
        file_date = extract_date_from_filename(path)
        if file_date is not None and not (START_DATE <= file_date <= END_DATE):
            progress.progress(i / total, text=f"범위 밖 파일 건너뜀: {path.name}")
            continue

        raw = read_csv_robust(path)
        prepared = prepare_time_columns(raw, file_date)
        if not prepared.empty:
            g = (
                prepared.groupby([DATE_COL, "time_window_start", "time_window_end", "window_minute", AREA_COL], as_index=False)[VALUE_COL]
                .quantile(OPERATION_VALUE_QUANTILE)
            )
            frames.append(g)
        progress.progress(i / total, text=f"10분 집계 중: {i}/{total} · {path.name}")

    progress.empty()
    if not frames:
        return pd.DataFrame()

    base = pd.concat(frames, ignore_index=True)
    base = base.rename(columns={VALUE_COL: "ensemble_pred", "time_window_start": "timestamp"})
    base[AREA_COL] = base[AREA_COL].astype(str).str.strip().str.upper()
    base["area_type"] = base[AREA_COL].map(classify_area)
    base["wait_time_min"] = [compute_wait_time_min(t, v) for t, v in zip(base["area_type"], base["ensemble_pred"])]
    base["congestion_grade"] = base.groupby(AREA_COL)["ensemble_pred"].transform(lambda s: congestion_grade(s))
    base["operation_basis"] = "실측 10분 85% 분위값"
    base["operation_interval_min"] = OPERATION_INTERVAL_MINUTES

    base = base[
        [
            DATE_COL, "timestamp", "time_window_end", "window_minute", AREA_COL,
            "area_type", "ensemble_pred", "wait_time_min", "congestion_grade",
            "operation_basis", "operation_interval_min",
        ]
    ].sort_values([DATE_COL, "timestamp", AREA_COL])
    return base.reset_index(drop=True)


def make_checkin_recommendations(base: pd.DataFrame) -> pd.DataFrame:
    d = base[base[AREA_COL].astype(str).isin(CHECKIN_AREAS)].copy()
    if d.empty:
        return pd.DataFrame()
    d["raw_required_counters"] = raw_required_servers(
        d["ensemble_pred"],
        CHECKIN_SERVICE_RATE_PAX_PER_COUNTER_HOUR,
        CHECKIN_TARGET_WAIT_MIN,
        CHECKIN_MAX_COUNTERS_PER_AREA,
    )
    d["recommended_open_counters"] = snap_to_stages(d["raw_required_counters"], CHECKIN_STAGE_COUNTERS)
    d["operation_level"] = operation_stage_label(d["recommended_open_counters"], CHECKIN_STAGE_COUNTERS, "개")
    d["recommendation"] = np.where(
        d["recommended_open_counters"] > 0,
        d[AREA_COL].astype(str) + " 카운터 " + d["recommended_open_counters"].astype(str) + "개 개방 권장",
        d[AREA_COL].astype(str) + " 카운터 감시 유지",
    )
    return d.sort_values(["timestamp", AREA_COL]).reset_index(drop=True)


def make_im_recommendations(base: pd.DataFrame) -> pd.DataFrame:
    d = base.copy()
    d["im_gate"] = d[AREA_COL].map(map_im_gate)
    d = d.dropna(subset=["im_gate"])
    if d.empty:
        return pd.DataFrame()

    agg = (
        d.groupby([DATE_COL, "timestamp", "time_window_end", "window_minute", "im_gate"], as_index=False)
        .agg(wait_time_min=("wait_time_min", "mean"), ensemble_pred=("ensemble_pred", "sum"))
    )
    wait_pivot = agg.pivot_table(
        index=[DATE_COL, "timestamp", "time_window_end", "window_minute"],
        columns="im_gate",
        values="wait_time_min",
        aggfunc="mean",
    ).reset_index()
    pred_pivot = agg.pivot_table(
        index=[DATE_COL, "timestamp", "time_window_end", "window_minute"],
        columns="im_gate",
        values="ensemble_pred",
        aggfunc="sum",
    ).reset_index()

    out = wait_pivot.copy()
    for gate in ["IM1", "IM2"]:
        if gate not in out.columns:
            out[gate] = np.nan
    out = out.rename(columns={"IM1": "IM1_wait", "IM2": "IM2_wait"})

    pred_tmp = pred_pivot.copy()
    for gate in ["IM1", "IM2"]:
        if gate not in pred_tmp.columns:
            pred_tmp[gate] = np.nan
    pred_tmp = pred_tmp.rename(columns={"IM1": "IM1_people", "IM2": "IM2_people"})
    out = out.merge(pred_tmp[[DATE_COL, "timestamp", "IM1_people", "IM2_people"]], on=[DATE_COL, "timestamp"], how="left")

    diff = (out["IM1_wait"] - out["IM2_wait"]).abs()
    out["faster_gate"] = np.select(
        [out["IM1_wait"].isna() & out["IM2_wait"].notna(),
         out["IM2_wait"].isna() & out["IM1_wait"].notna(),
         diff <= IM_TIE_THRESHOLD_MIN,
         out["IM1_wait"] < out["IM2_wait"]],
        ["IM2", "IM1", "유사", "IM1"],
        default="IM2",
    )
    out["wait_diff_min"] = diff.round(2)
    out["recommendation"] = np.where(
        out["faster_gate"].eq("유사"),
        "IM1·IM2 대기시간 유사: 가까운 출국장 이용 안내",
        out["faster_gate"].astype(str) + " 이용 권장",
    )
    return out.sort_values(["timestamp"]).reset_index(drop=True)


def make_security_recommendations(base: pd.DataFrame) -> pd.DataFrame:
    d = base[base["area_type"].eq("security")].copy()
    if d.empty:
        return pd.DataFrame()
    d["im_gate"] = d[AREA_COL].map(map_im_gate).fillna(d[AREA_COL].astype(str))
    d["raw_required_security_lanes"] = raw_required_servers(
        d["ensemble_pred"],
        SECURITY_SERVICE_RATE_PAX_PER_LANE_HOUR,
        SECURITY_TARGET_WAIT_MIN,
        SECURITY_MAX_LANES_PER_GATE,
    )
    d["recommended_security_lanes"] = snap_to_stages(d["raw_required_security_lanes"], SECURITY_STAGE_LANES)
    d["operation_level"] = operation_stage_label(d["recommended_security_lanes"], SECURITY_STAGE_LANES, "대")
    d["recommendation"] = np.where(
        d["recommended_security_lanes"] > 0,
        d["im_gate"].astype(str) + " 보안검색대 " + d["recommended_security_lanes"].astype(str) + "대 개방 권장",
        d["im_gate"].astype(str) + " 보안검색대 감시 유지",
    )
    return d.sort_values(["timestamp", "im_gate", AREA_COL]).reset_index(drop=True)


@st.cache_data(show_spinner=False)
def read_cached_csv(path: str) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p)
    for col in ["timestamp", "time_window_end"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    if DATE_COL in df.columns:
        df[DATE_COL] = pd.to_datetime(df[DATE_COL], errors="coerce").dt.strftime("%Y-%m-%d")
    return df


def save_outputs(output_dir: str, base: pd.DataFrame, checkin: pd.DataFrame, im: pd.DataFrame, security: pd.DataFrame) -> None:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base.to_csv(out_dir / "operation_base_10min.csv", index=False, encoding="utf-8-sig")
    checkin.to_csv(out_dir / "checkin_counter_recommendations_10min.csv", index=False, encoding="utf-8-sig")
    im.to_csv(out_dir / "im_gate_recommendations_10min.csv", index=False, encoding="utf-8-sig")
    security.to_csv(out_dir / "security_lane_recommendations_10min.csv", index=False, encoding="utf-8-sig")


# ============================================================
# 3. 시각화 함수: 기존 관리자 대시보드 운영 추천 그래프 스타일
# ============================================================


def plot_recommended_open(df: pd.DataFrame, value_col: str, title: str, y_title: str, group_col: str = AREA_COL) -> go.Figure:
    fig = go.Figure()
    if df.empty:
        fig.update_layout(title=title, height=460)
        return fig

    for group, g in df.groupby(group_col):
        g = g.sort_values("timestamp")
        fig.add_trace(
            go.Scatter(
                x=g["timestamp"],
                y=g[value_col],
                mode="lines+markers",
                name=str(group),
                hovertemplate=(
                    "%{x|%Y-%m-%d %H:%M}<br>"
                    f"{group_col}=%{{fullData.name}}<br>"
                    f"{y_title}=%{{y:.0f}}<extra></extra>"
                ),
            )
        )

    max_y = max(float(pd.to_numeric(df[value_col], errors="coerce").max()) + 2, 5)
    fig.update_layout(
        title=title,
        hovermode="x unified",
        height=460,
        xaxis_title="시간",
        yaxis_title=y_title,
        yaxis=dict(dtick=1, range=[0, max_y]),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        margin=dict(l=30, r=20, t=60, b=40),
    )
    return fig


def plot_im_wait_compare(im_rec: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    if not im_rec.empty:
        if "IM1_wait" in im_rec.columns:
            fig.add_trace(go.Scatter(x=im_rec["timestamp"], y=im_rec["IM1_wait"], mode="lines", name="IM1 대기시간"))
        if "IM2_wait" in im_rec.columns:
            fig.add_trace(go.Scatter(x=im_rec["timestamp"], y=im_rec["IM2_wait"], mode="lines", name="IM2 대기시간"))
    fig.update_layout(
        title="IM1/IM2 예측 대기시간 비교",
        height=420,
        xaxis_title="시간",
        yaxis_title="대기시간(분)",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        margin=dict(l=30, r=20, t=60, b=40),
    )
    return fig


def plot_daily_operation_heatmap(df: pd.DataFrame, value_col: str, title: str, area_col: str = AREA_COL) -> go.Figure:
    d = df.copy()
    d["time_label"] = pd.to_datetime(d["timestamp"]).dt.strftime("%H:%M")
    pivot = d.pivot_table(index=area_col, columns="time_label", values=value_col, aggfunc="max", fill_value=0)
    pivot = pivot.reindex(sorted(pivot.index), axis=0)
    fig = px.imshow(
        pivot,
        aspect="auto",
        labels=dict(x="10분 구간", y="구역", color="권장 개방 수"),
        title=title,
    )
    fig.update_layout(height=max(420, 34 * len(pivot.index)), margin=dict(l=30, r=20, t=60, b=40))
    return fig


def table_time_format(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "timestamp" in out.columns:
        out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M")
    if "time_window_end" in out.columns:
        out["time_window_end"] = pd.to_datetime(out["time_window_end"], errors="coerce").dt.strftime("%H:%M")
    return out


# ============================================================
# 4. 사이드바 및 데이터 준비
# ============================================================

st.title(APP_TITLE)
st.caption(APP_SUBTITLE)

with st.sidebar:
    st.header("운영 추천 설정")
    data_dir = st.text_input("입력 데이터 폴더", DEFAULT_DATA_DIR)
    input_glob = st.text_input("입력 CSV 패턴", DEFAULT_INPUT_GLOB)
    output_dir = st.text_input("운영 추천 캐시 폴더", DEFAULT_OUTPUT_DIR)

    st.markdown("---")
    st.caption(f"날짜 범위: {START_DATE} ~ {END_DATE}")
    st.caption(f"운영 시간 단위: {OPERATION_INTERVAL_MINUTES}분")
    st.caption(f"10분 기준값: {int(OPERATION_VALUE_QUANTILE * 100)}% 분위값")

    generate = st.button("운영 추천 데이터 생성 / 새로고침", type="primary", use_container_width=True)

out_path = Path(output_dir)
base_path = out_path / "operation_base_10min.csv"
checkin_path = out_path / "checkin_counter_recommendations_10min.csv"
im_path = out_path / "im_gate_recommendations_10min.csv"
security_path = out_path / "security_lane_recommendations_10min.csv"

if generate:
    with st.spinner("9월 1일~10월 31일 운영 추천 데이터를 생성하는 중입니다."):
        base = build_operation_base(data_dir, input_glob)
        if base.empty:
            st.error("입력 CSV를 찾지 못했거나, 날짜 범위에 해당하는 데이터가 없습니다.")
            st.stop()
        checkin_rec_all = make_checkin_recommendations(base)
        im_rec_all = make_im_recommendations(base)
        security_rec_all = make_security_recommendations(base)
        save_outputs(output_dir, base, checkin_rec_all, im_rec_all, security_rec_all)
        st.cache_data.clear()
        st.success(f"운영 추천 캐시 생성 완료: {output_dir}")

operation_base = read_cached_csv(str(base_path))
checkin_all = read_cached_csv(str(checkin_path))
im_all = read_cached_csv(str(im_path))
security_all = read_cached_csv(str(security_path))

if operation_base.empty:
    st.warning("운영 추천 캐시 CSV가 없습니다. 왼쪽 사이드바에서 '운영 추천 데이터 생성 / 새로고침'을 먼저 눌러주세요.")
    st.info("기존 학습/예측 결과 파일은 필요 없습니다. data 폴더의 area_count_time_full CSV만 사용합니다.")
    st.stop()

# 날짜/구역 선택은 기존 대시보드와 유사하게 사이드바에서 처리한다.
dates = sorted(operation_base[DATE_COL].dropna().unique().tolist())
valid_dates = [d for d in dates if START_DATE <= d <= END_DATE]
if not valid_dates:
    st.error("운영 추천 캐시에 2025-09-01 ~ 2025-10-31 날짜가 없습니다.")
    st.stop()

with st.sidebar:
    selected_date = st.selectbox("운영 추천 날짜", valid_dates, index=0)
    all_checkin_areas = sorted(checkin_all[AREA_COL].dropna().astype(str).unique().tolist()) if not checkin_all.empty and AREA_COL in checkin_all.columns else sorted(list(CHECKIN_AREAS))
    selected_checkin_areas = st.multiselect("체크인 카운터 구역", all_checkin_areas, default=all_checkin_areas)
    st.markdown("---")
    st.caption("고정 운영 기준")
    st.caption("체크인: 10 / 20 / 30개")
    st.caption("보안검색대: 12 / 15 / 17대")
    st.caption("IM 추천: 대기시간 차이 1분 이하는 유사")

checkin_day = checkin_all[checkin_all[DATE_COL] == selected_date].copy() if not checkin_all.empty else pd.DataFrame()
im_day = im_all[im_all[DATE_COL] == selected_date].copy() if not im_all.empty else pd.DataFrame()
security_day = security_all[security_all[DATE_COL] == selected_date].copy() if not security_all.empty else pd.DataFrame()

if selected_checkin_areas and not checkin_day.empty:
    checkin_day = checkin_day[checkin_day[AREA_COL].astype(str).isin(selected_checkin_areas)].copy()

# ============================================================
# 5. 운영 추천 화면만 출력
# ============================================================

st.subheader("관리자 운영 요약")

c1, c2, c3, c4 = st.columns(4)
c1.metric("체크인 최대 권장 개방", "-" if checkin_day.empty else int(checkin_day["recommended_open_counters"].max()))
c2.metric("보안검색대 최대 권장 개방", "-" if security_day.empty else int(security_day["recommended_security_lanes"].max()))
if not im_day.empty and "faster_gate" in im_day.columns:
    c3.metric("IM1 추천 10분 구간 수", f"{int((im_day['faster_gate'] == 'IM1').sum()):,}")
    c4.metric("IM2 추천 10분 구간 수", f"{int((im_day['faster_gate'] == 'IM2').sum()):,}")
else:
    c3.metric("IM 추천", "IM1/IM2 없음")
    c4.metric("IM 추천", "-")

st.caption(
    "운영 추천은 10분 단위로 안정화해 계산합니다. "
    "기준은 고정 적용: 체크인 카운터 10/20/30개, 보안검색대 12/15/17대의 3단계 여유 운영입니다."
)

# 다운로드용 전체 날짜 CSV
csv_cols = st.columns(3)
with csv_cols[0]:
    if not checkin_day.empty:
        st.download_button(
            "선택 날짜 체크인 추천 CSV",
            data=checkin_day.to_csv(index=False, encoding="utf-8-sig"),
            file_name=f"checkin_recommendations_{selected_date}.csv",
            mime="text/csv",
            use_container_width=True,
        )
with csv_cols[1]:
    if not im_day.empty:
        st.download_button(
            "선택 날짜 IM 추천 CSV",
            data=im_day.to_csv(index=False, encoding="utf-8-sig"),
            file_name=f"im_gate_recommendations_{selected_date}.csv",
            mime="text/csv",
            use_container_width=True,
        )
with csv_cols[2]:
    if not security_day.empty:
        st.download_button(
            "선택 날짜 보안검색 추천 CSV",
            data=security_day.to_csv(index=False, encoding="utf-8-sig"),
            file_name=f"security_recommendations_{selected_date}.csv",
            mime="text/csv",
            use_container_width=True,
        )

tab1, tab2, tab3 = st.tabs(["체크인 카운터 개방", "IM1/IM2 빠른 출국장", "보안검색대 개방"])

with tab1:
    st.markdown("예측 대기인원에 해당하는 실측 10분 85% 분위값을 기준으로, 각 구역별 체크인 카운터 개방 수를 3단계로 추천합니다.")
    if checkin_day.empty:
        st.warning("선택 날짜의 체크인 구역(A~N) 데이터가 없습니다.")
    else:
        st.plotly_chart(
            plot_recommended_open(
                checkin_day,
                "recommended_open_counters",
                "체크인 카운터 구역별 권장 개방 수",
                "권장 개방 수",
            ),
            use_container_width=True,
        )
        st.plotly_chart(
            plot_daily_operation_heatmap(
                checkin_day,
                "recommended_open_counters",
                "체크인 카운터 권장 개방 수 히트맵",
                area_col=AREA_COL,
            ),
            use_container_width=True,
        )
        peak_cols = [
            "timestamp", "time_window_end", "operation_basis", AREA_COL,
            "ensemble_pred", "wait_time_min", "congestion_grade",
            "raw_required_counters", "recommended_open_counters", "operation_level", "recommendation",
        ]
        peak_cols = [c for c in peak_cols if c in checkin_day.columns]
        peak = checkin_day[peak_cols].sort_values(["recommended_open_counters", "ensemble_pred"], ascending=False).head(30)
        st.dataframe(table_time_format(peak), use_container_width=True, height=440)

with tab2:
    st.markdown("IM1·IM2의 예측 대기시간을 비교해, 해당 10분 구간에 더 빠르게 이용 가능한 출국장을 추천합니다.")
    if im_day.empty:
        st.warning("IM1/IM2로 식별되는 구역이 없습니다. area 이름이 `IM1`, `IM2` 또는 `1`, `2` 형태인지 확인하세요.")
    else:
        st.plotly_chart(plot_im_wait_compare(im_day), use_container_width=True)
        st.dataframe(table_time_format(im_day.head(300)), use_container_width=True, height=520)

with tab3:
    st.markdown("출국장 내부 보안검색 구역의 예측 대기인원을 기준으로, 줄 대기를 최소화하는 방향의 검색대 개방 수를 3단계로 추천합니다.")
    if security_day.empty:
        st.warning("선택 날짜의 보안검색/IM 구역 데이터가 없습니다.")
    else:
        group_col = "im_gate" if "im_gate" in security_day.columns else AREA_COL
        st.plotly_chart(
            plot_recommended_open(
                security_day,
                "recommended_security_lanes",
                "출국장 보안검색대 권장 개방 수",
                "권장 개방 수",
                group_col=group_col,
            ),
            use_container_width=True,
        )
        st.plotly_chart(
            plot_daily_operation_heatmap(
                security_day,
                "recommended_security_lanes",
                "보안검색대 권장 개방 수 히트맵",
                area_col=group_col,
            ),
            use_container_width=True,
        )
        peak_cols = [
            "timestamp", "time_window_end", "operation_basis", "im_gate", AREA_COL,
            "ensemble_pred", "wait_time_min", "congestion_grade",
            "raw_required_security_lanes", "recommended_security_lanes", "operation_level", "recommendation",
        ]
        peak_cols = [c for c in peak_cols if c in security_day.columns]
        peak = security_day[peak_cols].sort_values(["recommended_security_lanes", "ensemble_pred"], ascending=False).head(30)
        st.dataframe(table_time_format(peak), use_container_width=True, height=440)
