import json
import io
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import app as app_module  # noqa: E402
from app import AIGCService, AuthorizationError, ValidationError  # noqa: E402


@pytest.fixture()
def service(tmp_path):
    return AIGCService(tmp_path / "app.db", tmp_path / "outputs")


def test_load_env_file_reads_dotenv_without_overriding_existing_values(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# local secrets\n"
        "SENSENOVA_API_KEY=\"from-dotenv\"\n"
        "NEW_SETTING='quoted value'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SENSENOVA_API_KEY", "from-shell")
    monkeypatch.delenv("NEW_SETTING", raising=False)

    app_module._load_env_file([env_file])

    assert os.environ["SENSENOVA_API_KEY"] == "from-shell"
    assert os.environ["NEW_SETTING"] == "quoted value"


def test_generate_uses_module_preset_and_records_usage(service):
    result = service.generate({
        "module": "mobile_wallpaper",
        "template_id": "mobile-default",
        "prompt": "夜晚城市和霓虹灯",
        "model_id": "doubao-seedream",
        "user_id": "u1",
    })
    assert result["size"] == {"width": 1290, "height": 2796}
    assert result["status"] == "succeeded"
    assert result["output_url"].startswith("/outputs/")
    assert result["runtime"]["mode"] == "mock"
    assert result["runtime"]["label"] == "开发测试模型 · Mock"
    assert result["runtime"]["provider"] == "doubao"
    assert result["runtime"]["model_name"] == "Doubao-Seedream"
    assert result["runtime"]["actual_call"] is False
    assert Path(result["output_path"]).is_file()
    usage = service.usage({"module": "mobile_wallpaper"})
    assert usage["calls"] == 1 and usage["successes"] == 1
    assert service.usage({"provider": "doubao", "model_id": "doubao-seedream", "template_id": "mobile-default", "user_id": "u1"})["calls"] == 1
    history = service.list_generations("u1")
    assert len(history) == 1
    assert history[0]["output_url"] == result["output_url"]
    assert history[0]["runtime_mode"] == "mock"
    assert history[0]["prompt"].startswith("适合手机锁屏")


def test_models_expose_provider_and_runtime_mode(service):
    models = {model["id"]: model for model in service.models()}
    assert models["qwen-image"]["provider_name"] == "千问"
    assert models["qwen-image"]["runtime_mode"] == "mock"
    assert models["qwen-image"]["runtime_label"] == "开发测试模型 · Mock"


def test_sensenova_u15_lite_is_enabled_image_model(service):
    models = {model["id"]: model for model in service.models()}

    assert models["sensenova-u1.5-lite"]["provider_name"] == "商汤"
    assert models["sensenova-u1.5-lite"]["role"] == "image"
    assert models["sensenova-u1.5-lite"]["enabled"] == 1
    assert models["sensenova-u1.5-lite"]["remote_model_id"] == "sensenova-u1.5-lite"


def test_sensenova_chat_model_reports_real_runtime_when_configured(service, monkeypatch):
    service.set_model("sensenova-6.8-flash-lite", {
        "enabled": 1,
        "adapter": "sensenova_chat",
        "endpoint": "https://token.sensenova.cn/v1/chat/completions",
        "auth_env": "SENSENOVA_API_KEY",
        "remote_model_id": "sensenova-6.8-flash-lite",
    })
    monkeypatch.setenv("SENSENOVA_API_KEY", "test-key")
    model = next(item for item in service.models() if item["id"] == "sensenova-6.8-flash-lite")
    assert model["runtime_mode"] == "real"
    assert model["runtime_label"] == "真实适配器 · 已配置"


def test_sensenova_image_model_can_save_http_endpoint_and_auth_env(service):
    updated = service.set_model("sensenova-u1.5-lite", {
        "adapter": "sensenova_http",
        "endpoint": "https://token.sensetime.com/api/v1/image/sync",
        "auth_env": "SENSENOVA_API_KEY",
    })

    assert updated["adapter"] == "sensenova_http"
    assert updated["endpoint"].endswith("/image/sync")
    assert updated["auth_env"] == "SENSENOVA_API_KEY"


def test_sensenova_http_adapter_downloads_image_and_marks_real_call(service, monkeypatch):
    service.set_model("sensenova-u1.5-lite", {
        "adapter": "sensenova_http",
        "endpoint": "https://token.sensetime.com/api/v1/image/sync",
        "auth_env": "SENSENOVA_API_KEY",
    })
    monkeypatch.setenv("SENSENOVA_API_KEY", "test-sensenova-key")
    calls = []

    class FakeResponse:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return self.body

    def fake_urlopen(request, timeout=0):
        calls.append(request)
        if isinstance(request, str):
            return FakeResponse(b"PNG-DATA")
        assert request.headers["Authorization"] == "Bearer test-sensenova-key"
        return FakeResponse(json.dumps({"data": [{"url": "https://token.sensetime.com/image.png"}]}).encode())

    monkeypatch.setattr(app_module.urllib.request, "urlopen", fake_urlopen)
    result = service.generate({
        "module": "emoji", "template_id": "emoji-default", "prompt": "测试商汤",
        "model_id": "sensenova-u1.5-lite", "user_id": "u1",
    })

    assert result["runtime"]["mode"] == "real"
    assert result["runtime"]["actual_call"] is True
    assert result["output_url"].endswith(".png")
    assert len(calls) == 2
    request_body = json.loads(calls[0].data.decode())
    assert request_body["model"] == "sensenova-u1.5-lite"
    assert request_body["size"] == "1024x1024"


