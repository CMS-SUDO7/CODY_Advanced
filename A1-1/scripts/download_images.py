"""Fashion-MNIST 테스트 이미지 파일을 내려받는 스크립트.

상품 이미지 피처를 만들기 위해 Fashion-MNIST(MIT 라이선스)의
t10k 이미지 파일(28x28 흑백 10,000장)을 data/raw/ 에 저장한다.
파일은 gzip으로 압축된 IDX 형식 그대로 저장하고, 해석(파싱)은
src/pipeline.py 의 load_idx_images()가 NumPy로 직접 한다.

실행 위치: A1-1 폴더
    python scripts/download_images.py
"""

import sys
import urllib.request
from pathlib import Path

IMAGE_URL = (
    "https://raw.githubusercontent.com/zalandoresearch/fashion-mnist/"
    "master/data/fashion/t10k-images-idx3-ubyte.gz"
)
# 스크립트 위치 기준으로 경로를 잡아, 어느 폴더에서 실행해도 같은 곳에 저장한다.
SAVE_PATH = (
    Path(__file__).resolve().parent.parent
    / "data" / "raw" / "t10k-images-idx3-ubyte.gz"
)


def download_file(url: str, save_path: Path) -> None:
    """url의 파일을 save_path에 저장한다. 이미 있으면 건너뛴다."""
    if save_path.exists():
        print(f"이미 있음: {save_path}")
        return

    save_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"다운로드 중: {url}")
    urllib.request.urlretrieve(url, save_path)
    size_mb = save_path.stat().st_size / 1_000_000
    print(f"저장 완료: {save_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    try:
        download_file(IMAGE_URL, SAVE_PATH)
    except OSError as error:
        # 네트워크 차단·주소 오류 등은 원인과 대처 방법을 알려 주고 종료한다.
        print(f"다운로드 실패: {error}")
        print("브라우저로 위 주소의 파일을 받아 data/raw/ 에 넣어도 됩니다.")
        sys.exit(1)
