"""集中配置：全部从环境变量读取，默认值与 .env.example 对齐。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# 自动加载项目根 .env（不覆盖已存在的环境变量）
try:
    from dotenv import load_dotenv

    _root = Path(__file__).resolve().parents[3]
    load_dotenv(_root / ".env", override=False)
except Exception:  # noqa: BLE001
    pass


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv("APP_ENV", "development")
    api_base_url: str = os.getenv("API_BASE_URL", "http://localhost:8000")

    storage_root: Path = Path(os.getenv("STORAGE_ROOT", "./storage"))
    sqlite_path: Path = Path(os.getenv("SQLITE_PATH", "./storage/app.db"))
    file_retention_hours: int = _get_int("FILE_RETENTION_HOURS", 24)
    max_concurrent_jobs: int = _get_int("MAX_CONCURRENT_JOBS", 4)
    cleanup_interval_seconds: int = _get_int("CLEANUP_INTERVAL_SECONDS", 600)

    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # 多模态 provider（OpenAI-compatible 第一实现；Phase 3）
    llm_base_url: str = os.getenv("LLM_BASE_URL", "")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")

    # provider 选择：openai（默认）| volcano（豆包录音文件识别，规格 §15）
    asr_provider: str = os.getenv("ASR_PROVIDER", "openai")
    ocr_provider: str = os.getenv("OCR_PROVIDER", "openai")

    # 火山豆包「录音文件识别（标准版）」—— 独立于 Doubao chat
    # 文档：https://www.volcengine.com/docs/6561/1354868 （提交音频 URL + 轮询，状态码在 header）
    volcano_asr_api_key: str = os.getenv("VOLCANO_ASR_API_KEY", "")
    volcano_asr_resource_id: str = os.getenv("VOLCANO_ASR_RESOURCE_ID", "volc.bigasr.auc")
    volcano_asr_endpoint: str = os.getenv(
        "VOLCANO_ASR_ENDPOINT", "https://openspeech.bytedance.com/api/v3/auc/bigmodel"
    )
    # 火山为「回拉音频」模式：需公网可达基址，用于拼装临时签名下载 URL（localhost 不可用）
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "")
    # 签名音频下载 token 的密钥（worker 与 api 共享同一份）；缺省时用 VOLCANO_ASR_API_KEY 派生
    asr_source_secret: str = os.getenv("ASR_SOURCE_SECRET", "")
    asr_source_ttl_seconds: int = _get_int("ASR_SOURCE_TTL_SECONDS", 1800)
    # 轮询参数：单次流程最长等待与间隔（须小于 job_runner 分类超时 audio=600/video=1200）
    asr_query_timeout_seconds: int = _get_int("ASR_QUERY_TIMEOUT_SECONDS", 1080)
    asr_query_interval_seconds: int = _get_int("ASR_QUERY_INTERVAL_SECONDS", 5)

    # 文件大小上限（MB）—— 规格 §6
    max_image_mb: int = _get_int("MAX_IMAGE_MB", 20)
    max_pdf_mb: int = _get_int("MAX_PDF_MB", 50)
    max_word_mb: int = _get_int("MAX_WORD_MB", 30)
    max_ppt_mb: int = _get_int("MAX_PPT_MB", 50)
    max_excel_mb: int = _get_int("MAX_EXCEL_MB", 30)
    max_txt_mb: int = _get_int("MAX_TXT_MB", 10)
    max_audio_mb: int = _get_int("MAX_AUDIO_MB", 100)
    max_wav_mb: int = _get_int("MAX_WAV_MB", 200)
    max_video_mb: int = _get_int("MAX_VIDEO_MB", 500)

    # 系统级二进制
    libreoffice_bin: str = os.getenv("LIBREOFFICE_BIN", "libreoffice")
    ffmpeg_bin: str = os.getenv("FFMPEG_BIN", "ffmpeg")

    # 部署：Caddy 域名驱动（空=本地反代，有值=自动 TLS）
    caddy_domain: str = os.getenv("CADDY_DOMAIN", "")

    # CORS：前端来源
    cors_origins: list[str] = field(
        default_factory=lambda: [
            o.strip()
            for o in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
            if o.strip()
        ]
    )

    @property
    def category_max_mb(self) -> dict[str, int]:
        return {
            "pdf": self.max_pdf_mb,
            "word": self.max_word_mb,
            "ppt": self.max_ppt_mb,
            "excel": self.max_excel_mb,
            "txt": self.max_txt_mb,
            "image": self.max_image_mb,
            "audio": self.max_audio_mb,
            "video": self.max_video_mb,
        }

    def ensure_dirs(self) -> None:
        for sub in ("uploads", "results", "workdirs", "asr_tmp"):
            (self.storage_root / sub).mkdir(parents=True, exist_ok=True)


settings = Settings()