def test_sensenova_openai_image_protocol_uses_generations_endpoint(service, monkeypatch):
    service.set_model("sensenova-u1.5-lite", {
        "adapter": "sensenova_http",
        "endpoint": "https://token.sensenova.cn/v1/images/generations",
        "auth_env": "SENSENOVA_API_KEY",
    })
    monkeypatch.setenv("SENSENOVA_API_KEY", "test-sensenova-key")
    calls = []

    class FakeResponse:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return self.body

    def fake_urlopen(request, timeout=0):
        calls.append(request)
        if isinstance(request, str):
            return FakeResponse(b"PNG-DATA")
        return FakeResponse(json.dumps({"data": [{"url": "https://token.sensenova.cn/image.png"}]}).encode())

    monkeypatch.setattr(app_module.urllib.request, "urlopen", fake_urlopen)
    result = service.generate({
        "module": "emoji", "template_id": "emoji-default", "prompt": "测试商汤",
        "model_id": "sensenova-u1.5-lite", "user_id": "u1",
    })

    assert result["runtime"]["mode"] == "real"
    request = calls[0]
    assert request.full_url == "https://token.sensenova.cn/v1/images/generations"
    request_body = json.loads(request.data.decode())
    assert request_body["model"] == "sensenova-u1.5-lite"
    assert request_body["size"] == "1024x1024"
    assert request_body["n"] == 1
    assert request_body["watermark"] is False
    assert request_body["prompt_extend"] is True
    assert request_body["response_format"] == "url"


def test_sensenova_openai_image_protocol_uses_edits_for_reference_image(service, monkeypatch):
    service.set_model("sensenova-u1.5-lite", {
        "adapter": "sensenova_http",
        "endpoint": "https://token.sensenova.cn/v1/images/generations",
        "auth_env": "SENSENOVA_API_KEY",
    })
    monkeypatch.setenv("SENSENOVA_API_KEY", "test-sensenova-key")
    calls = []

    class FakeResponse:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return self.body

    def fake_urlopen(request, timeout=0):
        calls.append(request)
        if isinstance(request, str):
            return FakeResponse(b"PNG-DATA")
        return FakeResponse(json.dumps({"data": [{"url": "https://token.sensenova.cn/image.png"}]}).encode())

    monkeypatch.setattr(app_module.urllib.request, "urlopen", fake_urlopen)
    result = service.generate({
        "module": "emoji", "template_id": "emoji-default", "prompt": "编辑图片",
        "reference_image": "data:image/png;base64,QUJD", "model_id": "sensenova-u1.5-lite", "user_id": "u1",
    })

    assert result["runtime"]["mode"] == "real"
    request = calls[0]
    assert request.full_url == "https://token.sensenova.cn/v1/images/edits"
    request_body = json.loads(request.data.decode())
    assert request_body["images"] == [{"image_url": "data:image/png;base64,QUJD"}]
    assert "image" not in request_body


def test_sensenova_http_adapter_surfaces_provider_auth_error(service, monkeypatch):
    service.set_model("sensenova-u1.5-lite", {
        "adapter": "sensenova_http",
        "endpoint": "https://token.sensetime.com/api/v1/image/sync",
        "auth_env": "SENSENOVA_API_KEY",
    })
    monkeypatch.setenv("SENSENOVA_API_KEY", "test-sensenova-key")
    provider_error = app_module.urllib.error.HTTPError(
        "https://token.sensetime.com/api/v1/image/sync",
        401,
        "Unauthorized",
        {},
        io.BytesIO(b'{"code":"authentication_error","message":"incorrect API key provided"}'),
    )
    monkeypatch.setattr(app_module.urllib.request, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(provider_error))

    with pytest.raises(ValidationError, match=r"HTTP 401.*incorrect API key provided"):
        service.generate({
            "module": "emoji", "template_id": "emoji-default", "prompt": "测试商汤",
            "model_id": "sensenova-u1.5-lite", "user_id": "u1",
        })


def test_model_endpoint_rejects_api_key_and_accepts_http_url(service):
    with pytest.raises(ValidationError, match=r"接口地址必须是 http\(s\) URL"):
        service.set_model("qwen-image", {"endpoint": "sk-test-key"})
    updated = service.set_model("qwen-image", {"endpoint": "https://workspace.example.com/images/generations"})
    assert updated["endpoint"] == "https://workspace.example.com/images/generations"


