# config.py
import os
import json
from dataclasses import dataclass, field
from dotenv import load_dotenv
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Optional, Any

# Load environment variables from .env file
load_dotenv()

# --- Logging Configuration ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE_PATH = os.getenv("LOG_FILE", "logs/mcp_server.log")
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", 10 * 1024 * 1024))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", 5))

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS")
if ALLOWED_ORIGINS:
    ALLOWED_ORIGINS = ALLOWED_ORIGINS.split(",")
else:
    ALLOWED_ORIGINS = ["http://localhost", "http://127.0.0.1", "http://*", "https://localhost", "https://127.0.0.1", "vscode-file://vscode-app"]

ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS")
if ALLOWED_HOSTS:
    ALLOWED_HOSTS = ALLOWED_HOSTS.split(",")
else:
    ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

# Get the root logger
root_logger = logging.getLogger()
root_logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

# Create formatter
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Remove existing handlers to avoid duplication if script is reloaded
for handler in root_logger.handlers[:]:
    root_logger.removeHandler(handler)

# Console Handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
root_logger.addHandler(console_handler)

# File Handler - Ensure log directory exists
log_file = Path(LOG_FILE_PATH)
log_file.parent.mkdir(parents=True, exist_ok=True)

file_handler = RotatingFileHandler(
    log_file,
    maxBytes=LOG_MAX_BYTES,
    backupCount=LOG_BACKUP_COUNT
)
file_handler.setFormatter(log_formatter)
root_logger.addHandler(file_handler)

# The specific logger used in server.py and elsewhere will inherit this configuration.
logger = logging.getLogger(__name__)

# --- Database Configuration ---
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", 3306))
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")
DB_CHARSET = os.getenv("DB_CHARSET")

# --- SSL Configuration ---
DB_SSL = os.getenv("DB_SSL", "false").lower() == "true"
DB_SSL_CA = os.getenv("DB_SSL_CA")
DB_SSL_CERT = os.getenv("DB_SSL_CERT")
DB_SSL_KEY = os.getenv("DB_SSL_KEY")
DB_SSL_VERIFY_CERT = os.getenv("DB_SSL_VERIFY_CERT", "true").lower() == "true"
DB_SSL_VERIFY_IDENTITY = os.getenv("DB_SSL_VERIFY_IDENTITY", "false").lower() == "true"

# --- MCP Server Configuration ---
# Read-only mode
MCP_READ_ONLY = os.getenv("MCP_READ_ONLY", "true").lower() == "true"
MCP_MAX_POOL_SIZE = int(os.getenv("MCP_MAX_POOL_SIZE", 10))

# --- Embedding Configuration ---
# Provider selection ('openai' or 'gemini' or 'huggingface')
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER")
EMBEDDING_PROVIDER = EMBEDDING_PROVIDER.lower() if EMBEDDING_PROVIDER else None
# API Keys
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")  # Custom base URL for local embedding server
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# Open models from Huggingface
HF_MODEL = os.getenv("HF_MODEL")


# --- Validation ---
if not DB_USER:
    logger.error("DB_USER is empty or missing from the environment or .env file.")
if DB_PASSWORD is None:
    logger.error("DB_PASSWORD is missing from the environment or .env file.")

# Embedding Provider and Keys
logger.info(f"Selected Embedding Provider: {EMBEDDING_PROVIDER}")
if EMBEDDING_PROVIDER == "openai":
    if not OPENAI_API_KEY:
        logger.error("EMBEDDING_PROVIDER is 'openai' but OPENAI_API_KEY is missing.")
        raise ValueError("OpenAI API key is required when EMBEDDING_PROVIDER is 'openai'.")
elif EMBEDDING_PROVIDER == "gemini":
    if not GEMINI_API_KEY:
        logger.error("EMBEDDING_PROVIDER is 'gemini' but GEMINI_API_KEY is missing.")
        raise ValueError("Gemini API key is required when EMBEDDING_PROVIDER is 'gemini'.")
elif EMBEDDING_PROVIDER == "huggingface":
    if not HF_MODEL:
        logger.error("EMBEDDING_PROVIDER is 'huggingface' but HF_MODEL is missing.")
        raise ValueError("HuggingFace model is required when EMBEDDING_PROVIDER is 'huggingface'.")
