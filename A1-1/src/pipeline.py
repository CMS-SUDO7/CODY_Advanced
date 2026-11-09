"""이커머스 거래 데이터 분석 파이프라인.

DataAnalyzer 클래스 하나로 데이터 로드 → 결측치 처리 → 피처 엔지니어링
(텍스트·이미지) → 이상치 탐지 → 통계 요약 → RFM 고객 세분화를 수행한다.
분석 노트북(notebooks/analysis_report.ipynb)은 이 클래스를 import 해서
결과 확인·시각화·해석만 맡는다.

사용 라이브러리: 표준 라이브러리(gzip, pathlib), NumPy, Pandas
"""

import gzip
from pathlib import Path

import numpy as np
import pandas as pd

# ----- 데이터 형식 관련 상수 -----
CSV_ENCODING = "ISO-8859-1"          # 원본 CSV는 UTF-8이 아니라 Latin-1
DATE_FORMAT = "%m/%d/%Y %H:%M"       # 예: 12/1/2010 8:26
CANCEL_PREFIX = "C"                  # 취소 주문은 InvoiceNo가 C로 시작
PRODUCT_CODE_PATTERN = r"^\d{5}"     # 실제 상품은 숫자 5자리로 시작
#   (POST=배송비, M=수동 조정, BANK CHARGES 등은 상품이 아니다)

# ----- IDX(Fashion-MNIST) 파일 형식 상수 -----
IDX_HEADER_BYTES = 16                # 매직넘버·개수·행·열 = 4바이트 x 4
IDX_IMAGE_MAGIC = 2051               # 이미지 파일임을 뜻하는 매직넘버

# ----- 분석 기본값 -----
DEFAULT_IQR_MULTIPLIER = 1.5
DEFAULT_RANDOM_SEED = 42
DEFAULT_SCORE_BINS = 5               # RFM 점수를 1~5점으로 매긴다
MIN_NET_MONETARY = 0.01              # 1펜스 미만 순구매액은 0으로 본다
#   (구매-취소 계산에서 생기는 1e-12 같은 부동소수점 잔여값 대비)


def calculate_iqr_bounds(values, multiplier=DEFAULT_IQR_MULTIPLIER):
    """IQR 방식으로 정상 범위의 하한·상한을 계산한다.

    수식:
        IQR   = Q3 - Q1
        lower = Q1 - multiplier * IQR
        upper = Q3 + multiplier * IQR

    Args:
        values: 수치형 pandas Series.
        multiplier: IQR에 곱할 배수. 1.5가 관례, 3.0은 '극단' 이상치.

    Returns:
        (lower, upper) 튜플.
    """
    q1 = values.quantile(0.25)
    q3 = values.quantile(0.75)
    iqr = q3 - q1
    lower = q1 - multiplier * iqr
    upper = q3 + multiplier * iqr
    return lower, upper


def load_idx_images(path):
    """gzip으로 압축된 IDX 이미지 파일을 NumPy 배열로 읽는다.

    IDX 형식: 앞 16바이트가 헤더(빅엔디언 정수 4개), 나머지가
    픽셀값(0~255, 1바이트씩)이다. 이미지 전용 라이브러리 없이
    바이트를 그대로 배열로 바꾼 뒤 reshape 한다.

    Returns:
        shape (이미지 수, 행, 열), dtype uint8 배열.
    """
    with gzip.open(path, "rb") as file:
        raw_bytes = file.read()

    # '>i4' = 빅엔디언 4바이트 정수. 헤더 4개를 한 번에 읽는다.
    header = np.frombuffer(raw_bytes[:IDX_HEADER_BYTES], dtype=">i4")
    magic, image_count, rows, cols = header
    if magic != IDX_IMAGE_MAGIC:
        raise ValueError(f"IDX 이미지 파일이 아닙니다: magic={magic}")

    pixels = np.frombuffer(raw_bytes[IDX_HEADER_BYTES:], dtype=np.uint8)
    return pixels.reshape(image_count, rows, cols)