def test_real_qwen_adapter_requires_credentials_instead_of_silent_mock(service, monkeypatch):
    service.set_model("qwen-image", {"adapter": "qwen_http"})
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.delenv("QWEN_ENDPOINT", raising=False)
    with pytest.raises(ValidationError, match="千问真实适配器未配置"):
        service.generate({
            "module": "emoji",
            "template_id": "emoji-default",
            "prompt": "测试",
            "model_id": "qwen-image",
            "user_id": "u1",
        })


def test_real_qwen_adapter_downloads_image_and_records_real_receipt(service, monkeypatch):
    service.set_model("qwen-image", {"adapter": "qwen_http"})
    monkeypatch.setenv("QWEN_API_KEY", "test-key")
    monkeypatch.setenv("QWEN_ENDPOINT", "https://qwen.test/images/generations")
    calls = []

    class FakeResponse:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return self.body

    def fake_urlopen(request, timeout=0):
        calls.append(request)
        if isinstance(request, str):
            return FakeResponse(b"PNG-DATA")
        return FakeResponse(json.dumps({
            "data": [{"url": "https://qwen.test/image.png"}],
            "usage": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20, "cost": 0.18},
        }).encode())

    monkeypatch.setattr(app_module.urllib.request, "urlopen", fake_urlopen)
    result = service.generate({
        "module": "emoji", "template_id": "emoji-default", "prompt": "测试", "model_id": "qwen-image", "user_id": "u1",
        "reference_image": "data:image/png;base64,QUJD",
        "negative_prompt": "模糊、水印",
    })
    assert result["runtime"]["mode"] == "real"
    assert result["runtime"]["actual_call"] is True
    assert result["runtime"]["usage"]["tokens"] == 20
    assert result["runtime"]["usage"]["cost"] == 0.18
    assert result["output_url"].endswith(".png")
    assert Path(result["output_path"]).read_bytes() == b"PNG-DATA"
    assert len(calls) == 2
    request_body = json.loads(calls[0].data.decode())
    assert request_body["model"] == "qwen-image-3.0-pro"
    assert request_body["size"] == "1024x1024"
    assert request_body["image"] == "data:image/png;base64,QUJD"
    assert request_body["negative_prompt"] == "模糊、水印"
    usage = service.usage({"module": "emoji"})
    assert usage["credits"] == 1 and usage["cost"] == 0.18 and usage["tokens"] == 20


def test_real_doubao_adapter_requires_credentials_instead_of_silent_mock(service, monkeypatch):
    service.set_model("doubao-seedream", {"adapter": "doubao_http"})
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.delenv("ARK_ENDPOINT", raising=False)

    model = next(item for item in service.models() if item["id"] == "doubao-seedream")
    assert model["runtime_mode"] == "real-pending"
    assert model["runtime_label"] == "真实适配器 · 待配置"
    check = service.check_model("doubao-seedream")
    assert check["ready"] is False
    assert "ARK_API_KEY" in check["message"]
    with pytest.raises(ValidationError, match="豆包真实适配器未配置"):
        service.generate({
            "module": "emoji",
            "template_id": "emoji-default",
            "prompt": "测试",
            "model_id": "doubao-seedream",
            "user_id": "u1",
        })


def test_real_doubao_adapter_downloads_image_and_records_real_receipt(service, monkeypatch):
    service.set_model("doubao-seedream", {
        "enabled": 1,
        "adapter": "doubao_http",
        "remote_model_id": "doubao-seedream-5-0-260128",
        "auth_env": "ARK_API_KEY",
        "endpoint": "https://ark.test/api/v3/images/generations",
    })
    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")
    calls = []

    class FakeResponse:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return self.body

    def fake_urlopen(request, timeout=0):
        calls.append(request)
        if isinstance(request, str):
            return FakeResponse(b"PNG-DATA")
        assert request.headers["Authorization"] == "Bearer test-ark-key"
        return FakeResponse(json.dumps({
            "data": [{"url": "https://ark.test/image.png"}],
            "usage": {"total_tokens": 17, "cost": 0.22},
        }).encode())

    monkeypatch.setattr(app_module.urllib.request, "urlopen", fake_urlopen)
    result = service.generate({
        "module": "mobile_wallpaper",
        "template_id": "mobile-default",
        "prompt": "测试豆包",
        "model_id": "doubao-seedream",
        "user_id": "u1",
        "reference_image": "data:image/png;base64,QUJD",
    })

    assert result["runtime"]["mode"] == "real"
    assert result["runtime"]["actual_call"] is True
    assert result["runtime"]["usage"]["tokens"] == 17
    assert result["runtime"]["usage"]["cost"] == 0.22
    assert Path(result["output_path"]).read_bytes() == b"PNG-DATA"
    assert len(calls) == 2
    request_body = json.loads(calls[0].data.decode())
    assert calls[0].full_url == "https://ark.test/api/v3/images/generations"
    assert request_body["model"] == "doubao-seedream-5-0-260128"
    assert request_body["size"] == "1290x2796"
    assert request_body["response_format"] == "url"
    assert request_body["image"] == "data:image/png;base64,QUJD"
    usage = service.usage({"module": "mobile_wallpaper"})
    assert usage["credits"] == 1 and usage["cost"] == 0.22 and usage["tokens"] == 17


