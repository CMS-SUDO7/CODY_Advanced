"""H&M 구매 데이터 분석 파이프라인.

DataAnalyzer 클래스 하나로 데이터 로드 → 결측치 처리 → 피처 엔지니어링
(텍스트·이미지) → 이상치 탐지 → 통계 요약 → RFM 고객 세분화를 수행한다.
분석 노트북(notebooks/analysis_report.ipynb)은 이 클래스를 import 해서
결과 확인·시각화·해석만 맡는다.

사용 라이브러리: 표준 라이브러리(pathlib), NumPy, Pandas, Matplotlib
(Matplotlib은 이미지 파일을 배열로 읽는 imread에만 쓴다.)
"""

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# ----- 데이터 파일 이름 -----
TRANSACTIONS_FILE = "transactions_sample.csv"
ARTICLES_FILE = "articles_sample.csv"
CUSTOMERS_FILE = "customers_sample.csv"
ARTICLE_ID_DIGITS = 10               # 상품 ID는 앞자리 0을 포함한 10자리

# ----- 이미지 처리 상수 -----
IMAGE_HEIGHT = 96                    # 원본 1750x1166(세로x가로)을
IMAGE_WIDTH = 64                     # 비율(약 3:2)을 유지해 줄인 크기
GRAY_WEIGHTS = np.array([0.299, 0.587, 0.114])  # RGB → 밝기(ITU-R BT.601)
HISTOGRAM_EDGES = [0, 64, 128, 192, 256]  # 밝기 4구간: 어두움 ~ 밝음

# ----- 분석 기본값 -----
DEFAULT_IQR_MULTIPLIER = 1.5
DEFAULT_SCORE_BINS = 5               # RFM 점수를 1~5점으로 매긴다
# Frequency 점수 구간: 1회→1점, 2회→2점, 3회→3점, 4~5회→4점, 6회 이상→5점.
# 고객 절반 이상이 1회라서 순위로 나누면 같은 1회 고객이 서로 다른
# 점수를 받는다. 방문 횟수 자체로 구간을 정해 그 문제를 피한다.
DEFAULT_FREQUENCY_EDGES = [0, 1, 2, 3, 5, np.inf]


def calculate_iqr_bounds(values, multiplier=DEFAULT_IQR_MULTIPLIER):
    """IQR 방식으로 정상 범위의 하한·상한을 계산한다.

    수식:
        IQR   = Q3 - Q1
        하한  = Q1 - multiplier * IQR
        상한  = Q3 + multiplier * IQR
    """
    q1 = values.quantile(0.25)
    q3 = values.quantile(0.75)
    iqr = q3 - q1
    lower = q1 - multiplier * iqr
    upper = q3 + multiplier * iqr
    return lower, upper


def read_image_resized(path, height=IMAGE_HEIGHT, width=IMAGE_WIDTH):
    """이미지 파일 하나를 읽어 (height, width, 3) uint8 배열로 줄인다.

    크기 조절은 NumPy 인덱싱으로 직접 한다. 원본 세로·가로 범위에서
    같은 간격으로 고른 행·열 번호만 남기는 '최근접 다운샘플링'이다.
    """
    image = plt.imread(path)                 # JPG → (세로, 가로, 3) 배열
    rows = np.linspace(0, image.shape[0] - 1, height).round().astype(int)
    cols = np.linspace(0, image.shape[1] - 1, width).round().astype(int)
    return image[rows][:, cols, :3]          # 알파 채널이 있으면 버린다


def load_product_images(image_dir, article_ids, cache_path=None):
    """상품 ID 순서대로 이미지를 읽어 (N, H, W, 3) 배열 하나로 쌓는다.

    JPG 3,000장을 매번 다시 읽으면 오래 걸리므로, cache_path를 주면
    줄인 배열을 .npz로 저장해 두고 다음 실행부터 그 파일을 읽는다.
    캐시의 상품 ID 순서가 요청과 다르면 캐시를 쓰지 않는다.
    """
    article_ids = list(article_ids)
    if cache_path is not None and Path(cache_path).exists():
        cached = np.load(cache_path)
        if list(cached["article_ids"]) == article_ids:
            return cached["images"]

    # 파일 읽기는 한 장씩일 수밖에 없다. 통계 계산은 아래에서 배열로 한다.
    resized = [read_image_resized(Path(image_dir) / f"{aid}.jpg")
               for aid in article_ids]
    images = np.stack(resized)               # (N, H, W, 3)

    if cache_path is not None:
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        np.savez(cache_path, images=images, article_ids=article_ids)
    return images


