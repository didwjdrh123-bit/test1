"""교사용 대시보드 — teacher_dashboard.py (Supabase + Streamlit)
─────────────────────────────────────────────────────────────────
목표: 교사 친화적/직관적/시각적 UI

주요 기능
- 학번(부분) 검색, 최근 N일 필터, 모델 필터
- 수동 새로고침 + (선택) 자동 새로고침(폴링)
- KPI: 총 제출 수, 고유 학생 수, 최신 제출 시각, 문항별 O 비율
- 시각화: 일자별 제출 추이, 문항별 O/X 비율 막대
- 목록: 보기 편한 컬럼 구성 + 상세(답/피드백) 펼쳐보기
- 개인별: 선택 학번의 제출 이력 + 문항별 성취(최근 제출 기준)
- CSV 다운로드

※ 보안: 대시보드는 서버/로컬 환경에서만 운영 권장.
   SERVICE_ROLE_KEY는 절대 외부 공개/클라이언트 배포 금지.
"""

from __future__ import annotations

import streamlit as st
import pandas as pd
import altair as alt

from supabase import create_client, Client
from datetime import datetime, timedelta, timezone

# ────────────────────────────────────────────────────────────────
# 0) 페이지/스타일
# ────────────────────────────────────────────────────────────────
st.set_page_config(page_title="교사용 대시보드", page_icon="📊", layout="wide")