def test_generate_rejects_unsafe_custom_size(service):
    with pytest.raises(ValidationError, match="安全范围"):
        service.generate({
            "module": "emoji",
            "template_id": "emoji-default",
            "prompt": "开心",
            "width": 100,
            "height": 100,
            "model_id": "qwen-image",
            "user_id": "u1",
        })


def test_reference_image_accepts_data_url_and_rejects_unknown_format(service):
    result = service.generate({
        "module": "emoji", "template_id": "emoji-default", "prompt": "测试", "model_id": "doubao-seedream",
        "reference_image": "data:image/png;base64,QUJD", "negative_prompt": "文字", "user_id": "u1",
    })
    assert result["runtime"]["reference_image_attached"] is True
    assert result["runtime"]["negative_prompt_attached"] is True
    with pytest.raises(ValidationError, match="Base64 或 http"):
        service.generate({
            "module": "emoji", "template_id": "emoji-default", "prompt": "测试", "model_id": "doubao-seedream",
            "reference_image": "file:///tmp/a.png", "user_id": "u2",
        })


def test_user_can_delete_only_their_own_generation(service):
    result = service.generate({
        "module": "emoji", "template_id": "emoji-default", "prompt": "待删除图片",
        "model_id": "doubao-seedream", "user_id": "u1",
    })
    with pytest.raises(AuthorizationError, match="无权删除"):
        service.delete_generation(result["id"], "u2")

    deleted = service.delete_generation(result["id"], "u1")
    assert deleted["status"] == "deleted"
    assert not Path(result["output_path"]).exists()
    assert service.list_generations("u1") == []


def test_expired_generations_are_cleaned_from_db_and_disk(service):
    result = service.generate({
        "module": "emoji", "template_id": "emoji-default", "prompt": "过期图片",
        "model_id": "doubao-seedream", "user_id": "u1",
    })
    with service._connect() as db:
        db.execute("UPDATE generations SET created_at=datetime('now','-8 days') WHERE id=?", (result["id"],))

    summary = service.cleanup_expired_generations(7)
    assert summary["deleted"] == 1
    assert not Path(result["output_path"]).exists()
    assert service.list_generations("u1") == []


def test_daily_free_quota_limits_successful_generations(service):
    assert service.daily_quota("u1") == {"limit": 3, "used": 0, "remaining": 3}
    for index in range(3):
        service.generate({
            "module": "emoji", "template_id": "emoji-default", "prompt": f"额度测试 {index}",
            "model_id": "doubao-seedream", "user_id": "u1",
        })
    assert service.daily_quota("u1") == {"limit": 3, "used": 3, "remaining": 0}
    with pytest.raises(ValidationError, match="今日免费额度已用完"):
        service.generate({
            "module": "emoji", "template_id": "emoji-default", "prompt": "超额测试",
            "model_id": "doubao-seedream", "user_id": "u1",
        })


def test_approved_candidate_records_internal_publish_status(service):
    candidate = service.ingest_candidate({
        "module": "emoji", "title": "内部发布测试", "source_url": "https://example.com/internal-publish",
        "prompt_template": "简洁可爱的猫咪", "copyright_status": "cleared",
    })
    result = service.review_candidate(candidate["id"], "approved", "admin")
    assert result["status"] == "published"
    row = next(item for item in service.candidates() if item["id"] == candidate["id"])
    assert row["publish_status"] == "internal_published"
    assert row["publish_result"]["mode"] == "internal"
    assert service.list_template_versions(f"candidate-{candidate['id']}")[0]["version"] == 1
    assert any(item["action"] == "candidate.ingest" for item in service.audit_logs())
    assert any(item["action"] == "candidate.approved" for item in service.audit_logs())


def test_publish_candidate_uses_only_authorized_official_connector(service, monkeypatch):
    candidate = service.ingest_candidate({
        "module": "emoji", "title": "官方推送测试", "source_url": "https://example.com/official-publish",
        "prompt_template": "简洁可爱的猫咪", "copyright_status": "cleared",
    })
    service.review_candidate(candidate["id"], "approved", "admin")
    connector = service.list_connectors()[0]
    service.set_connector(connector["id"], {
        "authorization_status": "approved", "enabled": 1, "mode": "official_api",
        "endpoint": "https://source.test/publish", "auth_env": "SOURCE_API_KEY",
    })
    monkeypatch.setenv("SOURCE_API_KEY", "test-source-key")
    calls = []

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self): return b'{"id":"remote-1","status":"published"}'

    def fake_urlopen(request, timeout=0):
        calls.append(request)
        return FakeResponse()

    monkeypatch.setattr(app_module.urllib.request, "urlopen", fake_urlopen)
    result = service.publish_candidate(candidate["id"], connector["id"], "admin")
    assert result["publish_status"] == "pushed"
    assert result["publish_result"]["external_id"] == "remote-1"
    assert calls[0].headers["Authorization"] == "Bearer test-source-key"
    request_body = json.loads(calls[0].data.decode())
    assert request_body["title"] == "官方推送测试"


