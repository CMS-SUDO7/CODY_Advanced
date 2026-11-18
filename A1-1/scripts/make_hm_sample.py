"""H&M 대회 데이터에서 A1-1 미션용 샘플을 잘라 낸다.

이 스크립트는 내 PC가 아니라 Kaggle 노트북에서 실행한다.
대회 데이터(34.56GB)가 Kaggle 서버에 이미 연결되어 있으므로
전체를 내려받지 않고 필요한 부분만 잘라 zip 하나로 만든다.

실행 방법
    1. Kaggle에서 대회 규칙에 동의한다.
       https://www.kaggle.com/competitions/
       h-and-m-personalized-fashion-recommendations
    2. 대회 Code 탭에서 비공개 노트북을 만들고 이 파일 내용을
       셀에 붙여 넣어 실행한다. (약 6분)
    3. /kaggle/working/hm_sample.zip(약 720MB)을 내려받아
       A1-1/data/raw/ 에 압축을 푼다.

샘플 기준
    - 기간: 2020-06-23 ~ 2020-09-22 (데이터 마지막 약 3개월)
    - 상품: 기간 내 판매량 상위 중 사진이 있는 3,000개
    - 고객: 위 상품을 산 고객 중 무작위 30,000명 (시드 42)

대회 규칙상 데이터는 비상업·교육 목적으로만 쓸 수 있고 재배포가
금지된다. 만든 zip과 압축을 푼 파일을 GitHub 등에 올리지 않는다.
"""

import shutil
from pathlib import Path

import numpy as np
import pandas as pd

# Kaggle 노트북에서 대회 데이터가 연결되는 경로
INPUT_DIR = Path(
    "/kaggle/input/competitions/h-and-m-personalized-fashion-recommendations"
)
OUTPUT_DIR = Path("/kaggle/working/hm_sample")
ZIP_BASE_NAME = "/kaggle/working/hm_sample"   # .zip은 자동으로 붙는다

START_DATE = "2020-06-23"
END_DATE = "2020-09-22"
N_ARTICLES = 3000
N_CUSTOMERS = 30000
RANDOM_SEED = 42
ARTICLE_ID_DIGITS = 10


def image_path(article_id):
    """상품 ID(10자리)로 사진 경로를 만든다. 폴더 이름은 ID 앞 3자리다."""
    return INPUT_DIR / "images" / article_id[:3] / f"{article_id}.jpg"


def load_transactions_in_period():
    """전체 구매 기록(약 3,179만 행)을 읽어 분석 기간만 남긴다."""
    transactions = pd.read_csv(
        INPUT_DIR / "transactions_train.csv",
        dtype={"customer_id": str, "article_id": str,
               "price": "float32", "sales_channel_id": "int8"},
        parse_dates=["t_dat"],
    )
    transactions["article_id"] = (transactions["article_id"]
                                  .str.zfill(ARTICLE_ID_DIGITS))
    in_period = transactions["t_dat"].between(START_DATE, END_DATE)
    return transactions[in_period]


def choose_top_articles(transactions):
    """기간 내 판매량 순으로 사진이 있는 상품을 N_ARTICLES개 고른다."""
    sales_count = transactions["article_id"].value_counts()
    with_image = [aid for aid in sales_count.index
                  if image_path(aid).exists()]
    return with_image[:N_ARTICLES]


def choose_customers(transactions):
    """구매 고객 중 N_CUSTOMERS명을 시드 고정 난수로 고른다."""
    rng = np.random.default_rng(RANDOM_SEED)
    all_customers = transactions["customer_id"].unique()
    sample_size = min(N_CUSTOMERS, len(all_customers))
    return rng.choice(all_customers, size=sample_size, replace=False)


def save_sample(transactions, customer_ids):
    """구매·상품·고객 CSV와 사진을 OUTPUT_DIR에 저장한다."""
    image_out_dir = OUTPUT_DIR / "images"
    image_out_dir.mkdir(parents=True, exist_ok=True)
    used_articles = transactions["article_id"].unique()

    articles = pd.read_csv(INPUT_DIR / "articles.csv",
                           dtype={"article_id": str})
    articles["article_id"] = articles["article_id"].str.zfill(
        ARTICLE_ID_DIGITS)
    articles = articles[articles["article_id"].isin(used_articles)]

    customers = pd.read_csv(INPUT_DIR / "customers.csv",
                            dtype={"customer_id": str})
    customers = customers[customers["customer_id"].isin(customer_ids)]

    transactions.to_csv(OUTPUT_DIR / "transactions_sample.csv", index=False)
    articles.to_csv(OUTPUT_DIR / "articles_sample.csv", index=False)
    customers.to_csv(OUTPUT_DIR / "customers_sample.csv", index=False)

    for aid in used_articles:
        shutil.copy(image_path(aid), image_out_dir / f"{aid}.jpg")
    return len(articles), len(customers)


def main():
    transactions = load_transactions_in_period()
    print("기간 내 구매 행:", len(transactions))

    top_articles = choose_top_articles(transactions)
    transactions = transactions[transactions["article_id"]
                                .isin(top_articles)]
    customer_ids = choose_customers(transactions)
    transactions = transactions[transactions["customer_id"]
                                .isin(customer_ids)]

    article_count, customer_count = save_sample(transactions, customer_ids)
    print("구매 행:", len(transactions), "| 고객:", customer_count,
          "| 상품·사진:", article_count)

    # 하나의 zip으로 묶고, 같은 파일이 두 번 내려받아지지 않게
    # 원래 폴더는 지운다.
    zip_path = shutil.make_archive(ZIP_BASE_NAME, "zip", OUTPUT_DIR)
    shutil.rmtree(OUTPUT_DIR)
    size_mb = Path(zip_path).stat().st_size / 1_000_000
    print(f"zip: {zip_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
