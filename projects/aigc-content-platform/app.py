"""可运行的 V1 图片平台骨架。

默认使用本地 SVG 模拟生图，配置了图片供应商 HTTP 地址后可替换为真实适配器。
"""
from __future__ import annotations

import json
import base64
import os
import re
import sqlite3
import threading
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


def _load_env_file(paths: list[str | Path] | None = None) -> Path | None:
    """Load the first available dotenv file without overriding process env.

    The service intentionally keeps secrets out of the database and frontend.  A
    shell environment variable always wins; this loader only fills in values that
    are absent when the app is launched directly from the project checkout.
    """
    if paths is None:
        app_path = Path(__file__).resolve()
        paths = [Path.cwd() / ".env", app_path.parent / ".env"]
        paths.extend(parent / ".env" for parent in app_path.parents)

    for candidate in paths:
        env_path = Path(candidate).expanduser()
        if not env_path.is_file():
            continue
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ.setdefault(key, value)
        return env_path
    return None


_load_env_file()


MODULES = {
    "emoji": {"name": "表情包", "preset": {"width": 1024, "height": 1024}},
    "mobile_wallpaper": {"name": "手机壁纸", "preset": {"width": 1290, "height": 2796}},
    "desktop_wallpaper": {"name": "电脑壁纸", "preset": {"width": 1920, "height": 1080}},
}
MIN_SIZE, MAX_SIZE = 256, 4096
MIN_RATIO, MAX_RATIO = 0.25, 4.0
FREE_DAILY_LIMIT = 3
PROVIDER_NAMES = {"doubao": "豆包", "qwen": "千问", "sensenova": "商汤"}
MOCK_LABEL = "开发测试模型 · Mock"
BUILTIN_MODEL_IDS = {"sensenova-6.8-flash-lite", "sensenova-u1.5-lite", "doubao-seedream", "qwen-image"}
RISK_RULES = (
    ("violence", "暴力/血腥", ("暴力", "血腥", "肢解", "枪击", "gore")),
    ("adult", "成人/裸露", ("色情", "裸露", "裸体", "porn", "nsfw")),
)


class ValidationError(ValueError):
    pass


class AuthorizationError(PermissionError):
    pass