def test_image_models_can_be_scoped_to_modules_and_sorted_by_priority(service):
    service.set_model("qwen-image", {"enabled": 1, "module_scope": "emoji", "priority": 20})
    emoji_models = service.models("emoji")
    wallpaper_models = service.models("mobile_wallpaper")
    assert emoji_models[0]["id"] == "qwen-image"
    assert any(item["id"] == "qwen-image" for item in emoji_models)
    assert not any(item["id"] == "qwen-image" for item in wallpaper_models)
    with pytest.raises(ValidationError, match="未绑定当前模块"):
        service.generate({
            "module": "mobile_wallpaper", "template_id": "mobile-default", "prompt": "越界测试",
            "model_id": "qwen-image", "user_id": "u1",
        })


def test_model_failure_retries_then_uses_configured_fallback_without_double_credit(service, monkeypatch):
    service.set_model("qwen-image", {
        "adapter": "qwen_http", "fallback_model_id": "doubao-seedream", "retry_count": 1,
        "timeout_seconds": 30,
    })
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.delenv("QWEN_ENDPOINT", raising=False)
    result = service.generate({
        "module": "emoji", "template_id": "emoji-default", "prompt": "容错测试",
        "model_id": "qwen-image", "user_id": "fallback-user",
    })
    assert result["model_id"] == "doubao-seedream"
    assert result["runtime"]["requested_model_id"] == "qwen-image"
    assert [item["status"] for item in result["runtime"]["attempts"]] == ["failed", "failed", "succeeded"]
    usage = service.usage({"user_id": "fallback-user"})
    assert usage["calls"] == 3 and usage["successes"] == 1 and usage["credits"] == 1


def test_model_retry_and_timeout_configuration_is_validated(service):
    with pytest.raises(ValidationError, match="5 到 600"):
        service.set_model("qwen-image", {"timeout_seconds": 4})
    with pytest.raises(ValidationError, match="0 到 3"):
        service.set_model("qwen-image", {"retry_count": 4})
    with pytest.raises(ValidationError, match="备用模型不能配置为自身"):
        service.set_model("qwen-image", {"fallback_model_id": "qwen-image"})


def test_connector_cannot_enable_without_authorization(service):
    connector = service.list_connectors()[0]
    with pytest.raises(ValidationError, match="未授权"):
        service.set_connector(connector["id"], {"enabled": 1})
    updated = service.set_connector(connector["id"], {"authorization_status": "approved", "enabled": 1, "mode": "official_api", "endpoint": "https://source.test/api"})
    assert updated["enabled"] == 1 and updated["authorization_status"] == "approved"
    assert any(item["action"] == "connector.update" for item in service.audit_logs())


def test_connector_configuration_check_never_returns_secret(service, monkeypatch):
    connector = service.list_connectors()[0]
    service.set_connector(connector["id"], {
        "authorization_status": "approved", "enabled": 1, "mode": "official_api",
        "endpoint": "https://source.example/api", "auth_env": "SOURCE_API_KEY",
    })
    monkeypatch.delenv("SOURCE_API_KEY", raising=False)
    missing = service.check_connector(connector["id"])
    assert missing["ready"] is False
    assert missing["checks"]["api_key"] is False
    assert "SOURCE_API_KEY" in missing["message"]
    assert "secret" not in json.dumps(missing).lower()
    monkeypatch.setenv("SOURCE_API_KEY", "source-secret")
    ready = service.check_connector(connector["id"])
    assert ready["ready"] is True
    assert ready["checks"]["api_key"] is True
    assert ready["auth_env"] == "SOURCE_API_KEY"
    assert service.list_connectors()[0]["auth_env_configured"] is True
    assert any(item["action"] == "connector.check" for item in service.audit_logs())


def test_model_configuration_check_is_non_billing(service, monkeypatch):
    service.set_model("qwen-image", {"enabled": 1, "adapter": "qwen_http", "remote_model_id": "qwen-image-3.0-pro"})
    monkeypatch.setenv("QWEN_ENDPOINT", "https://qwen.test/images/generations")
    monkeypatch.setenv("QWEN_API_KEY", "test-key")
    result = service.check_model("qwen-image")
    assert result["ready"] is True
    monkeypatch.delenv("QWEN_API_KEY")
    assert service.check_model("qwen-image")["ready"] is False


