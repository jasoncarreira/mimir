"""Paths, endpoints, and constants for the LongMemEval benchmark."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

DATASET_PATH = REPO_ROOT / "data" / "longmemeval" / "longmemeval_s_cleaned.json"
WORK_DIR = REPO_ROOT / "data" / "longmemeval" / "work"
RESULTS_DIR = REPO_ROOT / "results" / "longmemeval"
UPSTREAM_DIR = REPO_ROOT / "external" / "longmemeval"
BENCH_SAGA_CONFIG = Path(__file__).parent / "msam_bench.toml"

READER_BASE_URL = "https://api.minimax.io/v1"
READER_MODEL = "MiniMax-M2.7"
READER_API_KEY_ENV = "MINIMAX_API_KEY"

RETRIEVAL_TOP_K = 20
READER_MAX_TOKENS = 512
READER_TIMEOUT_S = 90