def to_grayscale(images):
    """(N, H, W, 3) 컬러 배열을 (N, H, W) 밝기 배열(0~255)로 바꾼다.

    마지막 축(RGB 3채널)과 가중치 벡터(3,)의 행렬곱 한 번으로
    모든 이미지·모든 픽셀을 반복문 없이 변환한다.
    """
    return images.astype(np.float64) @ GRAY_WEIGHTS


def calculate_image_features(gray_images):
    """(N, H, W) 밝기 배열에서 이미지별 수치 피처를 계산한다.

    모든 통계는 axis=(1, 2)로 세로·가로 축을 한꺼번에 줄여
    이미지마다 값 하나를 남긴다. 결과는 길이 N인 1차원 배열들이다.
    """
    image_axes = (1, 2)
    features = {
        "image_mean": gray_images.mean(axis=image_axes),   # 전체 밝기
        "image_std": gray_images.std(axis=image_axes),     # 명암 대비
    }

    # 보너스 1: 엣지 세기 = 옆 픽셀·아래 픽셀과의 밝기 차이 평균
    horizontal_diff = np.abs(np.diff(gray_images, axis=2))
    vertical_diff = np.abs(np.diff(gray_images, axis=1))
    features["image_edge"] = (horizontal_diff.mean(axis=image_axes)
                              + vertical_diff.mean(axis=image_axes)) / 2

    # 보너스 1: 밝기 히스토그램 = 4개 밝기 구간에 속한 픽셀 비율
    bin_pairs = zip(HISTOGRAM_EDGES[:-1], HISTOGRAM_EDGES[1:])
    for bin_index, (low, high) in enumerate(bin_pairs, start=1):
        in_bin = (gray_images >= low) & (gray_images < high)
        features[f"image_hist_{bin_index}"] = in_bin.mean(axis=image_axes)
    return features