def test_batch_candidate_import_deduplicates_source_fingerprint(service):
    payload = {
        "batch_id": "batch-1",
        "items": [
            {"module": "emoji", "title": "猫猫", "source_url": "https://example.com/cat"},
            {"module": "emoji", "title": "猫猫重复", "source_url": "https://example.com/cat"},
        ],
    }
    result = service.ingest_candidates_batch(payload)
    assert result["created"] == 1 and result["duplicates"] == 1
    assert len(service.candidates()) == 1
    assert len(service.candidates(status="pending_review", module="emoji", batch_id="batch-1")) == 1
    assert service.candidates(module="mobile_wallpaper") == []


def test_candidate_requires_review_before_publish(service):
    candidate = service.ingest_candidate({
        "module": "desktop_wallpaper",
        "title": "赛博城市",
        "source_url": "https://example.com/template/1",
        "copyright_status": "pending",
    })
    assert candidate["status"] == "pending_review"
    assert all(item["title"] != "赛博城市" for item in service.list_templates("desktop_wallpaper"))
    reviewed = service.review_candidate(candidate["id"], "approved", "admin", "版权状态已确认")
    assert reviewed["reviewed_at"]
    assert any(item["title"] == "赛博城市" for item in service.list_templates("desktop_wallpaper"))
    candidate_row = next(item for item in service.candidates() if item["id"] == candidate["id"])
    assert candidate_row["review_reason"] == "版权状态已确认"
    assert service.review_summary()["totals"]["published"] == 1


def test_candidate_prompt_template_survives_approval(service):
    candidate = service.ingest_candidate({
        "module": "emoji",
        "title": "品牌猫猫",
        "source_url": "https://example.com/brand-cat",
        "prompt_template": "品牌主色、圆润线条、主体清晰，预留短文字空间",
    })
    service.review_candidate(candidate["id"], "approved", "admin", "物料已确认")

    published = next(item for item in service.list_templates("emoji") if item["title"] == "品牌猫猫")
    assert published["prompt_template"] == "品牌主色、圆润线条、主体清晰，预留短文字空间"


def test_candidate_cannot_be_reviewed_twice(service):
    candidate = service.ingest_candidate({
        "module": "emoji", "title": "重复审核", "source_url": "https://example.com/review-once",
    })
    service.review_candidate(candidate["id"], "approved", "admin")
    with pytest.raises(ValidationError, match="不能重复审核"):
        service.review_candidate(candidate["id"], "approved", "admin")


def test_batch_review_processes_selected_candidates_and_audits(service):
    first = service.ingest_candidate({
        "module": "emoji", "title": "批量猫", "source_url": "https://example.com/batch-cat",
    })
    second = service.ingest_candidate({
        "module": "emoji", "title": "批量狗", "source_url": "https://example.com/batch-dog",
    })
    result = service.review_candidates_batch({
        "candidate_ids": [first["id"], second["id"], first["id"]],
        "decision": "approved", "reviewer": "admin", "reason": "批量确认",
    })
    assert result["total"] == 2
    assert result["processed"] == 2 and result["failed"] == 0
    assert all(item["status"] == "published" for item in service.candidates())
    assert any(item["action"] == "candidate.batch_review" for item in service.audit_logs())


def test_candidate_precheck_marks_risk_flags_without_auto_rejecting(service):
    candidate = service.ingest_candidate({
        "module": "emoji",
        "title": "血腥猫猫",
        "source_url": "https://example.com/risky-cat",
        "prompt_template": "暴力场景，主体清晰",
    })

    row = next(item for item in service.candidates() if item["id"] == candidate["id"])
    assert {flag["code"] for flag in row["risk_flags"]} == {"violence"}
    assert row["status"] == "pending_review"


def test_frontend_explains_enabled_understanding_model_when_no_image_model_is_available():
    source = (Path(__file__).parent / "frontend" / "app.js").read_text(encoding="utf-8")
    assert "已启用的是理解/审核模型" in source
    assert "请启用一个生图模型" in source


def test_admin_template_update_increments_version_and_controls_publish_state(service):
    before = service.list_admin_templates("emoji")[0]
    updated = service.update_template(before["id"], {
        "title": "通用表情包·新版",
        "prompt_template": "圆润、清晰、适合聊天的表情包",
        "status": "draft",
    })
    assert updated["version"] == before["version"] + 1
    assert updated["status"] == "draft"
    assert service.list_templates("emoji") == []
    assert service.list_template_versions(before["id"])[0]["version"] == updated["version"]
    service.update_template(before["id"], {"status": "published"})
    assert service.list_templates("emoji")[0]["title"] == "通用表情包·新版"
    assert any(item["action"] == "template.update" for item in service.audit_logs())
    rolled_back = service.rollback_template(before["id"], 1)
    assert rolled_back["version"] == before["version"] + 3
    assert rolled_back["title"] == before["title"]
    assert any(item["action"] == "template.rollback" for item in service.audit_logs())