class DataAnalyzer:
    """이커머스 거래 데이터를 전처리하고 RFM으로 고객을 세분화한다.

    self.df 는 '현재 작업 중인 거래 데이터'이며 아래 메서드들이 직접
    수정한다(이름이 add_/handle_/filter_/convert_ 로 시작하는 메서드).
    detect_outliers, clip_outliers, summarize_*, calculate_* 는
    self.df 를 바꾸지 않고 결과만 반환한다(calculate_rfm 은 결과를
    self.rfm 에도 저장).
    """

    def __init__(self, data_path):
        self.data_path = Path(data_path)
        self.df = None      # 거래 단위 데이터 (한 행 = 주문 상품 한 줄)
        self.cancellations = None   # 고객 취소 주문 행 (Amount가 음수)
        self.rfm = None     # 고객 단위 RFM 결과 (한 행 = 고객 한 명)
        self.rfm_excluded = None    # 순구매액 0 이하로 RFM에서 뺀 고객

    # ------------------------------------------------------------------
    # 1. 데이터 로드
    # ------------------------------------------------------------------
    def load_data(self):
        """CSV를 읽고 날짜·ID 컬럼의 타입을 분석에 맞게 바꾼다.

        Returns:
            로드된 DataFrame (self.df 와 같은 객체).
        """
        df = pd.read_csv(
            self.data_path,
            encoding=CSV_ENCODING,
            # 상품코드·주문번호는 숫자처럼 보여도 '이름'이므로 문자열로 읽는다.
            dtype={"InvoiceNo": str, "StockCode": str},
        )
        df["InvoiceDate"] = pd.to_datetime(
            df["InvoiceDate"], format=DATE_FORMAT
        )
        # CustomerID는 결측 때문에 float(17850.0)로 읽힌다.
        # 결측을 유지할 수 있는 정수 타입(Int64)을 거쳐 문자열로 바꾼다.
        df["CustomerID"] = df["CustomerID"].astype("Int64").astype("string")
        self.df = df
        return self.df

    # ------------------------------------------------------------------
    # 2. 결측치 처리
    # ------------------------------------------------------------------
    def summarize_missing(self):
        """컬럼별 결측 건수와 비율(%)을 표로 반환한다."""
        missing_count = self.df.isna().sum()
        missing_ratio = missing_count / len(self.df) * 100
        return pd.DataFrame(
            {"missing_count": missing_count,
             "missing_ratio_pct": missing_ratio.round(2)}
        )

    def convert_invalid_to_missing(self, column, min_valid):
        """column 값이 min_valid 미만이면 결측(NaN)으로 바꾼다.

        단가 0 이하처럼 '값은 있지만 의미상 틀린' 데이터를 결측으로
        표시해 handle_missing_values()에서 함께 대치하기 위한 단계.

        Returns:
            결측으로 바꾼 행 수.
        """
        is_invalid = self.df[column] < min_valid
        self.df.loc[is_invalid, column] = np.nan
        return int(is_invalid.sum())

    def handle_missing_values(self, strategy="group_median",
                              group_col="StockCode", columns=None,
                              fallback_value=None):
        """그룹별 통계로 결측치를 채운다 (self.df 를 직접 수정).

        Args:
            strategy: 'group_mean' | 'group_median' | 'group_mode' | 'drop'
                - group_mean/median: 수치형. 같은 그룹의 평균/중앙값으로 채움
                - group_mode: 범주·텍스트. 같은 그룹의 최빈값으로 채움
                - drop: 결측이 있는 행을 삭제
            group_col: 그룹을 나눌 컬럼 (예: 같은 상품코드끼리).
            columns: 처리할 컬럼 목록.
            fallback_value: 그룹 전체가 결측이라 통계를 못 구할 때 쓸 값.
                None 이면 수치형은 전체 중앙값, 그 외는 그대로 둔다.

        Returns:
            {컬럼: 채운(또는 삭제한) 건수} 사전.
        """
        if columns is None:
            raise ValueError("처리할 columns 를 지정하세요.")

        report = {}
        if strategy == "drop":
            before = len(self.df)
            self.df = self.df.dropna(subset=columns).reset_index(drop=True)
            report["dropped_rows"] = before - len(self.df)
            return report

        for column in columns:
            missing_before = int(self.df[column].isna().sum())
            group_values = self._group_statistic(strategy, group_col, column)
            self.df[column] = self.df[column].fillna(group_values)
            self._fill_remaining(column, fallback_value)
            report[column] = missing_before - int(self.df[column].isna().sum())
        return report

    def _group_statistic(self, strategy, group_col, column):
        """행마다 '자기 그룹의 통계값'을 담은 Series를 만든다.

        groupby().transform() 은 그룹별 결과를 원래 행 순서·길이에 맞춰
        펼쳐 주므로 fillna()에 그대로 넣을 수 있다.
        """
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
        """최빈값 중 첫 번째를 반환한다. 모두 결측이면 NaN."""
        modes = values.mode()
        if modes.empty:
            return np.nan
        return modes.iloc[0]

    def _fill_remaining(self, column, fallback_value):
        """그룹 통계로도 못 채운 결측을 대체값으로 채운다."""
        if fallback_value is not None:
            self.df[column] = self.df[column].fillna(fallback_value)
            return
        if pd.api.types.is_numeric_dtype(self.df[column]):
            self.df[column] = self.df[column].fillna(self.df[column].median())

    # ------------------------------------------------------------------
    # 3. 피처 엔지니어링 (텍스트·금액·이미지)
    # ------------------------------------------------------------------
    def add_text_features(self, text_col="Description"):
        """상품명의 단어 수와 문자열 길이 컬럼을 추가한다."""
        cleaned_text = self.df[text_col].fillna("").str.strip()
        # split()은 공백 여러 개를 하나로 보고 나눈다.
        self.df["desc_word_count"] = cleaned_text.str.split().str.len()
        self.df["desc_length"] = cleaned_text.str.len()

    def add_amount(self, quantity_col="Quantity", price_col="UnitPrice"):
        """거래 금액 Amount = 수량 x 단가 컬럼을 추가한다."""
        self.df["Amount"] = self.df[quantity_col] * self.df[price_col]

    def add_image_features(self, images, product_col="StockCode",
                           seed=DEFAULT_RANDOM_SEED):
        """상품마다 이미지 1장을 연결하고 픽셀 평균·표준편차를 추가한다.

        원본 거래 데이터에는 이미지가 없으므로, 고유 상품코드마다
        Fashion-MNIST 이미지 1장을 시드 고정 무작위로 배정한다.
        (실제 상품 사진이 아니라 '이미지 피처 계산 실습'용 연결이다.)

        반복문 없이 계산하는 방법:
            images[chosen] 의 shape 는 (상품 수, 28, 28).
            axis=(1, 2) 로 행·열 방향을 한꺼번에 줄이면
            이미지마다 값 하나가 남아 shape (상품 수,) 가 된다.

        Args:
            images: shape (N, H, W) 이미지 배열.
            product_col: 상품을 구분하는 컬럼.
            seed: 무작위 배정의 재현성을 위한 시드.

        Returns:
            상품별 이미지 피처 표 (product_col, image_index,
            image_mean, image_std).
        """
        product_codes = self.df[product_col].unique()
        if len(product_codes) > len(images):
            raise ValueError("이미지 수가 상품 수보다 적습니다.")

        rng = np.random.default_rng(seed)
        # replace=False: 상품끼리 같은 이미지를 공유하지 않게 한다.
        chosen = rng.choice(len(images), size=len(product_codes),
                            replace=False)
        chosen_images = images[chosen].astype(np.float64)

        image_features = pd.DataFrame({
            product_col: product_codes,
            "image_index": chosen,
            "image_mean": chosen_images.mean(axis=(1, 2)),
            "image_std": chosen_images.std(axis=(1, 2)),
        })
        # merge 는 새 DataFrame 을 만들므로 self.df 에 다시 대입한다.
        self.df = self.df.merge(image_features, on=product_col, how="left")
        return image_features

    # ------------------------------------------------------------------
    # 4. 분석 대상 거래 고르기
    # ------------------------------------------------------------------
    def filter_valid_transactions(self, customer_col="CustomerID",
                                  invoice_col="InvoiceNo",
                                  product_col="StockCode",
                                  quantity_col="Quantity"):
        """RFM 분석에 쓸 '정상 구매' 행만 남긴다 (self.df 를 직접 수정).

        제외 기준과 이유:
            - 고객 ID 없음: 누가 샀는지 몰라 고객 단위 분석이 불가능
            - 취소 주문(C로 시작): 구매가 아니라 구매의 되돌림
            - 수량 0 이하: 반품·재고 조정
            - 상품코드가 숫자 5자리로 시작하지 않음: 배송비·수수료 등

        Returns:
            {제외 사유: 행 수} 사전 (앞 조건에서 이미 빠진 행은 다시 세지 않음).
        """
        has_customer = self.df[customer_col].notna()
        is_cancelled = self.df[invoice_col].str.startswith(CANCEL_PREFIX)
        is_positive_qty = self.df[quantity_col] > 0
        is_product = self.df[product_col].str.match(PRODUCT_CODE_PATTERN)

        report = {
            "no_customer": int((~has_customer).sum()),
            "cancelled": int((has_customer & is_cancelled).sum()),
            "non_positive_qty": int(
                (has_customer & ~is_cancelled & ~is_positive_qty).sum()),
            "non_product_code": int(
                (has_customer & ~is_cancelled & is_positive_qty
                 & ~is_product).sum()),
        }
        # 취소 행은 버리지 않고 따로 보관한다. RFM의 Monetary에서
        # '샀다가 취소한 금액'을 빼는 데 쓴다 (calculate_rfm 참고).
        is_customer_cancel = has_customer & is_cancelled & is_product
        self.cancellations = self.df[is_customer_cancel].reset_index(
            drop=True)
        report["kept_cancellations"] = len(self.cancellations)

        is_valid = has_customer & ~is_cancelled & is_positive_qty & is_product
        self.df = self.df[is_valid].reset_index(drop=True)
        report["remaining_rows"] = len(self.df)
        return report

    # ------------------------------------------------------------------
    # 5. 이상치
    # ------------------------------------------------------------------
    def detect_outliers(self, column, threshold=DEFAULT_IQR_MULTIPLIER):
        """IQR 범위를 벗어난 행을 반환한다 (self.df 는 바꾸지 않음).

        Returns:
            (이상치 행 DataFrame, lower, upper) 튜플.
        """
        lower, upper = calculate_iqr_bounds(self.df[column], threshold)
        is_outlier = (self.df[column] < lower) | (self.df[column] > upper)
        return self.df[is_outlier], lower, upper

    def clip_outliers(self, columns, threshold=DEFAULT_IQR_MULTIPLIER):
        """IQR 경계 밖의 값을 경계값으로 바꾼 '사본'을 반환한다.

        삭제 대신 경계값으로 자르는(capping) 이유: 행을 지우면 그 주문의
        다른 정보까지 사라지기 때문이다. self.df 원본은 그대로 둔다.
        """
        clipped = self.df.copy()
        for column in columns:
            lower, upper = calculate_iqr_bounds(clipped[column], threshold)
            clipped[column] = clipped[column].clip(lower=lower, upper=upper)
        return clipped

    # ------------------------------------------------------------------
    # 6. 통계 요약
    # ------------------------------------------------------------------
    def summarize_numeric(self, columns, data=None):
        """평균·중앙값·표준편차·사분위수 표를 반환한다.

        Args:
            columns: 요약할 수치형 컬럼 목록.
            data: 요약할 DataFrame. None 이면 self.df.
        """
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
        """컬럼 간 상관계수 행렬을 반환한다.

        method='pearson' 은 직선 관계, 'spearman' 은 순위(단조) 관계를 본다.
        """
        if data is None:
            data = self.df
        return data[columns].corr(method=method)

    def calculate_monthly_revenue(self, date_col="InvoiceDate",
                                  amount_col="Amount"):
        """월별 매출 합계와 주문 수를 반환한다."""
        month = self.df[date_col].dt.to_period("M")
        monthly = self.df.groupby(month).agg(
            revenue=(amount_col, "sum"),
            orders=("InvoiceNo", "nunique"),
        )
        monthly.index = monthly.index.to_timestamp()
        return monthly

    # ------------------------------------------------------------------
    # 7. RFM
    # ------------------------------------------------------------------
    def calculate_rfm(self, customer_col="CustomerID",
                      date_col="InvoiceDate", amount_col="Amount",
                      invoice_col="InvoiceNo", reference_date=None,
                      cancellations=None, n_bins=DEFAULT_SCORE_BINS):
        """고객별 Recency·Frequency·Monetary 값과 1~n_bins 점수를 계산한다.

        정의:
            Recency   = 기준일 - 마지막 구매일 (일). 작을수록 좋다.
            Frequency = 서로 다른 주문번호 수 (취소 주문 제외).
            Monetary  = 구매 금액 합계 + 취소 금액 합계(음수)
                      = 실제로 남은 순구매액
        기준일(reference_date)이 None 이면 데이터의 최종 거래일을 쓴다.
        cancellations 가 None 이면 취소 금액을 빼지 않는다.

        점수: 고객을 값 순서대로 n_bins 개 같은 인원 구간(분위수)으로
        나눠 1~n_bins 점을 준다. Recency 는 작을수록 높은 점수.

        Returns:
            고객 단위 RFM DataFrame (self.rfm 에도 저장).
            순구매액이 0 이하인 고객(산 만큼 모두 취소)은 빼고,
            그 목록은 self.rfm_excluded 에 둔다.
        """
        if reference_date is None:
            reference_date = self.df[date_col].max()

        rfm = self.df.groupby(customer_col).agg(
            last_purchase=(date_col, "max"),
            Frequency=(invoice_col, "nunique"),
            Monetary=(amount_col, "sum"),
        )
        rfm["Recency"] = (reference_date - rfm["last_purchase"]).dt.days

        if cancellations is not None:
            cancelled_amount = cancellations.groupby(customer_col)[
                amount_col].sum()
            # 취소만 있고 구매가 없는 고객은 rfm 에 없으므로 제외된다.
            # fill_value=0: 취소가 없는 고객은 0원을 더한다.
            rfm["Monetary"] = rfm["Monetary"].add(
                cancelled_amount, fill_value=0).loc[rfm.index]

        has_net_purchase = rfm["Monetary"] >= MIN_NET_MONETARY
        self.rfm_excluded = rfm[~has_net_purchase]
        rfm = rfm[has_net_purchase].copy()

        rfm["R_score"] = self._quantile_score(rfm["Recency"], n_bins,
                                              higher_is_better=False)
        rfm["F_score"] = self._quantile_score(rfm["Frequency"], n_bins)
        rfm["M_score"] = self._quantile_score(rfm["Monetary"], n_bins)
        rfm["RFM_score"] = rfm["R_score"] + rfm["F_score"] + rfm["M_score"]

        self.rfm = rfm.drop(columns="last_purchase")
        return self.rfm

    @staticmethod
    def _quantile_score(values, n_bins, higher_is_better=True):
        """값을 분위수 구간으로 나눠 1~n_bins 점수를 매긴다.

        Frequency 는 1회 구매 고객이 매우 많아 같은 값이 몰린다.
        그대로 qcut 하면 구간 경계가 겹쳐 오류가 나므로, 먼저
        rank(method='first')로 동점에도 서로 다른 순위를 준 뒤 나눈다.
        """
        ranks = values.rank(method="first")
        labels = list(range(1, n_bins + 1))
        if not higher_is_better:
            labels = labels[::-1]
        return pd.qcut(ranks, q=n_bins, labels=labels).astype(int)

    def segment_customers(self):
        """R·F·M 점수 규칙으로 고객 세그먼트를 붙인다 (self.rfm 수정).

        규칙은 위에서부터 먼저 맞는 것을 적용한다 (np.select 동작).
            VIP     : R>=4, F>=4, M>=4  최근·자주·많이
            Loyal   : R>=3, F>=3        꾸준히 오는 고객
            New     : R>=4, F<=2        최근 왔지만 아직 횟수 적음
            At Risk : R<=2, F>=3        예전엔 자주 왔는데 최근 뜸함
            Churned : R<=2, F<=2        오래전에 조금 사고 안 옴
            Regular : 나머지            중간 고객
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
        """세그먼트별 고객 수·비율·평균 RFM·매출 비중 표를 반환한다."""
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
        return summary.sort_values("total_monetary", ascending=False).round(2)