else:
    EMBEDDING_PROVIDER = None
    logger.info(f"No EMBEDDING_PROVIDER selected or it is set to None. Disabling embedding features.")

logger.info(f"Read-only mode: {MCP_READ_ONLY}")
logger.info(f"Logging to console and to file: {LOG_FILE_PATH} (Level: {LOG_LEVEL}, MaxSize: {LOG_MAX_BYTES}B, Backups: {LOG_BACKUP_COUNT})")


# --- Multi-Instance Configuration ---
INSTANCES_FILE_NAME = "instances.json"


@dataclass
class InstanceConfig:
    """Configuration for a single database instance."""
    host: str = "localhost"
    port: int = 3306
    user: str = ""
    password: str = ""
    db: str = ""
    charset: Optional[str] = None
    ssl: bool = False
    ssl_ca: Optional[str] = None
    ssl_cert: Optional[str] = None
    ssl_key: Optional[str] = None
    ssl_verify_cert: bool = True
    ssl_verify_identity: bool = False


def load_instances() -> Dict[str, Any]:
    """
    Load instance configurations.

    Discovery order:
    1. Look for instances.json in the project root (parent of src/).
    2. If not found, fall back to single-instance mode using DB_* env vars.

    Returns:
        Dict with keys:
        - "default_instance": str — name of the default instance
        - "instances": Dict[str, InstanceConfig] — name → config mapping
    """
    # Project root = parent of src/ directory (where server.py lives)
    project_root = Path(__file__).resolve().parent.parent
    instances_file = project_root / INSTANCES_FILE_NAME

    if instances_file.is_file():
        logger.info(f"Loading multi-instance config from: {instances_file}")
        try:
            with open(instances_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Failed to parse {instances_file}: {e}")
            raise RuntimeError(f"Failed to load instances config: {e}") from e

        raw_instances = data.get("instances", {})
        if not raw_instances:
            raise RuntimeError(f"No instances defined in {instances_file}")

        default_name = data.get("default_instance")
        if not default_name or default_name not in raw_instances:
            # Fall back to the first key
            default_name = next(iter(raw_instances))
            logger.warning(f"default_instance not set or invalid, using: '{default_name}'")

        instances: Dict[str, InstanceConfig] = {}
        for name, cfg in raw_instances.items():
            instances[name] = InstanceConfig(
                host=cfg.get("host", "localhost"),
                port=int(cfg.get("port", 3306)),
                user=cfg.get("user", ""),
                password=cfg.get("password", ""),
                db=cfg.get("db", ""),
                charset=cfg.get("charset"),
                ssl=cfg.get("ssl", False),
                ssl_ca=cfg.get("ssl_ca"),
                ssl_cert=cfg.get("ssl_cert"),
                ssl_key=cfg.get("ssl_key"),
                ssl_verify_cert=cfg.get("ssl_verify_cert", True),
                ssl_verify_identity=cfg.get("ssl_verify_identity", False),
            )

        logger.info(f"Loaded {len(instances)} instance(s): {list(instances.keys())} (default: '{default_name}')")
        return {"default_instance": default_name, "instances": instances}

    # Fallback: single-instance mode from env vars
    logger.info("No instances.json found. Using single-instance mode from DB_* env vars.")
    if not DB_USER:
        logger.error("Single-instance mode requires DB_USER to be set.")
    if DB_PASSWORD is None:
        logger.error("Single-instance mode requires DB_PASSWORD to be set.")

    default_cfg = InstanceConfig(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER or "",
        password=DB_PASSWORD or "",
        db=DB_NAME or "",
        charset=DB_CHARSET,
        ssl=DB_SSL,
        ssl_ca=DB_SSL_CA,
        ssl_cert=DB_SSL_CERT,
        ssl_key=DB_SSL_KEY,
        ssl_verify_cert=DB_SSL_VERIFY_CERT,
        ssl_verify_identity=DB_SSL_VERIFY_IDENTITY,
    )
    return {"default_instance": "default", "instances": {"default": default_cfg}}