def test_admin_write_guard_requires_admin_role():
    handler = app_module.Handler.__new__(app_module.Handler)
    handler.headers = {}
    with pytest.raises(AuthorizationError):
        handler._require_admin()
    handler.headers = {"X-Admin-Role": "admin"}
    handler._require_admin()


def test_model_daily_limit_blocks_the_next_generation(service):
    service.set_model("doubao-seedream", {"daily_limit": 1})
    payload = {
        "module": "emoji", "template_id": "emoji-default", "prompt": "测试", "model_id": "doubao-seedream", "user_id": "u1",
    }
    service.generate(payload)
    with pytest.raises(ValidationError, match="今日额度已用完"):
        service.generate(payload)


def test_usage_period_filters_today_and_all_history(service):
    service.generate({
        "module": "emoji", "template_id": "emoji-default", "prompt": "昨天的测试",
        "model_id": "doubao-seedream", "user_id": "u1",
    })
    with service._connect() as db:
        db.execute("UPDATE usage SET created_at=datetime('now','-1 day')")

    assert service.usage({"period": "today"})["calls"] == 0
    assert service.usage({"period": "all"})["calls"] == 1


def test_review_snapshot_records_counts_and_can_be_filtered(service):
    first = service.ingest_candidate({
        "module": "emoji", "title": "猫猫", "source_url": "https://example.com/cat-1",
    })
    second = service.ingest_candidate({
        "module": "emoji", "title": "狗狗", "source_url": "https://example.com/dog-1",
    })
    service.ingest_candidate({
        "module": "mobile_wallpaper", "title": "城市", "source_url": "https://example.com/city-1",
    })
    service.review_candidate(first["id"], "approved", "admin", "版权已确认")
    service.review_candidate(second["id"], "rejected", "admin", "来源不明")

    snapshot = service.run_review_snapshot({"module": "emoji", "actor": "admin"})

    assert snapshot["module"] == "emoji"
    assert snapshot["totals"] == {"pending_review": 0, "published": 1, "rejected": 1}
    runs = service.list_review_runs(module="emoji")
    assert runs[0]["id"] == snapshot["id"]
    assert runs[0]["actor"] == "admin"


def test_official_connector_sync_imports_json_templates_and_deduplicates(service, monkeypatch):
    connector = service.list_connectors()[0]
    service.set_connector(connector["id"], {
        "authorization_status": "approved", "enabled": 1,
        "mode": "official_api", "endpoint": "https://source.example/templates",
    })

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return json.dumps({"items": [
                {"module": "emoji", "title": "猫猫", "source_url": "https://source.example/cat"},
                {"module": "mobile_wallpaper", "title": "城市", "source_url": "https://source.example/city"},
            ]}).encode()

    requests = []

    def fake_urlopen(request, timeout=0):
        requests.append((request.full_url, timeout))
        return FakeResponse()

    monkeypatch.setattr(app_module.urllib.request, "urlopen", fake_urlopen)
    first = service.sync_connector(connector["id"], "admin")
    second = service.sync_connector(connector["id"], "admin")

    assert requests == [("https://source.example/templates", 30), ("https://source.example/templates", 30)]
    assert first["created"] == 2 and first["duplicates"] == 0
    assert second["created"] == 0 and second["duplicates"] == 2
    assert len(service.candidates(status="pending_review")) == 2
    assert all(item["connector_id"] == connector["id"] for item in service.candidates())
    assert any(item["action"] == "connector.sync" for item in service.audit_logs())
    connector_state = next(item for item in service.list_connectors() if item["id"] == connector["id"])
    assert connector_state["last_sync_status"] == "succeeded"
    assert connector_state["last_sync_result"]["created"] == 0
    assert connector_state["last_sync_at"]


def test_official_connector_sync_reads_auth_from_environment(service, monkeypatch):
    connector = service.list_connectors()[0]
    service.set_connector(connector["id"], {
        "authorization_status": "approved", "enabled": 1,
        "mode": "official_api", "endpoint": "https://source.example/templates",
        "auth_env": "SOURCE_API_KEY",
    })
    monkeypatch.setenv("SOURCE_API_KEY", "source-secret")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return json.dumps({"items": [{
                "module": "emoji", "title": "授权模板", "source_url": "https://source.example/auth-cat",
            }]}).encode()

    def fake_urlopen(request, timeout=0):
        assert request.headers["Authorization"] == "Bearer source-secret"
        return FakeResponse()

    monkeypatch.setattr(app_module.urllib.request, "urlopen", fake_urlopen)
    result = service.sync_connector(connector["id"], "admin")

    assert result["created"] == 1
    assert service.list_connectors()[0]["auth_env"] == "SOURCE_API_KEY"