class AIGCService:
    def __init__(self, db_path: str | Path = "data/app.db", output_dir: str | Path = "data/outputs"):
        self.db_path = Path(db_path)
        self.output_dir = Path(output_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()
        self.cleanup_expired_generations()

    def _connect(self):
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    def _audit(self, action: str, resource_type: str, resource_id: str, actor: str, details: dict | None = None) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO audit_logs(action,resource_type,resource_id,actor,details) VALUES(?,?,?,?,?)",
                       (action, resource_type, resource_id, actor, json.dumps(details or {}, ensure_ascii=False)))

    def audit_logs(self, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as db:
            rows = db.execute("SELECT * FROM audit_logs ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"] or "{}")
            result.append(item)
        return result

    def _init_db(self):
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS models (
                  id TEXT PRIMARY KEY, provider TEXT NOT NULL, name TEXT NOT NULL,
                  role TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                  endpoint TEXT NOT NULL DEFAULT '', cost REAL NOT NULL DEFAULT 0,
                  auth_env TEXT NOT NULL DEFAULT '',
                  daily_limit INTEGER NOT NULL DEFAULT 0,
                  adapter TEXT NOT NULL DEFAULT 'mock',
                  remote_model_id TEXT NOT NULL DEFAULT '',
                  module_scope TEXT NOT NULL DEFAULT '',
                  priority INTEGER NOT NULL DEFAULT 0,
                  fallback_model_id TEXT NOT NULL DEFAULT '',
                  timeout_seconds INTEGER NOT NULL DEFAULT 120,
                  retry_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS templates (
                  id TEXT PRIMARY KEY, module TEXT NOT NULL, title TEXT NOT NULL,
                  source_url TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                  copyright_status TEXT NOT NULL DEFAULT 'pending', version INTEGER NOT NULL DEFAULT 1,
                  prompt_template TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS template_versions (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, template_id TEXT NOT NULL,
                  version INTEGER NOT NULL, title TEXT NOT NULL, prompt_template TEXT NOT NULL,
                  status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS audit_logs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
                  resource_type TEXT NOT NULL, resource_id TEXT NOT NULL,
                  actor TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS review_runs (
                  id TEXT PRIMARY KEY, run_date TEXT NOT NULL DEFAULT CURRENT_DATE,
                  module TEXT NOT NULL DEFAULT '', batch_id TEXT NOT NULL DEFAULT '',
                  pending_review INTEGER NOT NULL DEFAULT 0, published INTEGER NOT NULL DEFAULT 0,
                  rejected INTEGER NOT NULL DEFAULT 0, actor TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS candidates (
                  id TEXT PRIMARY KEY, module TEXT NOT NULL, title TEXT NOT NULL,
                  prompt_template TEXT NOT NULL DEFAULT '',
                  risk_flags TEXT NOT NULL DEFAULT '[]',
                  source_url TEXT NOT NULL, copyright_status TEXT NOT NULL,
                  fingerprint TEXT NOT NULL, batch_id TEXT NOT NULL,
                  status TEXT NOT NULL, reviewer TEXT NOT NULL DEFAULT '',
                  reviewed_at TEXT, review_reason TEXT NOT NULL DEFAULT '',
                  ai_review_status TEXT NOT NULL DEFAULT 'not_run',
                  ai_review_result TEXT NOT NULL DEFAULT '{}',
                  ai_reviewed_at TEXT, ai_review_model_id TEXT NOT NULL DEFAULT '',
                  publish_status TEXT NOT NULL DEFAULT 'not_published',
                  publish_result TEXT NOT NULL DEFAULT '{}',
                  published_at TEXT, publish_connector_id TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS connectors (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, platform TEXT NOT NULL,
                  mode TEXT NOT NULL DEFAULT 'manual', enabled INTEGER NOT NULL DEFAULT 0,
                  authorization_status TEXT NOT NULL DEFAULT 'pending', endpoint TEXT NOT NULL DEFAULT '',
                  auth_env TEXT NOT NULL DEFAULT '',
                  last_sync_at TEXT, last_sync_status TEXT NOT NULL DEFAULT 'never',
                  last_sync_result TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS usage (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, module TEXT NOT NULL,
                  provider TEXT NOT NULL, model_id TEXT NOT NULL, template_id TEXT NOT NULL,
                  user_id TEXT NOT NULL, status TEXT NOT NULL, tokens INTEGER NOT NULL,
                  credits REAL NOT NULL, cost REAL NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS generations (
                  id TEXT PRIMARY KEY, module TEXT NOT NULL, template_id TEXT NOT NULL,
                  user_id TEXT NOT NULL, model_id TEXT NOT NULL, status TEXT NOT NULL,
                  prompt TEXT NOT NULL, negative_prompt TEXT NOT NULL DEFAULT '', output_path TEXT NOT NULL, output_url TEXT NOT NULL,
                  width INTEGER NOT NULL, height INTEGER NOT NULL, runtime_mode TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(models)").fetchall()}
            if "adapter" not in columns:
                db.execute("ALTER TABLE models ADD COLUMN adapter TEXT NOT NULL DEFAULT 'mock'")
            if "remote_model_id" not in columns:
                db.execute("ALTER TABLE models ADD COLUMN remote_model_id TEXT NOT NULL DEFAULT ''")
            if "auth_env" not in columns:
                db.execute("ALTER TABLE models ADD COLUMN auth_env TEXT NOT NULL DEFAULT ''")
            if "module_scope" not in columns:
                db.execute("ALTER TABLE models ADD COLUMN module_scope TEXT NOT NULL DEFAULT ''")
            if "priority" not in columns:
                db.execute("ALTER TABLE models ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
            if "fallback_model_id" not in columns:
                db.execute("ALTER TABLE models ADD COLUMN fallback_model_id TEXT NOT NULL DEFAULT ''")
            if "timeout_seconds" not in columns:
                db.execute("ALTER TABLE models ADD COLUMN timeout_seconds INTEGER NOT NULL DEFAULT 120")
            if "retry_count" not in columns:
                db.execute("ALTER TABLE models ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
            candidate_columns = {row[1] for row in db.execute("PRAGMA table_info(candidates)").fetchall()}
            if "connector_id" not in candidate_columns:
                db.execute("ALTER TABLE candidates ADD COLUMN connector_id TEXT NOT NULL DEFAULT ''")
            if "reviewed_at" not in candidate_columns:
                db.execute("ALTER TABLE candidates ADD COLUMN reviewed_at TEXT")
            if "review_reason" not in candidate_columns:
                db.execute("ALTER TABLE candidates ADD COLUMN review_reason TEXT NOT NULL DEFAULT ''")
            if "prompt_template" not in candidate_columns:
                db.execute("ALTER TABLE candidates ADD COLUMN prompt_template TEXT NOT NULL DEFAULT ''")
            if "risk_flags" not in candidate_columns:
                db.execute("ALTER TABLE candidates ADD COLUMN risk_flags TEXT NOT NULL DEFAULT '[]'")
            if "ai_review_status" not in candidate_columns:
                db.execute("ALTER TABLE candidates ADD COLUMN ai_review_status TEXT NOT NULL DEFAULT 'not_run'")
            if "ai_review_result" not in candidate_columns:
                db.execute("ALTER TABLE candidates ADD COLUMN ai_review_result TEXT NOT NULL DEFAULT '{}'")
            if "ai_reviewed_at" not in candidate_columns:
                db.execute("ALTER TABLE candidates ADD COLUMN ai_reviewed_at TEXT")
            if "ai_review_model_id" not in candidate_columns:
                db.execute("ALTER TABLE candidates ADD COLUMN ai_review_model_id TEXT NOT NULL DEFAULT ''")
            if "publish_status" not in candidate_columns:
                db.execute("ALTER TABLE candidates ADD COLUMN publish_status TEXT NOT NULL DEFAULT 'not_published'")
            if "publish_result" not in candidate_columns:
                db.execute("ALTER TABLE candidates ADD COLUMN publish_result TEXT NOT NULL DEFAULT '{}'")
            if "published_at" not in candidate_columns:
                db.execute("ALTER TABLE candidates ADD COLUMN published_at TEXT")
            if "publish_connector_id" not in candidate_columns:
                db.execute("ALTER TABLE candidates ADD COLUMN publish_connector_id TEXT NOT NULL DEFAULT ''")
            connector_columns = {row[1] for row in db.execute("PRAGMA table_info(connectors)").fetchall()}
            if "auth_env" not in connector_columns:
                db.execute("ALTER TABLE connectors ADD COLUMN auth_env TEXT NOT NULL DEFAULT ''")
            if "last_sync_at" not in connector_columns:
                db.execute("ALTER TABLE connectors ADD COLUMN last_sync_at TEXT")
            if "last_sync_status" not in connector_columns:
                db.execute("ALTER TABLE connectors ADD COLUMN last_sync_status TEXT NOT NULL DEFAULT 'never'")
            if "last_sync_result" not in connector_columns:
                db.execute("ALTER TABLE connectors ADD COLUMN last_sync_result TEXT NOT NULL DEFAULT '{}'")
            generation_columns = {row[1] for row in db.execute("PRAGMA table_info(generations)").fetchall()}
            if "negative_prompt" not in generation_columns:
                db.execute("ALTER TABLE generations ADD COLUMN negative_prompt TEXT NOT NULL DEFAULT ''")
            template_columns = {row[1] for row in db.execute("PRAGMA table_info(templates)").fetchall()}
            if "prompt_template" not in template_columns:
                db.execute("ALTER TABLE templates ADD COLUMN prompt_template TEXT NOT NULL DEFAULT ''")
            models = [
                ("sensenova-6.8-flash-lite", "sensenova", "SenseNova 6.8 Flash Lite", "understanding", 0, "mock", "sensenova-6.8-flash-lite"),
                ("sensenova-u1.5-lite", "sensenova", "SenseNova U1.5 Lite", "image", 1, "mock", "sensenova-u1.5-lite"),
                ("doubao-seedream", "doubao", "Doubao-Seedream", "image", 1, "mock", "doubao-seedream"),
                ("qwen-image", "qwen", "Qwen-Image", "image", 1, "mock", "qwen-image-3.0-pro"),
            ]
            db.executemany(
                "INSERT OR IGNORE INTO models(id,provider,name,role,enabled,adapter,remote_model_id) VALUES(?,?,?,?,?,?,?)", models
            )
            db.execute("UPDATE models SET remote_model_id=? WHERE id=? AND (remote_model_id='' OR remote_model_id IS NULL)",
                       ("qwen-image-3.0-pro", "qwen-image"))
            defaults = [
                ("emoji-default", "emoji", "通用表情包", "", "published", "cleared", "可爱、简洁、主体清晰的聊天表情包，留出文字空间"),
                ("mobile-default", "mobile_wallpaper", "手机壁纸·通用", "", "published", "cleared", "适合手机锁屏的竖版壁纸，主体居中，顶部留出时间区域"),
                ("desktop-default", "desktop_wallpaper", "电脑壁纸·通用", "", "published", "cleared", "适合电脑桌面的横版壁纸，主体错开中心，留出图标空间"),
            ]
            db.executemany(
                "INSERT OR IGNORE INTO templates(id,module,title,source_url,status,copyright_status,prompt_template) VALUES(?,?,?,?,?,?,?)",
                defaults,
            )
            for template_id, prompt_template in ((item[0], item[6]) for item in defaults):
                db.execute("UPDATE templates SET prompt_template=? WHERE id=? AND (prompt_template='' OR prompt_template IS NULL)",
                           (prompt_template, template_id))
                db.execute("""INSERT INTO template_versions(template_id,version,title,prompt_template,status)
                              SELECT id,version,title,prompt_template,status FROM templates
                              WHERE id=? AND NOT EXISTS (SELECT 1 FROM template_versions WHERE template_id=templates.id)""",
                           (template_id,))
            connectors = [
                ("doubao", "豆包模板", "doubao", "manual", 0, "pending", ""),
                ("chatgpt-templates", "ChatGPT 生图模板", "chatgpt", "manual", 0, "pending", ""),
                ("jimmeng", "即梦模板", "jimmeng", "manual", 0, "pending", ""),
            ]
            db.executemany(
                "INSERT OR IGNORE INTO connectors(id,name,platform,mode,enabled,authorization_status,endpoint) VALUES(?,?,?,?,?,?,?)",
                connectors,
            )

    def _size(self, payload: dict) -> dict:
        module = payload.get("module")
        if module not in MODULES:
            raise ValidationError("不支持的图片模块")
        if payload.get("preset", True) and not payload.get("width") and not payload.get("height"):
            return dict(MODULES[module]["preset"])
        try:
            width, height = int(payload["width"]), int(payload["height"])
        except (KeyError, TypeError, ValueError):
            raise ValidationError("自定义尺寸需要提供 width 和 height")
        ratio = width / height if height else 0
        if not (MIN_SIZE <= width <= MAX_SIZE and MIN_SIZE <= height <= MAX_SIZE and MIN_RATIO <= ratio <= MAX_RATIO):
            raise ValidationError(f"尺寸超出安全范围（{MIN_SIZE}-{MAX_SIZE}px，比例 {MIN_RATIO}-{MAX_RATIO}）")
        return {"width": width, "height": height}

    @staticmethod
    def _risk_flags(title: str, prompt_template: str) -> list[dict]:
        text = f"{title}\n{prompt_template}".lower()
        return [{"code": code, "label": label} for code, label, terms in RISK_RULES if any(term in text for term in terms)]

    @staticmethod
    def _validate_endpoint(value: str) -> str:
        endpoint = str(value or "").strip()
        if endpoint:
            parsed = urlparse(endpoint)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValidationError("接口地址必须是 http(s) URL，API Key 请放在环境变量中")
        return endpoint

    @staticmethod
    def _normalize_module_scope(value) -> str:
        if value is None:
            return ""
        if isinstance(value, (list, tuple, set)):
            raw_values = value
        else:
            raw_values = str(value).replace("，", ",").split(",")
        modules = []
        for raw in raw_values:
            module = str(raw).strip()
            if not module:
                continue
            if module not in MODULES:
                raise ValidationError("模型模块范围包含不支持的模块")
            if module not in modules:
                modules.append(module)
        return ",".join(modules)

    def _model(self, model_id: str, module: str | None = None):
        with self._connect() as db:
            row = db.execute("SELECT * FROM models WHERE id=? AND enabled=1", (model_id,)).fetchone()
        if not row:
            raise ValidationError("模型未启用或不存在")
        if row["role"] != "image":
            raise ValidationError("当前模型是理解/审核模型，不能直接用于生图")
        if module not in MODULES:
            raise ValidationError("不支持的图片模块")
        scoped_modules = {item for item in str(row["module_scope"] or "").split(",") if item}
        if scoped_modules and module not in scoped_modules:
            raise ValidationError("模型未绑定当前模块")
        return row

    def _check_daily_limit(self, model, payload: dict) -> None:
        limit = int(model["daily_limit"] or 0)
        if limit <= 0:
            return
        user_id = payload.get("user_id", "anonymous")
        with self._connect() as db:
            used = db.execute(
                "SELECT COUNT(*) FROM usage WHERE model_id=? AND user_id=? AND status='succeeded' AND date(created_at)=date('now')",
                (model["id"], user_id),
            ).fetchone()[0]
        if used >= limit:
            raise ValidationError(f"模型今日额度已用完（{limit} 次）")

    def daily_quota(self, user_id: str = "demo") -> dict:
        user_id = str(user_id or "anonymous")
        with self._connect() as db:
            used = db.execute(
                "SELECT COUNT(*) FROM generations WHERE user_id=? AND status='succeeded' AND date(created_at)=date('now')",
                (user_id,),
            ).fetchone()[0]
        used = int(used or 0)
        return {"limit": FREE_DAILY_LIMIT, "used": used, "remaining": max(0, FREE_DAILY_LIMIT - used)}

    def _check_user_daily_quota(self, payload: dict) -> None:
        quota = self.daily_quota(payload.get("user_id", "anonymous"))
        if quota["remaining"] <= 0:
            raise ValidationError(f"今日免费额度已用完（{quota['limit']} 张）")

    def _reference_image(self, payload: dict) -> str:
        reference = str(payload.get("reference_image") or "").strip()
        if not reference:
            return ""
        if reference.startswith("data:image/"):
            if len(reference) > 14 * 1024 * 1024:
                raise ValidationError("参考图片不能超过 10 MB")
            return reference
        if reference.startswith(("https://", "http://")):
            return reference
        raise ValidationError("参考图片必须是图片 Base64 或 http(s) URL")

    def _model_candidates(self, model, module: str) -> list:
        candidates = [model]
        seen = {model["id"]}
        fallback_id = str(model["fallback_model_id"] or "")
        while fallback_id and fallback_id not in seen:
            with self._connect() as db:
                fallback = db.execute("SELECT * FROM models WHERE id=? AND enabled=1", (fallback_id,)).fetchone()
            if not fallback or fallback["role"] != "image":
                break
            scope = {item for item in str(fallback["module_scope"] or "").split(",") if item}
            if scope and module not in scope:
                break
            candidates.append(fallback)
            seen.add(fallback_id)
            fallback_id = str(fallback["fallback_model_id"] or "")
        return candidates

    def _record_failed_attempt(self, payload: dict, model, template_id: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO usage(module,provider,model_id,template_id,user_id,status,tokens,credits,cost) VALUES(?,?,?,?,?,?,?,?,?)",
                (payload["module"], model["provider"], model["id"], template_id,
                 payload.get("user_id", "anonymous"), "failed", 0, 0, 0),
            )

    def _generate_one(self, model, payload: dict, template_id: str, size: dict) -> dict:
        model_id = model["id"]
        if model["adapter"] == "qwen_http":
            return self._generate_qwen(model, payload, template_id, size)
        if model["adapter"] == "sensenova_http":
            return self._generate_sensenova(model, payload, template_id, size)
        if model["adapter"] == "doubao_http":
            return self._generate_doubao(model, payload, template_id, size)
        generation_id = uuid.uuid4().hex[:12]
        output = self.output_dir / f"{generation_id}.svg"
        prompt = str(payload.get("prompt") or "").strip()[:160]
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{size["width"]}" height="{size["height"]}" '
            f'viewBox="0 0 {size["width"]} {size["height"]}">'
            '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
            '<stop stop-color="#33246f"/><stop offset="1" stop-color="#f18f6c"/></linearGradient></defs>'
            '<rect width="100%" height="100%" fill="url(#g)"/>'
            f'<text x="50%" y="50%" fill="white" text-anchor="middle" dominant-baseline="middle" '
            f'font-size="{max(28, min(size["width"], size["height"]) // 14)}" font-family="sans-serif">'
            f'{_xml(prompt or MODULES[payload["module"]]["name"])}</text></svg>'
        )
        output.write_text(svg, encoding="utf-8")
        with self._connect() as db:
            db.execute(
                "INSERT INTO usage(module,provider,model_id,template_id,user_id,status,tokens,credits,cost) VALUES(?,?,?,?,?,?,?,?,?)",
                (payload["module"], model["provider"], model_id, template_id, payload.get("user_id", "anonymous"),
                 "succeeded", 0, 1, float(model["cost"])),
            )
        provider = model["provider"]
        result = {"id": generation_id, "status": "succeeded", "module": payload["module"], "model_id": model_id,
                  "size": size, "output_path": str(output), "output_url": f"/outputs/{output.name}", "credits": 1,
                  "runtime": {"mode": "mock", "label": MOCK_LABEL, "actual_call": False,
                               "provider": provider, "provider_name": PROVIDER_NAMES.get(provider, provider),
                               "model_name": model["name"], "model_id": model_id,
                               "endpoint_configured": bool(model["endpoint"]),
                               "reference_image_attached": bool(payload.get("reference_image")),
                               "negative_prompt_attached": bool(payload.get("negative_prompt"))}}
        self._record_generation(payload, result)
        return result

    def generate(self, payload: dict) -> dict:
        size = self._size(payload)
        requested_model_id = payload.get("model_id") or "doubao-seedream"
        model = self._model(requested_model_id, payload.get("module"))
        template_id = payload.get("template_id", "")
        with self._connect() as db:
            template = db.execute(
                "SELECT * FROM templates WHERE id=? AND module=? AND status='published'",
                (template_id, payload.get("module")),
            ).fetchone()
        if not template:
            raise ValidationError("模板不存在或尚未发布")
        self._check_user_daily_quota(payload)
        reference_image = self._reference_image(payload)
        prompt = str(payload.get("prompt") or "").strip()
        negative_prompt = str(payload.get("negative_prompt") or "").strip()[:1000]
        template_prompt = str(template["prompt_template"] or "").strip()
        composed_prompt = "\n".join(part for part in (template_prompt, prompt) if part)[:2000]
        generation_payload = {**payload, "prompt": composed_prompt, "negative_prompt": negative_prompt}
        if reference_image:
            generation_payload["reference_image"] = reference_image
        attempts = []
        last_error = None
        for candidate in self._model_candidates(model, payload["module"]):
            try:
                self._check_daily_limit(candidate, payload)
            except ValidationError as exc:
                last_error = exc
                continue
            for attempt_index in range(int(candidate["retry_count"] or 0) + 1):
                attempt_payload = {**generation_payload, "model_id": candidate["id"]}
                try:
                    result = self._generate_one(candidate, attempt_payload, template_id, size)
                    result["runtime"]["requested_model_id"] = requested_model_id
                    result["runtime"]["attempts"] = [*attempts, {"model_id": candidate["id"], "attempt": attempt_index + 1, "status": "succeeded"}]
                    return result
                except ValidationError as exc:
                    self._record_failed_attempt(attempt_payload, candidate, template_id)
                    attempts.append({"model_id": candidate["id"], "attempt": attempt_index + 1, "status": "failed", "error": str(exc)[:240]})
                    last_error = exc
        if last_error:
            raise last_error
        raise ValidationError("没有可用的生图模型")

    def _record_generation(self, payload: dict, result: dict) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO generations(id,module,template_id,user_id,model_id,status,prompt,negative_prompt,output_path,output_url,width,height,runtime_mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (result["id"], payload["module"], payload.get("template_id", ""), payload.get("user_id", "anonymous"),
                 result["model_id"], result["status"], str(payload.get("prompt") or "").strip()[:2000], str(payload.get("negative_prompt") or "").strip()[:1000], result["output_path"],
                 result["output_url"], result["size"]["width"], result["size"]["height"], result["runtime"]["mode"]),
            )

    def list_generations(self, user_id: str = "demo", module: str | None = None) -> list[dict]:
        where, args = ["user_id=?"], [user_id]
        if module:
            where.append("module=?")
            args.append(module)
        with self._connect() as db:
            rows = db.execute("SELECT * FROM generations WHERE " + " AND ".join(where) + " ORDER BY created_at DESC", args).fetchall()
        return [dict(row) for row in rows]

    def delete_generation(self, generation_id: str, user_id: str = "demo") -> dict:
        with self._connect() as db:
            row = db.execute("SELECT * FROM generations WHERE id=?", (generation_id,)).fetchone()
            if not row:
                raise ValidationError("生成记录不存在")
            if row["user_id"] != user_id:
                raise AuthorizationError("无权删除该生成记录")
            output_path = Path(row["output_path"]).resolve()
            output_root = self.output_dir.resolve()
            if output_path.parent != output_root:
                raise ValidationError("生成文件路径不合法")
            try:
                output_path.unlink(missing_ok=True)
            except OSError as exc:
                raise ValidationError("生成文件删除失败") from exc
            db.execute("DELETE FROM generations WHERE id=?", (generation_id,))
        self._audit("generation.delete", "generation", generation_id, user_id, {"output_url": row["output_url"]})
        return {"id": generation_id, "status": "deleted"}

    def cleanup_expired_generations(self, retention_days: int = 7) -> dict:
        try:
            retention_days = int(retention_days)
        except (TypeError, ValueError) as exc:
            raise ValidationError("图片保留天数不正确") from exc
        if retention_days < 1 or retention_days > 3650:
            raise ValidationError("图片保留天数必须在 1 到 3650 天之间")
        with self._connect() as db:
            rows = db.execute(
                "SELECT id,output_path FROM generations WHERE created_at < datetime('now', ?)",
                (f"-{retention_days} days",),
            ).fetchall()
            deleted = 0
            missing_files = 0
            output_root = self.output_dir.resolve()
            for row in rows:
                output_path = Path(row["output_path"]).resolve()
                if output_path.parent != output_root:
                    continue
                try:
                    if output_path.exists():
                        output_path.unlink()
                    else:
                        missing_files += 1
                except OSError:
                    continue
                db.execute("DELETE FROM generations WHERE id=?", (row["id"],))
                deleted += 1
        if deleted:
            self._audit("generation.cleanup", "generation", "expired", "system", {
                "retention_days": retention_days, "deleted": deleted, "missing_files": missing_files,
            })
        return {"deleted": deleted, "missing_files": missing_files, "retention_days": retention_days}

    def _generate_qwen(self, model, payload: dict, template_id: str, size: dict) -> dict:
        endpoint = os.getenv("QWEN_ENDPOINT") or model["endpoint"]
        api_key = os.getenv("QWEN_API_KEY")
        if not endpoint or not api_key:
            raise ValidationError("千问真实适配器未配置：请设置 QWEN_API_KEY 和 QWEN_ENDPOINT")
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            raise ValidationError("真实生图需要提供提示词")
        request_body = json.dumps({
            "model": model["remote_model_id"] or "qwen-image-3.0-pro",
            "prompt": prompt[:2000],
            "size": f"{size['width']}x{size['height']}",
            "n": 1,
            **({"image": payload["reference_image"]} if payload.get("reference_image") else {}),
            **({"negative_prompt": payload["negative_prompt"]} if payload.get("negative_prompt") else {}),
        }).encode("utf-8")
        request = urllib.request.Request(endpoint, data=request_body, headers={
            "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
        }, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=int(model["timeout_seconds"] or 120)) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ValidationError(f"千问真实生图调用失败：{exc}") from exc
        image_url = ((body.get("data") or [{}])[0]).get("url")
        if not image_url:
            raise ValidationError("千问真实生图返回中没有图片 URL")
        usage_meta = self._parse_provider_usage(body, model)
        generation_id = uuid.uuid4().hex[:12]
        output = self.output_dir / f"{generation_id}.png"
        try:
            with urllib.request.urlopen(image_url, timeout=int(model["timeout_seconds"] or 120)) as response:
                output.write_bytes(response.read())
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ValidationError(f"下载千问图片失败：{exc}") from exc
        with self._connect() as db:
            db.execute(
                "INSERT INTO usage(module,provider,model_id,template_id,user_id,status,tokens,credits,cost) VALUES(?,?,?,?,?,?,?,?,?)",
                (payload["module"], model["provider"], payload.get("model_id", model["id"]), template_id,
                 payload.get("user_id", "anonymous"), "succeeded", usage_meta["tokens"], 1, usage_meta["cost"]),
            )
        result = {"id": generation_id, "status": "succeeded", "module": payload["module"],
                  "model_id": payload.get("model_id", model["id"]), "size": size,
                  "output_path": str(output), "output_url": f"/outputs/{output.name}", "credits": 1,
                  "runtime": {"mode": "real", "label": "真实调用", "actual_call": True,
                               "provider": model["provider"], "provider_name": PROVIDER_NAMES.get(model["provider"], model["provider"]),
                               "model_name": model["name"], "remote_model_id": model["remote_model_id"],
                               "model_id": payload.get("model_id", model["id"]), "endpoint_configured": True,
                               "usage": usage_meta, "reference_image_attached": bool(payload.get("reference_image")),
                               "negative_prompt_attached": bool(payload.get("negative_prompt"))}}
        self._record_generation(payload, result)
        return result

    def _generate_doubao(self, model, payload: dict, template_id: str, size: dict) -> dict:
        """Call Volcengine Ark's OpenAI-compatible Seedream image endpoint.

        The API key is deliberately read from an environment variable and is
        never persisted in the database or returned to the browser.  The
        endpoint can be overridden per model; otherwise ARK_ENDPOINT or the
        official Beijing Ark endpoint is used.
        """
        endpoint = str(model["endpoint"] or os.getenv("ARK_ENDPOINT") or
                       "https://ark.cn-beijing.volces.com/api/v3/images/generations").rstrip("/")
        if not endpoint.endswith("/images/generations"):
            endpoint = f"{endpoint}/images/generations"
        auth_env = model["auth_env"] or "ARK_API_KEY"
        api_key = os.getenv(auth_env)
        if not api_key:
            raise ValidationError(f"豆包真实适配器未配置：请设置 {auth_env}")
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            raise ValidationError("真实生图需要提供提示词")
        request_body = {
            "model": model["remote_model_id"] or "doubao-seedream-5-0-lite-260128",
            "prompt": prompt[:2000],
            "size": f"{size['width']}x{size['height']}",
            "response_format": "url",
            "watermark": False,
        }
        if payload.get("reference_image"):
            request_body["image"] = payload["reference_image"]
        request = urllib.request.Request(endpoint, data=json.dumps(request_body).encode("utf-8"), headers={
            "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
        }, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=int(model["timeout_seconds"] or 120)) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = f"HTTP {exc.code}"
            try:
                error_body = json.loads(exc.read().decode("utf-8", "replace"))
                if isinstance(error_body, dict):
                    provider_code = str(error_body.get("code") or error_body.get("error", {}).get("code") or "").strip()
                    provider_message = str(error_body.get("message") or error_body.get("error", {}).get("message") or "").strip()
                    if provider_code:
                        detail += f" {provider_code}"
                    if provider_message:
                        detail += f": {provider_message}"
            except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
                pass
            raise ValidationError(f"豆包真实生图调用失败：{detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ValidationError(f"豆包真实生图调用失败：{exc}") from exc
        image_url, image_bytes = self._extract_image(body)
        if not image_url and not image_bytes:
            raise ValidationError("豆包真实生图返回中没有图片 URL 或 Base64 图片")
        usage_meta = self._parse_provider_usage(body, model)
        generation_id = uuid.uuid4().hex[:12]
        output = self.output_dir / f"{generation_id}.png"
        try:
            if image_bytes:
                output.write_bytes(image_bytes)
            else:
                with urllib.request.urlopen(image_url, timeout=int(model["timeout_seconds"] or 120)) as response:
                    output.write_bytes(response.read())
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ValidationError(f"下载豆包图片失败：{exc}") from exc
        with self._connect() as db:
            db.execute(
                "INSERT INTO usage(module,provider,model_id,template_id,user_id,status,tokens,credits,cost) VALUES(?,?,?,?,?,?,?,?,?)",
                (payload["module"], model["provider"], payload.get("model_id", model["id"]), template_id,
                 payload.get("user_id", "anonymous"), "succeeded", usage_meta["tokens"], 1, usage_meta["cost"]),
            )
        result = {"id": generation_id, "status": "succeeded", "module": payload["module"],
                  "model_id": payload.get("model_id", model["id"]), "size": size,
                  "output_path": str(output), "output_url": f"/outputs/{output.name}", "credits": 1,
                  "runtime": {"mode": "real", "label": "真实调用", "actual_call": True,
                               "provider": model["provider"], "provider_name": PROVIDER_NAMES.get(model["provider"], model["provider"]),
                               "model_name": model["name"], "remote_model_id": model["remote_model_id"],
                               "model_id": payload.get("model_id", model["id"]), "endpoint_configured": True,
                               "usage": usage_meta, "reference_image_attached": bool(payload.get("reference_image")),
                               "negative_prompt_attached": bool(payload.get("negative_prompt"))}}
        self._record_generation(payload, result)
        return result

    def _generate_sensenova(self, model, payload: dict, template_id: str, size: dict) -> dict:
        endpoint = model["endpoint"] or os.getenv("SENSENOVA_ENDPOINT")
        auth_env = model["auth_env"] or "SENSENOVA_API_KEY"
        api_key = os.getenv(auth_env)
        if not endpoint or not api_key:
            raise ValidationError(f"商汤真实适配器未配置：请设置 {auth_env} 和接口地址")
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            raise ValidationError("真实生图需要提供提示词")
        reference_image = payload.get("reference_image")
        endpoint = str(endpoint).rstrip("/")
        is_openai_image = endpoint.endswith("/v1") or "/images/generations" in endpoint or "/images/edits" in endpoint
        if is_openai_image:
            if endpoint.endswith("/v1"):
                endpoint = f"{endpoint}/images/edits" if reference_image else f"{endpoint}/images/generations"
            elif reference_image and endpoint.endswith("/images/generations"):
                endpoint = f"{endpoint[:-len('/images/generations')]}/images/edits"
            elif not reference_image and endpoint.endswith("/images/edits"):
                endpoint = f"{endpoint[:-len('/images/edits')]}/images/generations"
            request_body = {
                "model": model["remote_model_id"] or "sensenova-u1.5-lite",
                "prompt": prompt[:2000],
                "size": f"{size['width']}x{size['height']}",
                "n": 1,
                "watermark": False,
                "prompt_extend": True,
                "response_format": "url",
            }
            if reference_image:
                request_body["images"] = [{"image_url": reference_image}]
        else:
            request_body = {
                "model": model["remote_model_id"] or "sensenova-u1.5-lite",
                "prompt": prompt[:2000],
                "size": f"{size['width']}x{size['height']}",
                "n": 1,
            }
            if payload.get("negative_prompt"):
                request_body["negative_prompt"] = payload["negative_prompt"]
            if reference_image:
                request_body["image"] = reference_image
        request = urllib.request.Request(endpoint, data=json.dumps(request_body).encode("utf-8"), headers={
            "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
        }, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=int(model["timeout_seconds"] or 120)) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = f"HTTP {exc.code}"
            try:
                error_body = json.loads(exc.read().decode("utf-8", "replace"))
                if isinstance(error_body, dict):
                    provider_code = str(error_body.get("code") or "").strip()
                    provider_message = str(error_body.get("message") or "").strip()
                    if provider_code:
                        detail += f" {provider_code}"
                    if provider_message:
                        detail += f": {provider_message}"
            except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
                pass
            raise ValidationError(f"商汤真实生图调用失败：{detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ValidationError(f"商汤真实生图调用失败：{exc}") from exc
        image_url, image_bytes = self._extract_image(body)
        if not image_url and not image_bytes:
            raise ValidationError("商汤真实生图返回中没有图片 URL 或 Base64 图片")
        usage_meta = self._parse_provider_usage(body, model)
        generation_id = uuid.uuid4().hex[:12]
        output = self.output_dir / f"{generation_id}.png"
        try:
            if image_bytes:
                output.write_bytes(image_bytes)
            else:
                with urllib.request.urlopen(image_url, timeout=int(model["timeout_seconds"] or 120)) as response:
                    output.write_bytes(response.read())
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ValidationError(f"下载商汤图片失败：{exc}") from exc
        with self._connect() as db:
            db.execute(
                "INSERT INTO usage(module,provider,model_id,template_id,user_id,status,tokens,credits,cost) VALUES(?,?,?,?,?,?,?,?,?)",
                (payload["module"], model["provider"], payload.get("model_id", model["id"]), template_id,
                 payload.get("user_id", "anonymous"), "succeeded", usage_meta["tokens"], 1, usage_meta["cost"]),
            )
        result = {"id": generation_id, "status": "succeeded", "module": payload["module"],
                  "model_id": payload.get("model_id", model["id"]), "size": size,
                  "output_path": str(output), "output_url": f"/outputs/{output.name}", "credits": 1,
                  "runtime": {"mode": "real", "label": "真实调用", "actual_call": True,
                               "provider": model["provider"], "provider_name": PROVIDER_NAMES.get(model["provider"], model["provider"]),
                               "model_name": model["name"], "remote_model_id": model["remote_model_id"],
                               "model_id": payload.get("model_id", model["id"]), "endpoint_configured": True,
                               "usage": usage_meta, "reference_image_attached": bool(payload.get("reference_image")),
                               "negative_prompt_attached": bool(payload.get("negative_prompt"))}}
        self._record_generation(payload, result)
        return result

    @staticmethod
    def _extract_image(body: dict) -> tuple[str, bytes]:
        entries = body.get("data") or body.get("images") or []
        if isinstance(entries, dict):
            entries = [entries]
        first = entries[0] if entries and isinstance(entries[0], dict) else {}
        image_url = first.get("url") or first.get("image_url") or body.get("url") or body.get("image_url") or ""
        encoded = first.get("b64_json") or first.get("base64") or body.get("b64_json") or ""
        if encoded:
            try:
                return str(image_url), base64.b64decode(encoded)
            except (ValueError, TypeError):
                return str(image_url), b""
        return str(image_url), b""

    @staticmethod
    def _parse_provider_usage(body: dict, model) -> dict:
        usage = body.get("usage") or {}
        tokens = usage.get("total_tokens") or usage.get("tokens") or usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        reported_cost = usage.get("cost") or usage.get("total_cost")
        try:
            tokens = int(tokens or 0)
        except (TypeError, ValueError):
            tokens = 0
        try:
            cost = float(reported_cost) if reported_cost is not None else float(model["cost"])
        except (TypeError, ValueError):
            cost = float(model["cost"])
        return {"tokens": tokens, "cost": cost, "source": "provider" if reported_cost is not None or tokens else "model_config"}

    def list_templates(self, module: str | None = None) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM templates WHERE status='published'" + (" AND module=?" if module else ""),
                              (module,) if module else ()).fetchall()
        return [dict(row) for row in rows]

    def list_admin_templates(self, module: str | None = None) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM templates" + (" WHERE module=?" if module else "") + " ORDER BY created_at DESC",
                              (module,) if module else ()).fetchall()
        return [dict(row) for row in rows]

    def list_template_versions(self, template_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM template_versions WHERE template_id=? ORDER BY version DESC", (template_id,)).fetchall()
        return [dict(row) for row in rows]

    def update_template(self, template_id: str, payload: dict) -> dict:
        allowed = {"title", "prompt_template", "status"}
        fields = {key: payload[key] for key in allowed if key in payload}
        if not fields:
            raise ValidationError("没有可更新的模板配置")
        if "title" in fields:
            fields["title"] = str(fields["title"]).strip()[:120]
            if not fields["title"]:
                raise ValidationError("模板名称不能为空")
        if "prompt_template" in fields:
            fields["prompt_template"] = str(fields["prompt_template"]).strip()[:2000]
        if "status" in fields and fields["status"] not in {"draft", "published", "archived"}:
            raise ValidationError("模板状态不合法")
        with self._connect() as db:
            current = db.execute("SELECT * FROM templates WHERE id=?", (template_id,)).fetchone()
            if not current:
                raise ValidationError("模板不存在")
            version = int(current["version"]) + 1
            assignments = ",".join(f"{key}=?" for key in fields)
            db.execute(f"UPDATE templates SET {assignments}, version=? WHERE id=?",
                       [*fields.values(), version, template_id])
            updated = db.execute("SELECT * FROM templates WHERE id=?", (template_id,)).fetchone()
            db.execute("INSERT INTO template_versions(template_id,version,title,prompt_template,status) VALUES(?,?,?,?,?)",
                       (template_id, version, updated["title"], updated["prompt_template"], updated["status"]))
        result = dict(updated)
        self._audit("template.update", "template", template_id, str(payload.get("actor") or "admin"),
                    {"version": result["version"], "status": result["status"]})
        return result

    def rollback_template(self, template_id: str, version: int, actor: str = "admin") -> dict:
        try:
            version = int(version)
        except (TypeError, ValueError) as exc:
            raise ValidationError("版本号不正确") from exc
        with self._connect() as db:
            current = db.execute("SELECT * FROM templates WHERE id=?", (template_id,)).fetchone()
            snapshot = db.execute("SELECT * FROM template_versions WHERE template_id=? AND version=?", (template_id, version)).fetchone()
            if not current:
                raise ValidationError("模板不存在")
            if not snapshot:
                raise ValidationError("模板版本不存在")
            new_version = int(current["version"]) + 1
            db.execute("UPDATE templates SET title=?, prompt_template=?, status=?, version=? WHERE id=?",
                       (snapshot["title"], snapshot["prompt_template"], snapshot["status"], new_version, template_id))
            db.execute("INSERT INTO template_versions(template_id,version,title,prompt_template,status) VALUES(?,?,?,?,?)",
                       (template_id, new_version, snapshot["title"], snapshot["prompt_template"], snapshot["status"]))
            updated = db.execute("SELECT * FROM templates WHERE id=?", (template_id,)).fetchone()
        result = dict(updated)
        self._audit("template.rollback", "template", template_id, actor,
                    {"from_version": version, "new_version": new_version})
        return result

    def ingest_candidate(self, payload: dict) -> dict:
        module = payload.get("module")
        if module not in MODULES:
            raise ValidationError("不支持的图片模块")
        source_url = str(payload.get("source_url") or "").strip()
        if not source_url.startswith(("https://", "http://")):
            raise ValidationError("来源地址必须是 http(s) URL")
        candidate_id = uuid.uuid4().hex[:12]
        batch_id = payload.get("batch_id") or f"batch-{uuid.uuid4().hex[:8]}"
        fingerprint = payload.get("fingerprint") or uuid.uuid5(uuid.NAMESPACE_URL, source_url).hex
        connector_id = str(payload.get("connector_id") or "").strip()
        prompt_template = str(payload.get("prompt_template") or payload.get("prompt") or "").strip()[:2000]
        risk_flags = self._risk_flags(str(payload.get("title") or "未命名模板"), prompt_template)
        with self._connect() as db:
            if connector_id:
                connector = db.execute("SELECT * FROM connectors WHERE id=?", (connector_id,)).fetchone()
                if not connector:
                    raise ValidationError("来源连接器不存在")
                if connector["authorization_status"] != "approved":
                    raise ValidationError("来源连接器尚未授权")
                if connector["mode"] == "official_api" and not connector["enabled"]:
                    raise ValidationError("官方 API 连接器尚未启用")
            duplicate = db.execute("SELECT id,status,batch_id FROM candidates WHERE fingerprint=?", (fingerprint,)).fetchone()
            if duplicate:
                return {"id": duplicate["id"], "status": "duplicate", "existing_status": duplicate["status"], "batch_id": duplicate["batch_id"]}
            db.execute("""INSERT INTO candidates(id,module,title,prompt_template,risk_flags,source_url,copyright_status,fingerprint,batch_id,status,reviewer,connector_id)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (candidate_id, module, payload.get("title") or "未命名模板", prompt_template, json.dumps(risk_flags, ensure_ascii=False), source_url,
                        payload.get("copyright_status", "pending"), fingerprint, batch_id, "pending_review", "", connector_id))
        self._audit("candidate.ingest", "candidate", candidate_id, str(payload.get("actor") or "admin"),
                    {"module": module, "batch_id": batch_id, "connector_id": connector_id,
                     "source_url": source_url, "fingerprint": fingerprint})
        return {"id": candidate_id, "status": "pending_review", "batch_id": batch_id, "connector_id": connector_id}

    def ingest_candidates_batch(self, payload: dict) -> dict:
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            raise ValidationError("批量导入需要提供非空 items 数组")
        if len(items) > 100:
            raise ValidationError("单批最多导入 100 个模板")
        batch_id = str(payload.get("batch_id") or f"batch-{uuid.uuid4().hex[:8]}")
        connector_id = str(payload.get("connector_id") or "")
        results = []
        for item in items:
            if not isinstance(item, dict):
                results.append({"status": "rejected", "error": "条目必须是对象"})
                continue
            results.append(self.ingest_candidate({**item, "batch_id": batch_id, "connector_id": connector_id}))
        return {"batch_id": batch_id, "total": len(results), "created": sum(item["status"] == "pending_review" for item in results),
                "duplicates": sum(item["status"] == "duplicate" for item in results), "results": results}

    def review_candidate(self, candidate_id: str, decision: str, reviewer: str, reason: str = "") -> dict:
        if decision not in {"approved", "rejected"}:
            raise ValidationError("审核结论只能是 approved 或 rejected")
        with self._connect() as db:
            row = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            if not row:
                raise ValidationError("候选模板不存在")
            if row["status"] != "pending_review":
                raise ValidationError("候选模板已经处理，不能重复审核")
            status = "published" if decision == "approved" else "rejected"
            db.execute("UPDATE candidates SET status=?, reviewer=?, reviewed_at=CURRENT_TIMESTAMP, review_reason=? WHERE id=?",
                       (status, reviewer, str(reason or "").strip()[:500], candidate_id))
            reviewed_at = db.execute("SELECT reviewed_at FROM candidates WHERE id=?", (candidate_id,)).fetchone()["reviewed_at"]
            if decision == "approved":
                template_id = f"candidate-{candidate_id}"
                db.execute("INSERT INTO templates(id,module,title,source_url,status,copyright_status,prompt_template) VALUES(?,?,?,?,?,?,?)",
                           (template_id, row["module"], row["title"], row["source_url"], "published", row["copyright_status"], row["prompt_template"]))
                db.execute("INSERT INTO template_versions(template_id,version,title,prompt_template,status) VALUES(?,?,?,?,?)",
                           (template_id, 1, row["title"], row["prompt_template"], "published"))
                db.execute("UPDATE candidates SET publish_status='internal_published', publish_result=?, published_at=CURRENT_TIMESTAMP WHERE id=?",
                           (json.dumps({"mode": "internal", "template_id": template_id}, ensure_ascii=False), candidate_id))
            else:
                db.execute("UPDATE candidates SET publish_status='not_published', publish_result='{}', published_at=NULL, publish_connector_id='' WHERE id=?",
                           (candidate_id,))
        self._audit(f"candidate.{decision}", "candidate", candidate_id, reviewer,
                    {"status": status, "source_url": row["source_url"], "reason": str(reason or "").strip()[:500],
                     "template_id": f"candidate-{candidate_id}" if decision == "approved" else ""})
        return {"id": candidate_id, "status": status, "reviewed_at": reviewed_at,
                "publish_status": "internal_published" if decision == "approved" else "not_published"}

    def review_candidates_batch(self, payload: dict) -> dict:
        candidate_ids = payload.get("candidate_ids")
        if not isinstance(candidate_ids, list) or not candidate_ids:
            raise ValidationError("批量审核需要提供 candidate_ids 数组")
        candidate_ids = list(dict.fromkeys(str(item).strip() for item in candidate_ids if str(item).strip()))
        if len(candidate_ids) > 100:
            raise ValidationError("单批最多审核 100 个候选模板")
        decision = str(payload.get("decision") or "").strip()
        reviewer = str(payload.get("reviewer") or "admin").strip() or "admin"
        reason = str(payload.get("reason") or "").strip()[:500]
        if decision not in {"approved", "rejected"}:
            raise ValidationError("审核结论只能是 approved 或 rejected")
        if decision == "rejected" and not reason:
            raise ValidationError("批量拒绝需要填写原因")
        results = []
        for candidate_id in candidate_ids:
            try:
                results.append(self.review_candidate(candidate_id, decision, reviewer, reason))
            except ValidationError as exc:
                results.append({"id": candidate_id, "status": "error", "error": str(exc)})
        processed = sum(item.get("status") in {"published", "rejected"} for item in results)
        failed = len(results) - processed
        self._audit("candidate.batch_review", "candidate_batch", ",".join(candidate_ids), reviewer,
                    {"decision": decision, "processed": processed, "failed": failed})
        return {"decision": decision, "total": len(results), "processed": processed, "failed": failed, "results": results}

    def publish_candidate(self, candidate_id: str, connector_id: str, actor: str = "admin") -> dict:
        connector_id = str(connector_id or "").strip()
        if not connector_id:
            raise ValidationError("推送需要指定来源连接器")
        with self._connect() as db:
            candidate = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            connector = db.execute("SELECT * FROM connectors WHERE id=?", (connector_id,)).fetchone()
        if not candidate:
            raise ValidationError("候选模板不存在")
        if candidate["status"] != "published":
            raise ValidationError("只有已审核通过的模板才能推送")
        if not connector:
            raise ValidationError("来源连接器不存在")
        if connector["authorization_status"] != "approved" or not connector["enabled"]:
            raise ValidationError("来源连接器未授权或未启用")
        if connector["mode"] != "official_api":
            raise ValidationError("只有官方 API 连接器支持推送")
        endpoint = str(connector["endpoint"] or "").strip()
        if not endpoint:
            raise ValidationError("来源连接器缺少推送接口地址")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        auth_env = str(connector["auth_env"] or "").strip()
        if auth_env:
            secret = os.getenv(auth_env)
            if not secret:
                raise ValidationError(f"来源连接器缺少环境变量 {auth_env}")
            headers["Authorization"] = f"Bearer {secret}"
        request_body = {
            "template_id": f"candidate-{candidate_id}", "module": candidate["module"],
            "title": candidate["title"], "prompt_template": candidate["prompt_template"],
            "source_url": candidate["source_url"], "copyright_status": candidate["copyright_status"],
        }
        request = urllib.request.Request(endpoint, data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = f"HTTP {exc.code}"
            with self._connect() as db:
                db.execute("UPDATE candidates SET publish_status='failed', publish_result=?, publish_connector_id=? WHERE id=?",
                           (json.dumps({"error": detail}, ensure_ascii=False), connector_id, candidate_id))
            raise ValidationError(f"模板推送失败：{detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            detail = str(exc)
            with self._connect() as db:
                db.execute("UPDATE candidates SET publish_status='failed', publish_result=?, publish_connector_id=? WHERE id=?",
                           (json.dumps({"error": detail[:300]}, ensure_ascii=False), connector_id, candidate_id))
            raise ValidationError(f"模板推送失败：{detail}") from exc
        if not isinstance(body, dict):
            body = {}
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        result = {"mode": "official_api", "status": str(body.get("status") or data.get("status") or "succeeded")[:80]}
        external_id = body.get("id") or data.get("id")
        if external_id:
            result["external_id"] = str(external_id)[:120]
        with self._connect() as db:
            db.execute("UPDATE candidates SET publish_status='pushed', publish_result=?, published_at=CURRENT_TIMESTAMP, publish_connector_id=? WHERE id=?",
                       (json.dumps(result, ensure_ascii=False), connector_id, candidate_id))
        self._audit("candidate.publish", "candidate", candidate_id, actor,
                    {"connector_id": connector_id, "status": result["status"], "external_id": result.get("external_id", "")})
        row = dict(candidate)
        row.update({"publish_status": "pushed", "publish_result": result, "publish_connector_id": connector_id})
        return row

    @staticmethod
    def _parse_chat_content(body: dict) -> dict:
        choices = body.get("choices") or []
        first = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        content = message.get("content") or first.get("text") or ""
        if isinstance(content, list):
            content = "".join(str(item.get("text") or "") if isinstance(item, dict) else str(item) for item in content)
        content = str(content).strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE | re.DOTALL).strip()
        start, end = content.find("{"), content.rfind("}")
        if start >= 0 and end > start:
            content = content[start:end + 1]
        try:
            result = json.loads(content)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValidationError("商汤审核模型返回的不是有效 JSON") from exc
        if not isinstance(result, dict):
            raise ValidationError("商汤审核模型返回格式不正确")
        verdict = str(result.get("verdict") or "").strip().lower()
        if verdict not in {"pass", "flag"}:
            raise ValidationError("商汤审核模型返回的 verdict 必须是 pass 或 flag")
        result["verdict"] = verdict
        for key in ("reasons", "categories"):
            value = result.get(key) or []
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list):
                value = []
            result[key] = [str(item).strip()[:200] for item in value if str(item).strip()][:10]
        return result

    def _sensenova_chat(self, model, messages: list[dict]) -> tuple[dict, dict]:
        endpoint = str(model["endpoint"] or os.getenv("SENSENOVA_BASE_URL") or "https://token.sensenova.cn/v1").rstrip("/")
        if endpoint.endswith("/v1"):
            endpoint += "/chat/completions"
        elif not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        auth_env = model["auth_env"] or "SENSENOVA_API_KEY"
        api_key = os.getenv(auth_env)
        if not api_key:
            raise ValidationError(f"商汤审核模型未配置：请设置 {auth_env}")
        request_body = {
            "model": model["remote_model_id"] or "sensenova-6.8-flash-lite",
            "messages": messages,
            "temperature": 0,
            "max_tokens": 512,
        }
        request = urllib.request.Request(endpoint, data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"), headers={
            "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
        }, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = f"HTTP {exc.code}"
            try:
                error_body = json.loads(exc.read().decode("utf-8", "replace"))
                if isinstance(error_body, dict):
                    code = str(error_body.get("code") or "").strip()
                    message = str(error_body.get("message") or "").strip()
                    if code:
                        detail += f" {code}"
                    if message:
                        detail += f": {message}"
            except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
                pass
            raise ValidationError(f"商汤审核模型调用失败：{detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ValidationError(f"商汤审核模型调用失败：{exc}") from exc
        return body, self._parse_provider_usage(body, model)

    def ai_review_candidate(self, candidate_id: str, actor: str = "admin") -> dict:
        with self._connect() as db:
            candidate = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            model = db.execute(
                "SELECT * FROM models WHERE enabled=1 AND role='understanding' AND provider='sensenova' AND adapter='sensenova_chat' ORDER BY id LIMIT 1"
            ).fetchone()
        if not candidate:
            raise ValidationError("候选模板不存在")
        if not model:
            raise ValidationError("没有已启用的商汤理解模型，请先配置 SenseNova 6.8 Flash Lite")
        messages = [
            {"role": "system", "content": '你是 AIGC 模板内容安全预审助手。只返回 JSON，不要 Markdown。格式：{"verdict":"pass"或"flag","reasons":["原因"],"categories":["分类"]}。pass 表示未发现明显风险，flag 表示需要人工重点审核。'},
            {"role": "user", "content": json.dumps({
                "title": candidate["title"], "prompt_template": candidate["prompt_template"],
                "module": candidate["module"], "source_url": candidate["source_url"],
            }, ensure_ascii=False)},
        ]
        try:
            body, usage_meta = self._sensenova_chat(model, messages)
            result = self._parse_chat_content(body)
        except ValidationError as exc:
            with self._connect() as db:
                db.execute("UPDATE candidates SET ai_review_status='error', ai_review_result=?, ai_reviewed_at=CURRENT_TIMESTAMP, ai_review_model_id=? WHERE id=?",
                           (json.dumps({"error": str(exc)}, ensure_ascii=False), model["id"], candidate_id))
            raise
        ai_status = "passed" if result["verdict"] == "pass" else "flagged"
        with self._connect() as db:
            db.execute("UPDATE candidates SET ai_review_status=?, ai_review_result=?, ai_reviewed_at=CURRENT_TIMESTAMP, ai_review_model_id=? WHERE id=?",
                       (ai_status, json.dumps(result, ensure_ascii=False), model["id"], candidate_id))
            db.execute("INSERT INTO usage(module,provider,model_id,template_id,user_id,status,tokens,credits,cost) VALUES(?,?,?,?,?,?,?,?,?)",
                       (candidate["module"], model["provider"], model["id"], candidate_id, str(actor or "admin"), "succeeded",
                        usage_meta["tokens"], 0, usage_meta["cost"]))
        self._audit("candidate.ai_review", "candidate", candidate_id, str(actor or "admin"),
                    {"model_id": model["id"], "status": ai_status, "verdict": result["verdict"]})
        result_row = dict(candidate)
        result_row.update({"ai_review_status": ai_status, "ai_review_result": result,
                           "ai_review_model_id": model["id"]})
        return result_row

    def candidates(self, status: str | None = None, module: str | None = None, batch_id: str | None = None) -> list[dict]:
        with self._connect() as db:
            query = "SELECT * FROM candidates"
            args, conditions = [], []
            if status:
                conditions.append("status=?")
                args.append(status)
            if module:
                conditions.append("module=?")
                args.append(module)
            if batch_id:
                conditions.append("batch_id=?")
                args.append(batch_id)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY created_at DESC"
            result = []
            for row in db.execute(query, args).fetchall():
                item = dict(row)
                try:
                    item["risk_flags"] = json.loads(item.get("risk_flags") or "[]")
                except json.JSONDecodeError:
                    item["risk_flags"] = []
                try:
                    item["ai_review_result"] = json.loads(item.get("ai_review_result") or "{}")
                except json.JSONDecodeError:
                    item["ai_review_result"] = {}
                try:
                    item["publish_result"] = json.loads(item.get("publish_result") or "{}")
                except json.JSONDecodeError:
                    item["publish_result"] = {}
                result.append(item)
            return result

    def review_summary(self) -> dict:
        with self._connect() as db:
            rows = db.execute("SELECT module,status,COUNT(*) count FROM candidates GROUP BY module,status").fetchall()
            oldest = db.execute("SELECT MIN(created_at) FROM candidates WHERE status='pending_review'").fetchone()[0]
        by_module = {}
        totals = {"pending_review": 0, "published": 0, "rejected": 0}
        for row in rows:
            by_module.setdefault(row["module"], {})[row["status"]] = row["count"]
            totals[row["status"]] = totals.get(row["status"], 0) + row["count"]
        return {"totals": totals, "by_module": by_module, "oldest_pending_at": oldest}

    def run_review_snapshot(self, payload: dict | None = None) -> dict:
        payload = payload or {}
        module = str(payload.get("module") or "").strip()
        batch_id = str(payload.get("batch_id") or "").strip()
        actor = str(payload.get("actor") or "admin").strip() or "admin"
        if module and module not in MODULES:
            raise ValidationError("不支持的图片模块")
        conditions, args = [], []
        if module:
            conditions.append("module=?")
            args.append(module)
        if batch_id:
            conditions.append("batch_id=?")
            args.append(batch_id)
        clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self._connect() as db:
            rows = db.execute(f"SELECT status,COUNT(*) count FROM candidates{clause} GROUP BY status", args).fetchall()
            totals = {"pending_review": 0, "published": 0, "rejected": 0}
            for row in rows:
                totals[row["status"]] = row["count"]
            run_id = uuid.uuid4().hex[:12]
            db.execute(
                "INSERT INTO review_runs(id,module,batch_id,pending_review,published,rejected,actor) VALUES(?,?,?,?,?,?,?)",
                (run_id, module, batch_id, totals["pending_review"], totals["published"], totals["rejected"], actor),
            )
            run = db.execute("SELECT * FROM review_runs WHERE id=?", (run_id,)).fetchone()
        result = dict(run)
        result["totals"] = {key: result[key] for key in ("pending_review", "published", "rejected")}
        self._audit("review.snapshot", "review_run", run_id, actor,
                    {"module": module, "batch_id": batch_id, "totals": result["totals"]})
        return result

    def list_review_runs(self, module: str | None = None, limit: int = 30) -> list[dict]:
        limit = max(1, min(int(limit), 100))
        with self._connect() as db:
            if module:
                rows = db.execute(
                    "SELECT * FROM review_runs WHERE module=? ORDER BY created_at DESC LIMIT ?", (module, limit)
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM review_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["totals"] = {key: item[key] for key in ("pending_review", "published", "rejected")}
            result.append(item)
        return result

    def list_connectors(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM connectors ORDER BY name").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["last_sync_result"] = json.loads(item.get("last_sync_result") or "{}")
            except json.JSONDecodeError:
                item["last_sync_result"] = {}
            item["auth_env_configured"] = bool(item.get("auth_env") and os.getenv(item["auth_env"]))
            result.append(item)
        return result

    def set_connector(self, connector_id: str, payload: dict) -> dict:
        allowed = {"enabled", "mode", "authorization_status", "endpoint", "auth_env"}
        fields = {key: payload[key] for key in allowed if key in payload}
        if not fields:
            raise ValidationError("没有可更新的来源配置")
        if "mode" in fields and fields["mode"] not in {"manual", "official_api"}:
            raise ValidationError("来源模式只能是 manual 或 official_api")
        if "authorization_status" in fields and fields["authorization_status"] not in {"pending", "approved", "rejected"}:
            raise ValidationError("授权状态不合法")
        if "endpoint" in fields:
            endpoint = str(fields["endpoint"] or "").strip()
            if endpoint and not endpoint.startswith(("https://", "http://")):
                raise ValidationError("接口地址必须是 http(s) URL")
            fields["endpoint"] = endpoint
        if "auth_env" in fields:
            auth_env = str(fields["auth_env"] or "").strip()
            if auth_env and not re.fullmatch(r"[A-Z][A-Z0-9_]*", auth_env):
                raise ValidationError("密钥环境变量名必须是大写字母、数字或下划线")
            fields["auth_env"] = auth_env
        with self._connect() as db:
            current = db.execute("SELECT * FROM connectors WHERE id=?", (connector_id,)).fetchone()
            if not current:
                raise ValidationError("来源连接器不存在")
            effective_enabled = fields.get("enabled", current["enabled"])
            effective_authorization = fields.get("authorization_status", current["authorization_status"])
            if effective_enabled and effective_authorization != "approved":
                raise ValidationError("来源未授权，不能启用")
            assignments = ",".join(f"{key}=?" for key in fields)
            db.execute(f"UPDATE connectors SET {assignments} WHERE id=?", [*fields.values(), connector_id])
            updated = db.execute("SELECT * FROM connectors WHERE id=?", (connector_id,)).fetchone()
        result = dict(updated)
        self._audit("connector.update", "connector", connector_id, str(payload.get("actor") or "admin"),
                    {key: value for key, value in fields.items() if key != "endpoint"})
        return result

    def check_connector(self, connector_id: str, actor: str = "admin") -> dict:
        with self._connect() as db:
            connector = db.execute("SELECT * FROM connectors WHERE id=?", (connector_id,)).fetchone()
        if not connector:
            raise ValidationError("来源连接器不存在")
        official = connector["mode"] == "official_api"
        auth_env = str(connector["auth_env"] or "").strip()
        checks = {
            "enabled": bool(connector["enabled"]),
            "authorized": connector["authorization_status"] == "approved",
            "endpoint": bool(str(connector["endpoint"] or "").strip()) if official else True,
            "api_key": bool(os.getenv(auth_env)) if auth_env else True,
        }
        messages = []
        if not checks["authorized"]:
            messages.append("来源尚未授权")
        if not checks["enabled"]:
            messages.append("来源未启用")
        if not checks["endpoint"]:
            messages.append("缺少官方 API 地址")
        if auth_env and not checks["api_key"]:
            messages.append(f"缺少环境变量 {auth_env}")
        ready = all(checks.values())
        result = {"id": connector_id, "ready": ready, "checks": checks,
                  "auth_env": auth_env, "message": "配置完整" if ready else "；".join(messages)}
        self._audit("connector.check", "connector", connector_id, actor,
                    {"ready": ready, "checks": checks})
        return result

    def _set_connector_sync_state(self, connector_id: str, status: str, result: dict) -> None:
        with self._connect() as db:
            db.execute("UPDATE connectors SET last_sync_status=?, last_sync_result=?, last_sync_at=CURRENT_TIMESTAMP WHERE id=?",
                       (status, json.dumps(result, ensure_ascii=False), connector_id))

    def sync_connector(self, connector_id: str, actor: str = "admin") -> dict:
        with self._connect() as db:
            connector = db.execute("SELECT * FROM connectors WHERE id=?", (connector_id,)).fetchone()
        if not connector:
            raise ValidationError("来源连接器不存在")
        if connector["authorization_status"] != "approved":
            raise ValidationError("来源连接器尚未授权")
        if connector["mode"] != "official_api":
            raise ValidationError("只有官方 API 连接器支持同步")
        if not connector["enabled"]:
            raise ValidationError("官方 API 连接器尚未启用")
        endpoint = str(connector["endpoint"] or "").strip()
        if not endpoint:
            raise ValidationError("官方 API 连接器缺少接口地址")
        self._set_connector_sync_state(connector_id, "running", {})
        headers = {"Accept": "application/json"}
        auth_env = str(connector["auth_env"] or "").strip()
        if auth_env:
            secret = os.getenv(auth_env)
            if not secret:
                raise ValidationError(f"来源连接器缺少环境变量 {auth_env}")
            headers["Authorization"] = f"Bearer {secret}"
        request = urllib.request.Request(endpoint, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            detail = str(exc)[:300]
            self._set_connector_sync_state(connector_id, "failed", {"error": detail})
            self._audit("connector.sync_failed", "connector", connector_id, actor, {"error": detail})
            raise ValidationError(f"来源连接器抓取失败：{exc}") from exc
        items = (body.get("items") or body.get("data")) if isinstance(body, dict) else body
        if isinstance(items, dict):
            items = items.get("items") or items.get("data")
        if not isinstance(items, list) or not items:
            self._set_connector_sync_state(connector_id, "failed", {"error": "官方 API 返回中没有可导入的模板列表"})
            raise ValidationError("官方 API 返回中没有可导入的模板列表")
        normalized = []
        for item in items[:100]:
            if not isinstance(item, dict):
                continue
            module = str(item.get("module") or "").strip()
            title = str(item.get("title") or item.get("name") or "").strip()
            source_url = str(item.get("source_url") or item.get("url") or "").strip()
            if module in MODULES and title and source_url.startswith(("https://", "http://")):
                normalized.append({"module": module, "title": title, "source_url": source_url,
                                   "prompt_template": str(item.get("prompt_template") or item.get("prompt") or "").strip()[:2000]})
        if not normalized:
            self._set_connector_sync_state(connector_id, "failed", {"error": "官方 API 返回的模板缺少合法 module、title 或 source_url"})
            raise ValidationError("官方 API 返回的模板缺少合法 module、title 或 source_url")
        batch_id = f"sync-{uuid.uuid4().hex[:8]}"
        imported = self.ingest_candidates_batch({"items": normalized, "batch_id": batch_id, "connector_id": connector_id})
        result = {"connector_id": connector_id, "batch_id": batch_id, **imported}
        self._set_connector_sync_state(connector_id, "succeeded", {
            "batch_id": batch_id, "created": result["created"], "duplicates": result["duplicates"],
            "total": result["total"],
        })
        self._audit("connector.sync", "connector", connector_id, actor,
                    {"batch_id": batch_id, "created": result["created"], "duplicates": result["duplicates"]})
        return result

    def usage(self, filters: dict | None = None) -> dict:
        filters = filters or {}
        where, args = [], []
        period = filters.get("period")
        if isinstance(period, list):
            period = period[0] if period else ""
        if period == "today":
            where.append("date(created_at,'localtime')=date('now','localtime')")
        elif period == "7d":
            where.append("datetime(created_at)>=datetime('now','-7 days','localtime')")
        elif period not in {None, "", "all"}:
            raise ValidationError("用量周期只能是 all、today 或 7d")
        for field in ("module", "provider", "model_id", "template_id", "user_id", "status"):
            value = filters.get(field)
            if isinstance(value, list):
                value = value[0] if value else ""
            if value:
                where.append(f"{field}=?")
                args.append(value)
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self._connect() as db:
            row = db.execute(f"SELECT COUNT(*) calls, SUM(status='succeeded') successes, SUM(tokens) tokens, SUM(credits) credits, SUM(cost) cost FROM usage{clause}", args).fetchone()
        return {"calls": row["calls"] or 0, "successes": row["successes"] or 0, "tokens": row["tokens"] or 0, "credits": row["credits"] or 0, "cost": row["cost"] or 0}

    def models(self, module: str | None = None) -> list[dict]:
        if module is not None and module not in MODULES:
            raise ValidationError("不支持的图片模块")
        with self._connect() as db:
            rows = [dict(r) for r in db.execute("SELECT id,provider,name,role,enabled,endpoint,auth_env,cost,daily_limit,adapter,remote_model_id,module_scope,priority,fallback_model_id,timeout_seconds,retry_count FROM models ORDER BY priority DESC, id").fetchall()]
        if module is not None:
            rows = [row for row in rows if row["role"] == "image" and (not row["module_scope"] or module in {item for item in row["module_scope"].split(",") if item})]
        for row in rows:
            if row["adapter"] == "mock":
                runtime_mode, runtime_label = "mock", MOCK_LABEL
                endpoint_configured = False
            elif row["adapter"] == "qwen_http":
                endpoint_configured = bool(os.getenv("QWEN_ENDPOINT") or row["endpoint"])
                has_credentials = endpoint_configured and bool(os.getenv("QWEN_API_KEY"))
                runtime_mode = "real" if has_credentials else "real-pending"
                runtime_label = "真实适配器 · 已配置" if has_credentials else "真实适配器 · 待配置"
            elif row["adapter"] == "doubao_http":
                endpoint_configured = bool(os.getenv("ARK_ENDPOINT") or row["endpoint"] or
                                           "https://ark.cn-beijing.volces.com/api/v3/images/generations")
                auth_env = row["auth_env"] or "ARK_API_KEY"
                has_credentials = endpoint_configured and bool(os.getenv(auth_env))
                runtime_mode = "real" if has_credentials else "real-pending"
                runtime_label = "真实适配器 · 已配置" if has_credentials else "真实适配器 · 待配置"
            elif row["adapter"] in {"sensenova_http", "sensenova_chat"}:
                endpoint_configured = bool(row["endpoint"])
                has_credentials = endpoint_configured and bool(os.getenv(row["auth_env"]) if row["auth_env"] else False)
                runtime_mode = "real" if has_credentials else "real-pending"
                runtime_label = "真实适配器 · 已配置" if has_credentials else "真实适配器 · 待配置"
            else:
                endpoint_configured = bool(row["endpoint"])
                runtime_mode, runtime_label = "unknown", f"未识别适配器 · {row['adapter']}"
            row.update({"provider_name": PROVIDER_NAMES.get(row["provider"], row["provider"]),
                        "runtime_mode": runtime_mode, "runtime_label": runtime_label,
                        "actual_call": False, "endpoint_configured": endpoint_configured,
                        "remote_model_id": row["remote_model_id"], "deletable": row["id"] not in BUILTIN_MODEL_IDS})
        return rows

    def set_model(self, model_id: str, payload: dict) -> dict:
        allowed = {"enabled", "endpoint", "auth_env", "cost", "daily_limit", "adapter", "remote_model_id", "module_scope", "priority", "fallback_model_id", "timeout_seconds", "retry_count"}
        fields = {k: payload[k] for k in allowed if k in payload}
        if not fields:
            raise ValidationError("没有可更新的模型配置")
        if "endpoint" in fields:
            fields["endpoint"] = self._validate_endpoint(fields["endpoint"])
        if "auth_env" in fields:
            auth_env = str(fields["auth_env"] or "").strip()
            if auth_env and not re.fullmatch(r"[A-Z][A-Z0-9_]*", auth_env):
                raise ValidationError("密钥环境变量名必须是大写字母、数字或下划线")
            fields["auth_env"] = auth_env
        if "module_scope" in fields:
            fields["module_scope"] = self._normalize_module_scope(fields["module_scope"])
        if "priority" in fields:
            try:
                fields["priority"] = int(fields["priority"] or 0)
            except (TypeError, ValueError) as exc:
                raise ValidationError("模型优先级格式不正确") from exc
            if fields["priority"] < 0:
                raise ValidationError("模型优先级不能为负数")
        if "timeout_seconds" in fields:
            try:
                fields["timeout_seconds"] = int(fields["timeout_seconds"] or 120)
            except (TypeError, ValueError) as exc:
                raise ValidationError("超时时间格式不正确") from exc
            if not 5 <= fields["timeout_seconds"] <= 600:
                raise ValidationError("超时时间需要在 5 到 600 秒之间")
        if "retry_count" in fields:
            try:
                fields["retry_count"] = int(fields["retry_count"] or 0)
            except (TypeError, ValueError) as exc:
                raise ValidationError("重试次数格式不正确") from exc
            if not 0 <= fields["retry_count"] <= 3:
                raise ValidationError("重试次数需要在 0 到 3 次之间")
        if "fallback_model_id" in fields:
            fields["fallback_model_id"] = str(fields["fallback_model_id"] or "").strip().lower()
        assignments = ",".join(f"{k}=?" for k in fields)
        with self._connect() as db:
            model = db.execute("SELECT provider FROM models WHERE id=?", (model_id,)).fetchone()
            if not model:
                raise ValidationError("模型不存在")
            if fields.get("adapter") == "qwen_http" and model["provider"] != "qwen":
                raise ValidationError("千问 HTTP 适配器只能绑定千问模型")
            if fields.get("adapter") == "doubao_http" and model["provider"] != "doubao":
                raise ValidationError("豆包 HTTP 适配器只能绑定豆包模型")
            if fields.get("adapter") in {"sensenova_http", "sensenova_chat"} and model["provider"] != "sensenova":
                raise ValidationError("商汤适配器只能绑定商汤模型")
            if fields.get("fallback_model_id"):
                if fields["fallback_model_id"] == model_id:
                    raise ValidationError("备用模型不能配置为自身")
                fallback = db.execute("SELECT role FROM models WHERE id=?", (fields["fallback_model_id"],)).fetchone()
                if not fallback or fallback["role"] != "image":
                    raise ValidationError("备用模型必须是已存在的生图模型")
            db.execute(f"UPDATE models SET {assignments} WHERE id=?", [*fields.values(), model_id])
            if db.total_changes == 0:
                raise ValidationError("模型不存在")
            row = db.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone()
        result = dict(row)
        self._audit("model.update", "model", model_id, str(payload.get("actor") or "admin"),
                    {key: value for key, value in fields.items() if key != "endpoint"})
        return result

    def create_model(self, payload: dict) -> dict:
        model_id = str(payload.get("id") or "").strip().lower()
        provider = str(payload.get("provider") or "").strip().lower()
        name = str(payload.get("name") or "").strip()[:120]
        role = str(payload.get("role") or "image").strip()
        adapter = str(payload.get("adapter") or "mock").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", model_id):
            raise ValidationError("模型 ID 只能包含小写字母、数字、下划线和短横线")
        if not provider or not name:
            raise ValidationError("供应商和模型名称不能为空")
        if role not in {"image", "understanding"}:
            raise ValidationError("模型角色只能是 image 或 understanding")
        if adapter not in {"mock", "qwen_http", "doubao_http", "sensenova_http", "sensenova_chat"}:
            raise ValidationError("暂不支持该模型适配器")
        if adapter == "qwen_http" and provider != "qwen":
            raise ValidationError("千问 HTTP 适配器只能绑定千问供应商")
        if adapter == "doubao_http" and provider != "doubao":
            raise ValidationError("豆包 HTTP 适配器只能绑定豆包供应商")
        if adapter in {"sensenova_http", "sensenova_chat"} and provider != "sensenova":
            raise ValidationError("商汤适配器只能绑定商汤供应商")
        try:
            cost = float(payload.get("cost", 0) or 0)
            daily_limit = int(payload.get("daily_limit", 0) or 0)
            priority = int(payload.get("priority", 0) or 0)
            timeout_seconds = int(payload.get("timeout_seconds", 120) or 120)
            retry_count = int(payload.get("retry_count", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise ValidationError("成本、每日限额、优先级、超时和重试格式不正确") from exc
        if cost < 0 or daily_limit < 0 or priority < 0:
            raise ValidationError("成本、每日限额和优先级不能为负数")
        if not 5 <= timeout_seconds <= 600:
            raise ValidationError("超时时间需要在 5 到 600 秒之间")
        if not 0 <= retry_count <= 3:
            raise ValidationError("重试次数需要在 0 到 3 次之间")
        fallback_model_id = str(payload.get("fallback_model_id") or "").strip().lower()
        if fallback_model_id == model_id:
            raise ValidationError("备用模型不能配置为自身")
        module_scope = self._normalize_module_scope(payload.get("module_scope"))
        auth_env = str(payload.get("auth_env") or "").strip()
        if auth_env and not re.fullmatch(r"[A-Z][A-Z0-9_]*", auth_env):
            raise ValidationError("密钥环境变量名必须是大写字母、数字或下划线")
        result = {
            "id": model_id, "provider": provider, "name": name, "role": role,
            "enabled": int(bool(payload.get("enabled", 0))),
            "endpoint": self._validate_endpoint(payload.get("endpoint")), "auth_env": auth_env, "cost": cost,
            "daily_limit": daily_limit, "adapter": adapter,
            "remote_model_id": str(payload.get("remote_model_id") or "").strip(),
            "module_scope": module_scope, "priority": priority,
            "fallback_model_id": fallback_model_id, "timeout_seconds": timeout_seconds, "retry_count": retry_count,
        }
        with self._connect() as db:
            try:
                if fallback_model_id:
                    fallback = db.execute("SELECT role FROM models WHERE id=?", (fallback_model_id,)).fetchone()
                    if not fallback or fallback["role"] != "image":
                        raise ValidationError("备用模型必须是已存在的生图模型")
                db.execute("""INSERT INTO models(id,provider,name,role,enabled,endpoint,auth_env,cost,daily_limit,adapter,remote_model_id,module_scope,priority,fallback_model_id,timeout_seconds,retry_count)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           tuple(result[key] for key in ("id", "provider", "name", "role", "enabled", "endpoint", "auth_env", "cost", "daily_limit", "adapter", "remote_model_id", "module_scope", "priority", "fallback_model_id", "timeout_seconds", "retry_count")))
            except sqlite3.IntegrityError as exc:
                raise ValidationError("模型 ID 已存在") from exc
        self._audit("model.create", "model", model_id, str(payload.get("actor") or "admin"),
                    {"provider": provider, "role": role, "adapter": adapter})
        return result

    def delete_model(self, model_id: str, actor: str = "admin") -> dict:
        if model_id in BUILTIN_MODEL_IDS:
            raise ValidationError("内置模型不能删除，请改为停用")
        with self._connect() as db:
            model = db.execute("SELECT id,enabled,name FROM models WHERE id=?", (model_id,)).fetchone()
            if not model:
                raise ValidationError("模型不存在")
            if model["enabled"]:
                raise ValidationError("请先停用模型，再执行删除")
            used = db.execute("SELECT COUNT(*) FROM usage WHERE model_id=?", (model_id,)).fetchone()[0]
            if used:
                raise ValidationError("模型已有调用记录，只能停用，不能删除")
            db.execute("DELETE FROM models WHERE id=?", (model_id,))
        self._audit("model.delete", "model", model_id, actor, {"name": model["name"]})
        return {"id": model_id, "status": "deleted"}

    def check_model(self, model_id: str) -> dict:
        with self._connect() as db:
            model = db.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone()
        if not model:
            raise ValidationError("模型不存在")
        checks = {"enabled": bool(model["enabled"]), "adapter": bool(model["adapter"]), "remote_model_id": True, "endpoint": True, "api_key": True}
        messages = []
        if not checks["enabled"]:
            messages.append("模型未启用")
        if not model["remote_model_id"]:
            checks["remote_model_id"] = False
            messages.append("缺少远端模型 ID")
        if model["adapter"] == "qwen_http":
            checks["endpoint"] = bool(os.getenv("QWEN_ENDPOINT") or model["endpoint"])
            checks["api_key"] = bool(os.getenv("QWEN_API_KEY"))
            if not checks["endpoint"]:
                messages.append("缺少 QWEN_ENDPOINT 或后台接口地址")
            if not checks["api_key"]:
                messages.append("缺少 QWEN_API_KEY")
        elif model["adapter"] == "doubao_http":
            checks["endpoint"] = bool(os.getenv("ARK_ENDPOINT") or model["endpoint"] or
                                       "https://ark.cn-beijing.volces.com/api/v3/images/generations")
            auth_env = model["auth_env"] or "ARK_API_KEY"
            checks["api_key"] = bool(os.getenv(auth_env))
            if not checks["endpoint"]:
                messages.append("缺少 ARK_ENDPOINT 或后台接口地址")
            if not checks["api_key"]:
                messages.append(f"缺少 {auth_env}")
        elif model["adapter"] in {"sensenova_http", "sensenova_chat"}:
            checks["endpoint"] = bool(model["endpoint"])
            checks["api_key"] = bool(model["auth_env"] and os.getenv(model["auth_env"]))
            if not checks["endpoint"]:
                messages.append("缺少商汤接口地址")
            if not checks["api_key"]:
                messages.append("缺少商汤密钥环境变量")
        elif model["adapter"] != "mock":
            checks["adapter"] = False
            messages.append("适配器未实现")
        ready = all(checks.values())
        return {"id": model_id, "ready": ready, "checks": checks, "message": "配置完整" if ready else "；".join(messages)}


def _xml(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


HTML = """<!doctype html><meta charset='utf-8'><title>AIGC 图片平台 V1</title>
<style>body{font:16px system-ui;max-width:960px;margin:40px auto;padding:0 20px;color:#202124}h1{margin-bottom:8px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.card{padding:18px;border:1px solid #ddd;border-radius:12px}.muted{color:#666}input,select,button{padding:10px;margin:4px 0;width:100%;box-sizing:border-box}button{cursor:pointer;background:#33246f;color:white;border:0;border-radius:8px}.result{white-space:pre-wrap;background:#f6f6f6;padding:12px;border-radius:8px}</style>
<h1>AIGC 图片平台 V1</h1><p class='muted'>表情包、手机壁纸、电脑壁纸 · 模板驱动 · 开发测试模型 · Mock</p>
<div class='grid'><div class='card'><h3>表情包</h3><button onclick="choose('emoji','emoji-default')">使用模板</button></div>
<div class='card'><h3>手机壁纸</h3><button onclick="choose('mobile_wallpaper','mobile-default')">使用模板</button></div>
<div class='card'><h3>电脑壁纸</h3><button onclick="choose('desktop_wallpaper','desktop-default')">使用模板</button></div></div>
<h2>生成图片</h2><input id='module' value='emoji' readonly><input id='template_id' value='emoji-default' readonly>
<input id='prompt' placeholder='输入主题，例如：开心的小猫'><select id='model_id'><option value='doubao-seedream'>豆包 · Doubao-Seedream（开发测试 Mock）</option><option value='qwen-image'>千问 · Qwen-Image（开发测试 Mock）</option></select>
<button onclick='generate()'>生成</button><div id='result' class='result'>等待生成…</div>
<script>function choose(m,t){module.value=m;template_id.value=t}async function generate(){let r=await fetch('/api/generate',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({module:module.value,template_id:template_id.value,prompt:prompt.value,model_id:model_id.value,user_id:'demo'})});result.textContent=JSON.stringify(await r.json(),null,2)}</script>"""

FRONTEND_DIR = Path(__file__).with_name("frontend")


class Handler(BaseHTTPRequestHandler):
    service = None

    def _send(self, status, data, content_type="application/json"):
        body = data if isinstance(data, bytes) else (json.dumps(data, ensure_ascii=False).encode() if content_type == "application/json" else data.encode())
        self.send_response(status); self.send_header("Content-Type", content_type + "; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def _json(self):
        length = int(self.headers.get("Content-Length", "0")); return json.loads(self.rfile.read(length) or b"{}")

    def _require_admin(self, payload: dict | None = None):
        payload = payload or {}
        role = self.headers.get("X-Admin-Role", "") or payload.get("actor_role", "")
        if role != "admin":
            raise AuthorizationError("需要管理员权限")

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                index = FRONTEND_DIR / "index.html"
                if index.is_file():
                    return self._send(200, index.read_text(encoding="utf-8"), "text/html")
                return self._send(200, HTML, "text/html")
            if parsed.path == "/admin":
                admin = FRONTEND_DIR / "admin.html"
                if admin.is_file():
                    return self._send(200, admin.read_text(encoding="utf-8"), "text/html")
                return self._send(404, {"error": "Admin UI not found"})
            if parsed.path.startswith("/outputs/"):
                filename = Path(parsed.path.removeprefix("/outputs/")).name
                output = self.service.output_dir / filename
                if output.is_file() and output.suffix in {".svg", ".png"}:
                    return self._send(200, output.read_bytes(), "image/svg+xml" if output.suffix == ".svg" else "image/png")
                return self._send(404, {"error": "Output not found"})
            if parsed.path in {"/styles.css", "/app.js", "/admin.js"}:
                asset = FRONTEND_DIR / parsed.path.lstrip("/")
                if asset.is_file():
                    content_type = "text/css" if asset.suffix == ".css" else "text/javascript"
                    return self._send(200, asset.read_text(encoding="utf-8"), content_type)
                return self._send(404, {"error": "Asset not found"})
            if parsed.path == "/api/health": return self._send(200, {"ok": True})
            if parsed.path == "/api/templates": return self._send(200, self.service.list_templates(parse_qs(parsed.query).get("module", [None])[0]))
            if parsed.path == "/api/quota":
                return self._send(200, self.service.daily_quota(parse_qs(parsed.query).get("user_id", ["demo"])[0]))
            if parsed.path == "/api/generations":
                query = parse_qs(parsed.query)
                return self._send(200, self.service.list_generations(query.get("user_id", ["demo"])[0], query.get("module", [None])[0]))
            if parsed.path == "/api/admin/templates":
                self._require_admin()
                return self._send(200, self.service.list_admin_templates(parse_qs(parsed.query).get("module", [None])[0]))
            match = re.fullmatch(r"/api/admin/templates/([^/]+)/versions", parsed.path)
            if match:
                self._require_admin()
                return self._send(200, self.service.list_template_versions(match.group(1)))
            if parsed.path == "/api/models": return self._send(200, self.service.models(parse_qs(parsed.query).get("module", [None])[0]))
            if parsed.path == "/api/admin/templates/candidates":
                self._require_admin()
                query = parse_qs(parsed.query)
                return self._send(200, self.service.candidates(query.get("status", [None])[0], query.get("module", [None])[0], query.get("batch_id", [None])[0]))
            if parsed.path == "/api/admin/templates/review-summary":
                self._require_admin()
                return self._send(200, self.service.review_summary())
            if parsed.path == "/api/admin/review-runs":
                self._require_admin()
                query = parse_qs(parsed.query)
                return self._send(200, self.service.list_review_runs(query.get("module", [None])[0], query.get("limit", [30])[0]))
            if parsed.path == "/api/admin/connectors":
                self._require_admin()
                return self._send(200, self.service.list_connectors())
            if parsed.path == "/api/admin/usage":
                self._require_admin()
                return self._send(200, self.service.usage(parse_qs(parsed.query)))
            if parsed.path == "/api/admin/audit-logs":
                self._require_admin()
                return self._send(200, self.service.audit_logs(parse_qs(parsed.query).get("limit", [100])[0]))
            return self._send(404, {"error": "Not found"})
        except AuthorizationError as exc: return self._send(403, {"error": str(exc)})
        except ValidationError as exc: return self._send(400, {"error": str(exc)})

    def do_POST(self):
        try:
            parsed = urlparse(self.path); payload = self._json()
            if parsed.path == "/api/generate": return self._send(200, self.service.generate(payload))
            if parsed.path == "/api/admin/templates/candidates":
                self._require_admin(payload); return self._send(201, self.service.ingest_candidate(payload))
            if parsed.path == "/api/admin/templates/candidates/batch":
                self._require_admin(payload); return self._send(201, self.service.ingest_candidates_batch(payload))
            if parsed.path == "/api/admin/review-runs/run":
                self._require_admin(payload); return self._send(201, self.service.run_review_snapshot(payload))
            if parsed.path == "/api/admin/templates/candidates/batch-review":
                self._require_admin(payload); return self._send(200, self.service.review_candidates_batch(payload))
            match = re.fullmatch(r"/api/admin/templates/candidates/([^/]+)/review", parsed.path)
            if match:
                self._require_admin(payload)
                return self._send(200, self.service.review_candidate(match.group(1), payload.get("decision", ""), payload.get("reviewer", "admin"), payload.get("reason", "")))
            match = re.fullmatch(r"/api/admin/templates/candidates/([^/]+)/ai-review", parsed.path)
            if match:
                self._require_admin(payload)
                return self._send(200, self.service.ai_review_candidate(match.group(1), payload.get("actor", "admin")))
            match = re.fullmatch(r"/api/admin/templates/candidates/([^/]+)/publish", parsed.path)
            if match:
                self._require_admin(payload)
                return self._send(200, self.service.publish_candidate(match.group(1), payload.get("connector_id", ""), payload.get("actor", "admin")))
            match = re.fullmatch(r"/api/admin/templates/([^/]+)", parsed.path)
            if match:
                self._require_admin(payload); return self._send(200, self.service.update_template(match.group(1), payload))
            match = re.fullmatch(r"/api/admin/templates/([^/]+)/rollback", parsed.path)
            if match:
                self._require_admin(payload); return self._send(200, self.service.rollback_template(match.group(1), payload.get("version"), payload.get("actor", "admin")))
            if parsed.path == "/api/admin/models":
                self._require_admin(payload); return self._send(201, self.service.create_model(payload))
            match = re.fullmatch(r"/api/admin/connectors/([^/]+)/check", parsed.path)
            if match:
                self._require_admin(payload); return self._send(200, self.service.check_connector(match.group(1), payload.get("actor", "admin")))
            match = re.fullmatch(r"/api/admin/connectors/([^/]+)/sync", parsed.path)
            if match:
                self._require_admin(payload); return self._send(201, self.service.sync_connector(match.group(1), payload.get("actor", "admin")))
            match = re.fullmatch(r"/api/admin/connectors/([^/]+)", parsed.path)
            if match:
                self._require_admin(payload); return self._send(200, self.service.set_connector(match.group(1), payload))
            match = re.fullmatch(r"/api/admin/models/([^/]+)", parsed.path)
            if match:
                self._require_admin(payload); return self._send(200, self.service.set_model(match.group(1), payload))
            match = re.fullmatch(r"/api/admin/models/([^/]+)/check", parsed.path)
            if match:
                self._require_admin(payload); return self._send(200, self.service.check_model(match.group(1)))
            return self._send(404, {"error": "Not found"})
        except AuthorizationError as exc: return self._send(403, {"error": str(exc)})
        except (ValidationError, json.JSONDecodeError) as exc: return self._send(400, {"error": str(exc)})

    def do_DELETE(self):
        try:
            parsed = urlparse(self.path)
            match = re.fullmatch(r"/api/generations/([^/]+)", parsed.path)
            if match:
                user_id = parse_qs(parsed.query).get("user_id", ["demo"])[0]
                return self._send(200, self.service.delete_generation(match.group(1), user_id))
            match = re.fullmatch(r"/api/admin/models/([^/]+)", parsed.path)
            if not match:
                return self._send(404, {"error": "Not found"})
            self._require_admin()
            return self._send(200, self.service.delete_model(match.group(1)))
        except AuthorizationError as exc: return self._send(403, {"error": str(exc)})
        except ValidationError as exc: return self._send(400, {"error": str(exc)})


def run(host="127.0.0.1", port=8000, data_dir="data"):
    data = Path(data_dir); Handler.service = AIGCService(data / "app.db", data / "outputs")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"AIGC V1 running at http://{host}:{port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__":
    run(port=int(os.getenv("PORT", "8000")))