class DataAnalyzer:
    """H&M 구매 데이터의 전처리·통계·RFM 분석을 담당하는 클래스.

    상태(인스턴스 변수)
        self.df   : 구매 단위 데이터 (한 행 = 고객이 상품 1개를 산 기록)
        self.rfm  : 고객 단위 RFM 결과 (한 행 = 고객 한 명)
    """

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.df = None
        self.rfm = None

    # ------------------------------------------------------------------
    # 1. 데이터 로드
    # ------------------------------------------------------------------
    def load_data(self):
        """구매·상품·고객 CSV 세 개를 읽어 구매 단위 표 하나로 합친다.

        세 표는 SQL의 JOIN처럼 상품 ID·고객 ID를 키로 연결한다.
        구매 행을 기준(how="left")으로 붙여 구매 기록을 잃지 않는다.
        """
        id_types = {"article_id": str, "customer_id": str}
        transactions = pd.read_csv(self.data_dir / TRANSACTIONS_FILE,
                                   dtype=id_types, parse_dates=["t_dat"])
        articles = pd.read_csv(self.data_dir / ARTICLES_FILE,
                               dtype=id_types)
        customers = pd.read_csv(self.data_dir / CUSTOMERS_FILE,
                                dtype=id_types)

        # 상품 ID의 앞자리 0이 빠지지 않도록 10자리 문자열로 맞춘다.
        for table in (transactions, articles):
            table["article_id"] = (table["article_id"]
                                   .str.zfill(ARTICLE_ID_DIGITS))

        df = transactions.merge(articles, on="article_id", how="left")
        df = df.merge(customers, on="customer_id", how="left")
        self.df = df
        return self.df

    # ------------------------------------------------------------------
    # 2. 결측치 처리
    # ------------------------------------------------------------------
    def summarize_missing(self):
        """컬럼별 결측 개수와 비율(%)을 표로 반환한다. 결측 있는 컬럼만."""
        missing_count = self.df.isna().sum()
        missing_ratio = missing_count / len(self.df) * 100
        summary = pd.DataFrame({"missing_count": missing_count,
                                "missing_ratio_pct": missing_ratio.round(2)})
        return summary[summary["missing_count"] > 0]

    def handle_missing_values(self, strategy="group_median", group_col=None,
                              columns=None, fallback_value=None):
        """결측치를 지정한 전략으로 채우거나 지운다. self.df를 직접 바꾼다.

        strategy
            "group_mean" / "group_median" / "group_mode":
                group_col이 같은 행끼리의 평균/중앙값/최빈값으로 채운다.
            "constant": fallback_value 하나로 채운다.
            "drop": 해당 컬럼에 결측이 있는 행을 지운다.
        fallback_value
            그룹 전체가 결측이라 그룹 통계로도 못 채운 칸에 쓸 값.
            None이면 수치 컬럼은 전체 중앙값, 그 밖은 그대로 둔다.

        반환: {컬럼: 채운 개수} 또는 {"dropped_rows": 지운 행 수}
        """
        if columns is None:
            raise ValueError("처리할 columns 를 지정하세요.")

        if strategy == "drop":
            before = len(self.df)
            self.df = self.df.dropna(subset=columns).reset_index(drop=True)
            return {"dropped_rows": before - len(self.df)}

        report = {}
        for column in columns:
            missing_before = int(self.df[column].isna().sum())
            if strategy == "constant":
                self.df[column] = self.df[column].fillna(fallback_value)
            else:
                group_values = self._group_statistic(strategy, group_col,
                                                     column)
                self.df[column] = self.df[column].fillna(group_values)
                self._fill_remaining(column, fallback_value)
            missing_after = int(self.df[column].isna().sum())
            report[column] = missing_before - missing_after
        return report

    def _group_statistic(self, strategy, group_col, column):
        """행마다 '자기 그룹의 통계값'을 담은 Series를 만든다.

        transform은 그룹 통계를 원래 행 개수만큼 펼쳐 주므로
        fillna에 바로 넣을 수 있다. (agg는 그룹 수만큼으로 줄어든다.)
        """
        if group_col is None:
            raise ValueError("그룹 전략에는 group_col 이 필요합니다.")
        grouped = self.df.groupby(group_col)[column]
        if strategy == "group_mean":
            return grouped.transform("mean")
        if strategy == "group_median":
            return grouped.transform("median")
        if strategy == "group_mode":
            return grouped.transform(self._first_mode)
        raise ValueError(f"지원하지 않는 strategy: {strategy}")

    @staticmethod
    def _first_mode(values):
        """그룹 하나의 최빈값. 동률이면 정렬상 첫 값, 전부 결측이면 NaN."""
        modes = values.mode()
        if modes.empty:
            return np.nan
        return modes.iloc[0]

    def _fill_remaining(self, column, fallback_value):
        """그룹 통계로도 채우지 못한 칸(그룹 전체가 결측)을 처리한다."""
        if fallback_value is not None:
            self.df[column] = self.df[column].fillna(fallback_value)
            return
        if pd.api.types.is_numeric_dtype(self.df[column]):
            overall_median = self.df[column].median()
            self.df[column] = self.df[column].fillna(overall_median)

    # ------------------------------------------------------------------
    # 3. 피처 엔지니어링: 텍스트·이미지
    # ------------------------------------------------------------------
    def add_text_features(self, name_col="prod_name",
                          desc_col="detail_desc"):
        """상품명 단어 수와 상세 설명 글자 수 컬럼을 self.df에 추가한다."""
        names = self.df[name_col].fillna("").str.strip()
        descriptions = self.df[desc_col].fillna("").str.strip()
        self.df["name_word_count"] = names.str.split().str.len()
        self.df["desc_length"] = descriptions.str.len()

    def add_image_features(self, images, article_ids,
                           article_col="article_id"):
        """상품별 이미지 피처를 계산해 self.df에 붙인다.

        images      : (N, H, W, 3) 컬러 배열 (article_ids와 같은 순서)
        article_ids : 길이 N인 상품 ID 목록
        반환: 상품별 피처 표 (N행)

        이미지는 상품 단위로 한 번만 계산하고, 구매 행에는 merge로
        붙인다. 같은 상품이 여러 번 팔려도 다시 계산하지 않는다.
        """
        if len(images) != len(article_ids):
            raise ValueError("이미지 수와 상품 ID 수가 다릅니다.")
        gray_images = to_grayscale(images)
        features = calculate_image_features(gray_images)
        image_features = pd.DataFrame({article_col: list(article_ids),
                                       **features})
        self.df = self.df.merge(image_features, on=article_col, how="left")
        return image_features

    # ------------------------------------------------------------------
    # 4. 이상치 탐지·처리
    # ------------------------------------------------------------------
    def detect_outliers(self, column, threshold=DEFAULT_IQR_MULTIPLIER,
                        data=None):
        """IQR 경계 밖의 행과 경계값을 반환한다. self.df는 바꾸지 않는다.

        data를 주면 그 표(예: 상품 단위 요약)에서 찾는다.
        반환: (이상치 행 DataFrame, 하한, 상한)
        """
        if data is None:
            data = self.df
        lower, upper = calculate_iqr_bounds(data[column], threshold)
        is_outlier = (data[column] < lower) | (data[column] > upper)
        return data[is_outlier], lower, upper

    def clip_outliers(self, columns, threshold=DEFAULT_IQR_MULTIPLIER,
                      data=None):
        """IQR 경계 밖 값을 경계값으로 자른 '사본'을 반환한다.

        행을 지우지 않고 값만 경계로 옮긴다(capping). 원본은 그대로
        두므로 RFM 같은 금액 계산에는 원본을 계속 쓸 수 있다.
        """
        if data is None:
            data = self.df
        clipped = data.copy()
        for column in columns:
            lower, upper = calculate_iqr_bounds(clipped[column], threshold)
            clipped[column] = clipped[column].clip(lower=lower, upper=upper)
        return clipped

    # ------------------------------------------------------------------
    # 5. 통계 요약
    # ------------------------------------------------------------------
    def summarize_numeric(self, columns, data=None):
        """평균·중앙값·표준편차·사분위수 표를 반환한다."""
        if data is None:
            data = self.df
        target = data[columns]
        return pd.DataFrame({
            "mean": target.mean(),
            "median": target.median(),
            "std": target.std(),
            "q1": target.quantile(0.25),
            "q3": target.quantile(0.75),
        })

    def calculate_correlations(self, columns, data=None, method="pearson"):
        """지정 컬럼끼리의 상관행렬(pearson 또는 spearman)을 반환한다."""
        if data is None:
            data = self.df
        return data[columns].corr(method=method)

    def summarize_articles(self, article_col="article_id",
                           customer_col="customer_id", price_col="price"):
        """상품 단위 요약 표: 판매 수량·구매 고객 수·평균 가격 + 피처.

        이미지·텍스트 피처는 상품마다 하나뿐이므로, 판매량과의 관계는
        구매 행이 아니라 상품 단위로 봐야 많이 팔린 상품의 피처가
        여러 번 세어지는 왜곡이 없다.
        """
        sales = self.df.groupby(article_col).agg(
            units_sold=(article_col, "size"),
            buyers=(customer_col, "nunique"),
            avg_price=(price_col, "mean"),
        )
        feature_cols = [col for col in self.df.columns
                        if col.startswith(("image_", "name_", "desc_"))]
        descriptive_cols = ["prod_name", "product_group_name",
                            "perceived_colour_value_name",
                            "index_group_name"]
        article_info = (self.df[[article_col] + descriptive_cols
                                + feature_cols]
                        .drop_duplicates(subset=article_col)
                        .set_index(article_col))
        return sales.join(article_info).reset_index()

    def calculate_weekly_sales(self, date_col="t_dat", price_col="price"):
        """주별 판매 수량·매출 합계·구매 고객 수를 반환한다.

        주는 월요일 시작이다. 첫 주·마지막 주는 일부 날짜만 포함할 수
        있으므로 day_count 컬럼으로 실제 포함된 날 수를 함께 준다.
        """
        week_start = self.df[date_col].dt.to_period("W-SUN").dt.start_time
        weekly = self.df.groupby(week_start).agg(
            units_sold=(price_col, "size"),
            revenue=(price_col, "sum"),
            customers=("customer_id", "nunique"),
            day_count=(date_col, "nunique"),
        )
        weekly.index.name = "week_start"
        return weekly

    # ------------------------------------------------------------------
    # 6. RFM 고객 세분화
    # ------------------------------------------------------------------
    def calculate_rfm(self, customer_col="customer_id", date_col="t_dat",
                      amount_col="price", reference_date=None,
                      n_bins=DEFAULT_SCORE_BINS,
                      frequency_edges=DEFAULT_FREQUENCY_EDGES):
        """고객별 Recency·Frequency·Monetary와 1~n_bins 점수를 계산한다.

        Recency   : 기준일 - 마지막 구매일 (일). 작을수록 좋다.
        Frequency : 서로 다른 구매일 수. 이 데이터에는 주문번호가 없어
                    같은 날 산 여러 상품을 한 번의 방문으로 본다.
        Monetary  : 구매 금액(price) 합계. 가격은 원본이 변환해 둔
                    값이라 통화 단위가 아닌 상대 크기로 해석한다.
        reference_date가 None이면 데이터의 최종 구매일을 쓴다.
        frequency_edges가 None이면 F도 R·M처럼 분위수로 점수화하고,
        목록을 주면 그 경계로 구간을 나눈다(구간 수 = n_bins).
        """
        if reference_date is None:
            reference_date = self.df[date_col].max()

        rfm = self.df.groupby(customer_col).agg(
            last_purchase=(date_col, "max"),
            Frequency=(date_col, "nunique"),
            Monetary=(amount_col, "sum"),
        )
        rfm["Recency"] = (reference_date - rfm["last_purchase"]).dt.days

        rfm["R_score"] = self._quantile_score(rfm["Recency"], n_bins,
                                              higher_is_better=False)
        if frequency_edges is None:
            rfm["F_score"] = self._quantile_score(rfm["Frequency"], n_bins)
        else:
            rfm["F_score"] = self._fixed_bin_score(rfm["Frequency"],
                                                   frequency_edges)
        rfm["M_score"] = self._quantile_score(rfm["Monetary"], n_bins)
        rfm["RFM_score"] = rfm["R_score"] + rfm["F_score"] + rfm["M_score"]

        self.rfm = rfm.drop(columns="last_purchase")
        return self.rfm

    @staticmethod
    def _quantile_score(values, n_bins, higher_is_better=True):
        """값을 순위로 바꾼 뒤 같은 인원의 n_bins 구간으로 나눠 점수화한다.

        Frequency처럼 같은 값이 많으면 qcut의 구간 경계가 겹쳐 오류가
        난다. rank(method="first")로 동률에도 서로 다른 순위를 준다.
        """
        ranks = values.rank(method="first")
        labels = list(range(1, n_bins + 1))
        if not higher_is_better:
            labels = labels[::-1]
        return pd.qcut(ranks, q=n_bins, labels=labels).astype(int)

    @staticmethod
    def _fixed_bin_score(values, edges):
        """정해 둔 경계(edges)로 구간을 나눠 1점부터 차례로 점수를 준다.

        pd.cut의 구간은 (왼쪽, 오른쪽]이다. 경계 [0, 1, 2, ...]이면
        1은 (0, 1] → 1점, 2는 (1, 2] → 2점이 된다.
        """
        labels = list(range(1, len(edges)))
        return pd.cut(values, bins=edges, labels=labels).astype(int)

    def segment_customers(self):
        """R·F·M 점수 규칙으로 세그먼트를 붙인다. 위 규칙이 우선한다.

        VIP      : R>=4, F>=4, M>=4  최근에·자주·많이 산다
        Loyal    : R>=3, F>=3        꾸준히 온다
        New      : R>=4, F<=2        최근 왔지만 횟수가 적다
        At Risk  : R<=2, F>=3        자주 왔지만 최근 뜸하다
        Churned  : R<=2, F<=2        예전에 조금 사고 오지 않는다
        Regular  : 나머지
        """
        r_score = self.rfm["R_score"]
        f_score = self.rfm["F_score"]
        m_score = self.rfm["M_score"]

        is_vip = (r_score >= 4) & (f_score >= 4) & (m_score >= 4)
        is_loyal = (r_score >= 3) & (f_score >= 3)
        is_new = (r_score >= 4) & (f_score <= 2)
        is_at_risk = (r_score <= 2) & (f_score >= 3)
        is_churned = (r_score <= 2) & (f_score <= 2)

        conditions = [is_vip, is_loyal, is_new, is_at_risk, is_churned]
        segment_names = ["VIP", "Loyal", "New", "At Risk", "Churned"]
        self.rfm["Segment"] = np.select(conditions, segment_names,
                                        default="Regular")
        return self.rfm

    def summarize_segments(self):
        """세그먼트별 고객 수·평균 R/F/M·매출 비중 표를 반환한다."""
        summary = self.rfm.groupby("Segment").agg(
            customers=("Recency", "size"),
            avg_recency_days=("Recency", "mean"),
            avg_frequency=("Frequency", "mean"),
            avg_monetary=("Monetary", "mean"),
            total_monetary=("Monetary", "sum"),
        )
        summary["customer_share_pct"] = (
            summary["customers"] / summary["customers"].sum() * 100
        )
        summary["revenue_share_pct"] = (
            summary["total_monetary"] / summary["total_monetary"].sum() * 100
        )
        return summary.sort_values("total_monetary", ascending=False)