def test_official_connector_sync_records_failure_state(service, monkeypatch):
    connector = service.list_connectors()[0]
    service.set_connector(connector["id"], {
        "authorization_status": "approved", "enabled": 1,
        "mode": "official_api", "endpoint": "https://source.example/templates",
    })
    monkeypatch.setattr(app_module.urllib.request, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("upstream timeout")))
    with pytest.raises(ValidationError, match="来源连接器抓取失败"):
        service.sync_connector(connector["id"], "admin")
    connector_state = next(item for item in service.list_connectors() if item["id"] == connector["id"])
    assert connector_state["last_sync_status"] == "failed"
    assert "upstream timeout" in connector_state["last_sync_result"]["error"]
    assert any(item["action"] == "connector.sync_failed" for item in service.audit_logs())


def test_admin_can_create_a_disabled_custom_model(service):
    created = service.create_model({
        "id": "custom-qwen", "provider": "qwen", "name": "我的千问模型",
        "remote_model_id": "qwen-image-3.0", "adapter": "qwen_http",
    })
    assert created["enabled"] == 0
    assert any(model["id"] == "custom-qwen" for model in service.models())
    with pytest.raises(ValidationError, match="模型 ID 已存在"):
        service.create_model({"id": "custom-qwen", "provider": "qwen", "name": "重复模型"})
    service.set_model("custom-qwen", {"enabled": 1})
    with pytest.raises(ValidationError, match="请先停用"):
        service.delete_model("custom-qwen")
    service.set_model("custom-qwen", {"enabled": 0})
    deleted = service.delete_model("custom-qwen")
    assert deleted["status"] == "deleted"
    assert not any(model["id"] == "custom-qwen" for model in service.models())


def test_builtin_or_used_models_cannot_be_deleted(service):
    with pytest.raises(ValidationError, match="内置模型不能删除"):
        service.delete_model("qwen-image")
    service.create_model({"id": "used-custom", "provider": "qwen", "name": "Used", "enabled": 1})
    service.generate({"module": "emoji", "template_id": "emoji-default", "prompt": "测试", "model_id": "used-custom", "user_id": "u1"})
    service.set_model("used-custom", {"enabled": 0})
    with pytest.raises(ValidationError, match="已有调用记录"):
        service.delete_model("used-custom")


def test_sensenova_chat_ai_review_persists_flag_without_publishing(service, monkeypatch):
    service.set_model("sensenova-6.8-flash-lite", {
        "enabled": 1,
        "adapter": "sensenova_chat",
        "endpoint": "https://token.sensenova.cn/v1/chat/completions",
        "auth_env": "SENSENOVA_API_KEY",
        "remote_model_id": "sensenova-6.8-flash-lite",
    })
    monkeypatch.setenv("SENSENOVA_API_KEY", "test-sensenova-key")
    candidate = service.ingest_candidate({
        "module": "emoji", "title": "待审核模板", "prompt_template": "人物裸露风格",
        "source_url": "https://example.com/review-cat",
    })
    calls = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": json.dumps({
                    "verdict": "flag", "reasons": ["成人/裸露风险"], "categories": ["adult"],
                }, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
            }, ensure_ascii=False).encode()

    def fake_urlopen(request, timeout=0):
        calls.append((request, timeout))
        assert request.headers["Authorization"] == "Bearer test-sensenova-key"
        return FakeResponse()

    monkeypatch.setattr(app_module.urllib.request, "urlopen", fake_urlopen)
    result = service.ai_review_candidate(candidate["id"], "admin")

    assert result["ai_review_status"] == "flagged"
    assert result["ai_review_result"]["verdict"] == "flag"
    assert result["ai_review_result"]["categories"] == ["adult"]
    assert result["status"] == "pending_review"
    assert result["ai_review_model_id"] == "sensenova-6.8-flash-lite"
    assert len(calls) == 1
    assert calls[0][0].full_url == "https://token.sensenova.cn/v1/chat/completions"
    request_body = json.loads(calls[0][0].data.decode())
    assert request_body["model"] == "sensenova-6.8-flash-lite"
    assert request_body["messages"][0]["role"] == "system"
    assert service.usage({"provider": "sensenova", "model_id": "sensenova-6.8-flash-lite"})["tokens"] == 20
    listed = service.candidates()[0]
    assert listed["ai_review_status"] == "flagged"


def test_sensenova_chat_ai_review_requires_enabled_understanding_model(service):
    candidate = service.ingest_candidate({
        "module": "emoji", "title": "待审核模板", "source_url": "https://example.com/review-dog",
    })
    with pytest.raises(ValidationError, match="理解模型"):
        service.ai_review_candidate(candidate["id"])


def test_sensenova_chat_model_configuration_check(service, monkeypatch):
    updated = service.set_model("sensenova-6.8-flash-lite", {
        "enabled": 1,
        "adapter": "sensenova_chat",
        "endpoint": "https://token.sensenova.cn/v1/chat/completions",
        "auth_env": "SENSENOVA_API_KEY",
        "remote_model_id": "sensenova-6.8-flash-lite",
    })
    monkeypatch.setenv("SENSENOVA_API_KEY", "test-sensenova-key")
    assert updated["adapter"] == "sensenova_chat"
    assert service.check_model("sensenova-6.8-flash-lite")["ready"] is True