# 가벼운 UI 보정(표/버튼 간격)
st.markdown(
    """
    <style>
      .block-container { padding-top: 1.2rem; padding-bottom: 2.0rem; }
      [data-testid="stMetricValue"] { font-size: 1.65rem; }
      .tiny-note { color: #6b7280; font-size: 0.85rem; }
      .pill { display:inline-block; padding:0.15rem 0.55rem; border-radius:999px; background:#f3f4f6; margin-right:0.35rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ────────────────────────────────────────────────────────────────
# 1) 간단 인증(권장: st.secrets에 저장)
# ────────────────────────────────────────────────────────────────
# secrets 예시
#   TEACHER_DASH_PASSWORD = "990606"  (혹은 해시)

if "auth_ok" not in st.session_state:
    st.session_state.auth_ok = False

with st.sidebar:
    st.title("🔐 교사 인증")
    if not st.session_state.auth_ok:
        pw = st.text_input("암호", type="password", placeholder="교사 인증 암호")
        if st.button("로그인", use_container_width=True):
            expected = st.secrets.get("TEACHER_DASH_PASSWORD", "990606")
            if pw == expected:
                st.session_state.auth_ok = True
                st.success("인증 완료")
            else:
                st.error("암호가 올바르지 않습니다.")
    else:
        st.success("로그인 상태")
        if st.button("로그아웃", use_container_width=True):
            st.session_state.auth_ok = False
            st.rerun()

if not st.session_state.auth_ok:
    st.info("좌측 사이드바에서 교사 인증 후 이용해 주세요.")
    st.stop()

# ────────────────────────────────────────────────────────────────
# 2) Supabase 연결
# ────────────────────────────────────────────────────────────────
@st.cache_resource
def get_supabase_client() -> Client:
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_SERVICE_ROLE_KEY"]  # 서버/로컬에서만 보관
    return create_client(url, key)


# ────────────────────────────────────────────────────────────────
# 3) 데이터 로드
# ────────────────────────────────────────────────────────────────
SELECT_COLS = (
    "id, student_id, answer_1, answer_2, answer_3, "
    "feedback_1, feedback_2, feedback_3, model, created_at"
)


@st.cache_data(show_spinner=False, ttl=30)
def fetch_data(search_id: str, days: int, model_filter: str | None) -> pd.DataFrame:
    """필터 조건에 맞는 제출 데이터 로드."""
    try:
        supabase = get_supabase_client()
        q = supabase.table("student_submissions").select(SELECT_COLS)

        if search_id:
            q = q.ilike("student_id", f"%{search_id}%")

        if days and days > 0:
            date_from = datetime.now(timezone.utc) - timedelta(days=int(days))
            q = q.gte("created_at", date_from.isoformat())

        if model_filter and model_filter != "전체":
            q = q.eq("model", model_filter)

        q = q.order("created_at", desc=True)
        res = q.execute()
        rows = res.data or []
        df = pd.DataFrame(rows)

        if not df.empty and "created_at" in df.columns:
            df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce", utc=True)

        # 결측/타입 정리
        for c in ["student_id", "model"]:
            if c in df.columns:
                df[c] = df[c].fillna("").astype(str)

        return df

    except Exception as e:
        st.error(f"Supabase 조회 오류: {e}")
        return pd.DataFrame()


@st.cache_data(show_spinner=False, ttl=30)
def fetch_student_history(student_id: str, limit: int = 200) -> pd.DataFrame:
    """특정 학번 제출 이력."""
    try:
        supabase = get_supabase_client()
        q = (
            supabase.table("student_submissions")
            .select(SELECT_COLS)
            .eq("student_id", student_id)
            .order("created_at", desc=True)
            .limit(limit)
        )
        res = q.execute()
        rows = res.data or []
        df = pd.DataFrame(rows)
        if not df.empty and "created_at" in df.columns:
            df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce", utc=True)
        return df
    except Exception as e:
        st.error(f"개인 이력 조회 오류: {e}")
        return pd.DataFrame()


# ────────────────────────────────────────────────────────────────
# 4) 유틸(정답/오답 판정)
# ────────────────────────────────────────────────────────────────

def _starts_with(tag: str, x: str) -> bool:
    return str(x).strip().startswith(tag)


def o_rate(series: pd.Series | None) -> float:
    if series is None or series.empty:
        return 0.0
    s = series.fillna("").astype(str)
    return (s.map(lambda x: _starts_with("O:", x)).sum() / len(s)) * 100.0


def ox_counts(series: pd.Series | None) -> dict:
    """feedback가 'O:'/'X:'로 시작한다고 가정하고 개수 산출."""
    if series is None or series.empty:
        return {"O": 0, "X": 0, "ETC": 0}
    s = series.fillna("").astype(str)
    o = s.map(lambda x: _starts_with("O:", x)).sum()
    x = s.map(lambda x: _starts_with("X:", x)).sum()
    etc = len(s) - o - x
    return {"O": int(o), "X": int(x), "ETC": int(etc)}


def local_time_str(dt: pd.Timestamp | None) -> str:
    if dt is None or pd.isna(dt):
        return "-"
    # 한국 시간 표시(Asia/Seoul)
    try:
        return dt.tz_convert("Asia/Seoul").strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(dt)


# ────────────────────────────────────────────────────────────────
# 5) 사이드바 필터/동작
# ────────────────────────────────────────────────────────────────
with st.sidebar:
    st.divider()
    st.subheader("⚙️ 필터")
    search_id = st.text_input("학번 검색(부분)", value="", placeholder="예: 20301 또는 0301")
    days = st.number_input("최근 N일", min_value=0, max_value=365, value=30, step=1)

    st.subheader("🔄 새로고침")
    auto = st.toggle("자동 새로고침(30초)", value=False)
    st.caption("자동 새로고침은 페이지를 주기적으로 다시 불러옵니다.")

    col_r1, col_r2 = st.columns(2)
    with col_r1:
        manual_refresh = st.button("지금 새로고침", use_container_width=True)
    with col_r2:
        clear_cache = st.button("캐시 초기화", use_container_width=True)

    st.divider()
    st.markdown(
        "<div class='tiny-note'>※ <b>created_at</b>은 UTC로 저장되며, 화면에는 KST로 표시됩니다.</div>",
        unsafe_allow_html=True,
    )

if clear_cache:
    st.cache_data.clear()
    st.rerun()

if manual_refresh:
    st.cache_data.clear()

if auto:
    # Streamlit 기본 폴링 방식 (추가 패키지 없이)
    # 30초마다 rerun
    st.experimental_set_query_params(_="auto")
    st.autorefresh(interval=30_000, key="teacher_autorefresh")

# ────────────────────────────────────────────────────────────────
# 6) 상단 제목/데이터 로드
# ────────────────────────────────────────────────────────────────
st.title("📊 교사용 대시보드 — 서술형 평가")
st.caption("필터를 조절해 최근 제출을 빠르게 확인하고, 학생별 피드백을 한 화면에서 확인할 수 있습니다.")

# 모델 목록은 데이터에서 동적으로
# (초기 로드: 모델 필터 없이 한 번만 가져오되 캐시 TTL 덕분에 부담 적음)
df_for_models = fetch_data(search_id="", days=int(days), model_filter=None)
models = sorted([m for m in df_for_models.get("model", pd.Series([], dtype=str)).unique().tolist() if m])
model_filter = st.selectbox("모델 필터", options=["전체"] + models, index=0)

# 메인 데이터
st.markdown("---")
df = fetch_data(search_id=search_id.strip(), days=int(days), model_filter=model_filter)

if df.empty:
    st.info("조건에 해당하는 데이터가 없습니다. (필터를 완화하거나 새로고침을 눌러보세요)")
    st.stop()

# ────────────────────────────────────────────────────────────────
# 7) KPI 요약
# ────────────────────────────────────────────────────────────────
unique_students = df["student_id"].nunique() if "student_id" in df.columns else 0
latest_time = df["created_at"].max() if "created_at" in df.columns else None

r1 = o_rate(df.get("feedback_1"))
r2 = o_rate(df.get("feedback_2"))
r3 = o_rate(df.get("feedback_3"))

k1, k2, k3, k4 = st.columns([1.1, 1.1, 1.2, 1.6])
k1.metric("총 제출", f"{len(df):,}")
k2.metric("고유 학생", f"{unique_students:,}")
k3.metric("최신 제출(KST)", local_time_str(latest_time))

with k4:
    st.markdown(
        """
        <span class='pill'>문항1 O {}</span>
        <span class='pill'>문항2 O {}</span>
        <span class='pill'>문항3 O {}</span>
        """.format(f"{r1:.1f}%", f"{r2:.1f}%", f"{r3:.1f}%"),
        unsafe_allow_html=True,
    )

# ────────────────────────────────────────────────────────────────
# 8) 탭 구성 (개요/목록/개인/분석)
# ────────────────────────────────────────────────────────────────
tab_overview, tab_list, tab_student, tab_analysis = st.tabs(
    ["🏠 개요", "📄 제출 목록", "👤 개인 피드백", "📈 분석"]
)

# ────────────────────────────────────────────────────────────────
# 탭: 개요
# ────────────────────────────────────────────────────────────────
with tab_overview:
    st.subheader("한눈에 보기")

    # 일자별 제출 추이
    if "created_at" in df.columns:
        tmp = df.copy()
        tmp["date_kst"] = tmp["created_at"].dt.tz_convert("Asia/Seoul").dt.date
        daily = tmp.groupby("date_kst").size().reset_index(name="submissions")

        line = (
            alt.Chart(daily)
            .mark_line(point=True)
            .encode(
                x=alt.X("date_kst:T", title="날짜"),
                y=alt.Y("submissions:Q", title="제출 수"),
                tooltip=[alt.Tooltip("date_kst:T", title="날짜"), alt.Tooltip("submissions:Q", title="제출")],
            )
            .properties(height=220)
        )
        st.altair_chart(line, use_container_width=True)

    # 문항별 O/X/기타
    c1 = ox_counts(df.get("feedback_1"))
    c2 = ox_counts(df.get("feedback_2"))
    c3 = ox_counts(df.get("feedback_3"))

    ox_df = pd.DataFrame(
        [
            {"문항": "1", **c1},
            {"문항": "2", **c2},
            {"문항": "3", **c3},
        ]
    ).melt(id_vars=["문항"], var_name="판정", value_name="개수")

    bar = (
        alt.Chart(ox_df)
        .mark_bar()
        .encode(
            x=alt.X("문항:N", title="문항"),
            y=alt.Y("개수:Q", title="건수", stack=True),
            color=alt.Color("판정:N", title="판정"),
            tooltip=["문항:N", "판정:N", "개수:Q"],
        )
        .properties(height=240)
    )

    st.altair_chart(bar, use_container_width=True)

    with st.expander("도움말: 판정(O/X) 기준"):
        st.write(
            "- feedback_i가 'O:'로 시작하면 정답(O), 'X:'로 시작하면 오답(X)으로 계산합니다.\n"
            "- 그 외 형식(예: 공란/다른 접두어)은 ETC로 분류됩니다.\n"
            "- 필요하면 접두어 규칙을 학교 양식에 맞게 바꿔 드릴 수 있어요."
        )

# ────────────────────────────────────────────────────────────────
# 탭: 제출 목록
# ────────────────────────────────────────────────────────────────
with tab_list:
    st.subheader("제출 목록")
    st.caption("표에서 최근 제출을 빠르게 확인하고, 상세 내용은 아래에서 펼쳐서 확인합니다.")

    # 보기용 컬럼(요약)
    view = df.copy()
    if "created_at" in view.columns:
        view["제출시각(KST)"] = view["created_at"].dt.tz_convert("Asia/Seoul").dt.strftime("%Y-%m-%d %H:%M")

    # 짧은 요약 컬럼 생성(답안/피드백 미리보기)
    def _preview(x: str, n: int = 24) -> str:
        x = "" if x is None else str(x)
        x = x.replace("\n", " ").strip()
        return x if len(x) <= n else (x[:n] + "…")

    for i in [1, 2, 3]:
        a = f"answer_{i}"
        f = f"feedback_{i}"
        if a in view.columns:
            view[f"답{i}(미리보기)"] = view[a].map(lambda x: _preview(x, 28))
        if f in view.columns:
            view[f"피드백{i}(미리보기)"] = view[f].map(lambda x: _preview(x, 28))

    show_cols = [
        "student_id",
        "제출시각(KST)",
        "답1(미리보기)", "피드백1(미리보기)",
        "답2(미리보기)", "피드백2(미리보기)",
        "답3(미리보기)", "피드백3(미리보기)",
        "model",
    ]
    show_cols = [c for c in show_cols if c in view.columns]

    st.dataframe(
        view[show_cols],
        use_container_width=True,
        hide_index=True,
    )

    # CSV 다운로드(원문 포함)
    csv_cols = [
        "student_id", "created_at",
        "answer_1", "feedback_1",
        "answer_2", "feedback_2",
        "answer_3", "feedback_3",
        "model",
    ]
    csv_cols = [c for c in csv_cols if c in df.columns]

    csv = df[csv_cols].to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "📥 CSV 다운로드(원문 포함)",
        data=csv,
        file_name="student_submissions.csv",
        mime="text/csv",
        use_container_width=False,
    )

    st.divider()
    st.subheader("최근 제출 상세 보기")
    st.caption("가장 최근 제출부터 30건을 표시합니다. (필요하면 개수 조절 가능)")

    n = st.slider("표시할 최근 제출 개수", min_value=5, max_value=80, value=30, step=5)
    recent = df.head(int(n)).copy()

    for idx, row in recent.iterrows():
        sid = row.get("student_id", "")
        ts = local_time_str(row.get("created_at"))
        model = row.get("model", "")

        with st.expander(f"🧾 {sid} · {ts} · {model}"):
            cA, cB, cC = st.columns(3)
            with cA:
                st.markdown("**문항 1**")
                st.write(row.get("answer_1", ""))
                st.markdown("**피드백 1**")
                st.write(row.get("feedback_1", ""))
            with cB:
                st.markdown("**문항 2**")
                st.write(row.get("answer_2", ""))
                st.markdown("**피드백 2**")
                st.write(row.get("feedback_2", ""))
            with cC:
                st.markdown("**문항 3**")
                st.write(row.get("answer_3", ""))
                st.markdown("**피드백 3**")
                st.write(row.get("feedback_3", ""))

# ────────────────────────────────────────────────────────────────
# 탭: 개인 피드백
# ────────────────────────────────────────────────────────────────
with tab_student:
    st.subheader("학생별 제출 이력")

    student_list = sorted(df["student_id"].dropna().astype(str).unique().tolist())
    left, right = st.columns([1.2, 2.8])

    with left:
        selected = st.selectbox("학번 선택", options=student_list)
        limit = st.slider("조회 개수(최근순)", min_value=50, max_value=400, value=200, step=50)

    if selected:
        history = fetch_student_history(selected, limit=int(limit))
        if history.empty:
            st.info("이 학번의 이력이 없습니다.")
        else:
            st.success(f"{selected} — 제출 {len(history)}건")

            # 최신 제출 요약
            latest = history.iloc[0]
            st.markdown("#### 최신 제출 요약")
            s1, s2, s3, s4 = st.columns([1.3, 1, 1, 1])
            s1.metric("제출 시각(KST)", local_time_str(latest.get("created_at")))
            s2.metric("문항1", "O" if _starts_with("O:", latest.get("feedback_1", "")) else "X")
            s3.metric("문항2", "O" if _starts_with("O:", latest.get("feedback_2", "")) else "X")
            s4.metric("문항3", "O" if _starts_with("O:", latest.get("feedback_3", "")) else "X")

            st.divider()
            st.markdown("#### 제출 이력(최근순)")

            hist = history.copy()
            if "created_at" in hist.columns:
                hist["제출시각(KST)"] = hist["created_at"].dt.tz_convert("Asia/Seoul").dt.strftime("%Y-%m-%d %H:%M")

            hist_cols = [
                "제출시각(KST)",
                "answer_1", "feedback_1",
                "answer_2", "feedback_2",
                "answer_3", "feedback_3",
                "model",
            ]
            hist_cols = [c for c in hist_cols if c in hist.columns]

            st.dataframe(hist[hist_cols], use_container_width=True, hide_index=True)

# ────────────────────────────────────────────────────────────────
# 탭: 분석
# ────────────────────────────────────────────────────────────────
with tab_analysis:
    st.subheader("분석")
    st.caption("교사가 빠르게 판단할 수 있도록, 제출 패턴과 문항별 성취를 시각화합니다.")

    # 학생별 제출 수 Top 20
    s_counts = (
        df.groupby("student_id").size().reset_index(name="제출수").sort_values("제출수", ascending=False).head(20)
    )

    c1, c2 = st.columns([1.2, 1.8])

    with c1:
        st.markdown("#### 학생별 제출 수 (Top 20)")
        chart = (
            alt.Chart(s_counts)
            .mark_bar()
            .encode(
                y=alt.Y("student_id:N", sort="-x", title="학번"),
                x=alt.X("제출수:Q", title="제출 수"),
                tooltip=["student_id:N", "제출수:Q"],
            )
            .properties(height=420)
        )
        st.altair_chart(chart, use_container_width=True)

    with c2:
        st.markdown("#### 문항별 O 비율 추이(최근 N일)")
        if "created_at" in df.columns:
            t = df.copy()
            t["date_kst"] = t["created_at"].dt.tz_convert("Asia/Seoul").dt.date

            def _daily_orate(feedback_col: str, label: str) -> pd.DataFrame:
                x = t[["date_kst", feedback_col]].copy()
                x["is_o"] = x[feedback_col].fillna("").astype(str).map(lambda v: _starts_with("O:", v)).astype(int)
                g = x.groupby("date_kst")["is_o"].mean().reset_index()
                g["문항"] = label
                g["O비율"] = g["is_o"] * 100.0
                return g[["date_kst", "문항", "O비율"]]

            frames = []
            if "feedback_1" in t.columns:
                frames.append(_daily_orate("feedback_1", "문항1"))
            if "feedback_2" in t.columns:
                frames.append(_daily_orate("feedback_2", "문항2"))
            if "feedback_3" in t.columns:
                frames.append(_daily_orate("feedback_3", "문항3"))

            if frames:
                dd = pd.concat(frames, ignore_index=True)
                line2 = (
                    alt.Chart(dd)
                    .mark_line(point=True)
                    .encode(
                        x=alt.X("date_kst:T", title="날짜"),
                        y=alt.Y("O비율:Q", title="O 비율(%)", scale=alt.Scale(domain=[0, 100])),
                        color=alt.Color("문항:N"),
                        tooltip=[alt.Tooltip("date_kst:T", title="날짜"), "문항:N", alt.Tooltip("O비율:Q", format=".1f")],
                    )
                    .properties(height=420)
                )
                st.altair_chart(line2, use_container_width=True)
            else:
                st.info("피드백 컬럼이 없어 O 비율 추이를 계산할 수 없습니다.")
        else:
            st.info("created_at 컬럼이 없어 일자별 분석을 표시할 수 없습니다.")

st.markdown("---")
st.caption("문의/개선 포인트: (1) O/X 판정 규칙, (2) 미제출 학생(명단) 대조, (3) 문항별 키워드/오답 유형 자동 집계")
